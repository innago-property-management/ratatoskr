"""Executor for GraphQL queries and DuckDB SQL processing."""

import json
import os
import re
import tempfile
from typing import Any

import duckdb
import structlog

from .graphql import execute_query as graphql_execute

logger = structlog.get_logger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def _connect_duckdb() -> duckdb.DuckDBPyConnection:
    """Create a DuckDB in-memory connection."""
    return duckdb.connect()


def _sandbox(conn: duckdb.DuckDBPyConnection) -> None:
    """Disable external file/network access on a DuckDB connection.

    Call AFTER loading data (read_json_auto needs file access) but
    BEFORE executing user-provided or template-rendered SQL.

    Prevents: read_csv_auto(), COPY TO, httpfs, and all external I/O.
    """
    conn.execute("SET enable_external_access = false")


def _safe_table_name(name: str) -> str:
    """Sanitize table name to alphanumeric + underscore only.

    Prevents SQL injection via table name interpolation.
    """
    sanitized = _SAFE_NAME_RE.sub("", name)
    if not sanitized or sanitized[0].isdigit():
        sanitized = "t_" + sanitized
    return sanitized[:64]


def extract_tables_from_response(
    data: Any, name: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Extract data from API response for DuckDB tables.

    DuckDB handles nested JSON natively:
    - STRUCT for nested objects (access via dot notation: user.name)
    - Arrays for lists (use unnest() to expand)

    Args:
        data: API response data (dict or list)
        name: User-specified table name

    Returns:
        Tuple of (tables_dict, schema_info_or_none)
        - tables_dict: {name: data} for storage
        - schema_info: Only for wrapped dicts (1-row tables), None otherwise
    """
    # Direct list response
    if isinstance(data, list):
        return {name: data}, None

    if not isinstance(data, dict):
        return {}, None

    # Dict response: prefer top-level list if exists
    for value in data.values():
        if isinstance(value, list):
            return {name: value}, None

    # No list found - wrap whole dict as single-row table + extract schema
    wrapped = [data]
    schema_info = _extract_schema(wrapped, name)
    return {name: wrapped}, schema_info


def _extract_schema(data: list[dict], table_name: str) -> dict[str, Any]:
    """Extract DuckDB schema from data (internal helper).

    Creates one temp file to get schema info for wrapped dicts.
    """
    if not data:
        return {"rows": 0, "schema": "", "hint": "Empty table"}

    temp_file = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            temp_file = f.name

        conn = _connect_duckdb()
        safe_name = _safe_table_name(table_name)
        try:
            conn.execute(
                f'CREATE TABLE "{safe_name}" AS SELECT * FROM read_json_auto(?)',
                [temp_file],
            )
            _sandbox(conn)  # Lock down after data load
            schema = conn.execute(f'DESCRIBE "{safe_name}"').fetchall()
        finally:
            conn.close()

        schema_str = ", ".join([f"{col[0]}: {col[1]}" for col in schema])

        return {
            "rows": len(data),
            "schema": schema_str,
            "hint": f'Use sql_query() to access fields. Example: SELECT "{schema[0][0]}" FROM "{safe_name}"',
        }
    except Exception as e:
        logger.exception("schema_extraction_error")
        return {"rows": len(data), "schema": "unknown", "hint": str(e)}
    finally:
        if temp_file:
            try:
                os.unlink(temp_file)
            except OSError:
                pass


def truncate_for_context(
    data: list[dict], table_name: str, max_chars: int | None = None
) -> dict[str, Any]:
    """Truncate data for LLM context safety, include schema if truncated.

    Args:
        data: List of records
        table_name: Name for the table
        max_chars: Max chars for response (defaults to settings.MAX_TOOL_RESPONSE_CHARS)

    Returns:
        Always: {"table", "rows", "data" (list), "truncated", "showing"?, "schema"?, "hint"?}
    """
    from .config import settings

    max_chars = max_chars or settings.MAX_TOOL_RESPONSE_CHARS
    total_rows = len(data)

    # Check if full data fits
    if len(json.dumps(data)) <= max_chars:
        return {"table": table_name, "rows": total_rows, "data": data, "truncated": False}

    # Find how many complete rows fit
    preview: list[dict] = []
    current_size = 2  # "[]"
    for row in data:
        row_json = json.dumps(row)
        new_size = current_size + len(row_json) + (1 if preview else 0)
        if new_size > max_chars:
            break
        preview.append(row)
        current_size = new_size

    schema = _extract_schema(data, table_name)
    return {
        "table": table_name,
        "rows": total_rows,
        "showing": len(preview),
        "schema": schema.get("schema", ""),
        "data": preview,
        "truncated": True,
        "hint": f"Showing {len(preview)}/{total_rows}. Use sql_query to filter.",
    }


# Keep for backwards compatibility
def get_table_schema_summary(data: list[dict], table_name: str) -> dict[str, Any]:
    """Get DuckDB schema summary (deprecated, use extract_tables_from_response)."""
    return _extract_schema(data, table_name)


_DDL_DML_RE = re.compile(
    r"\b(?:CREATE|DROP|INSERT|UPDATE|DELETE|ALTER|TRUNCATE|ATTACH|DETACH|COPY|LOAD|INSTALL|EXPORT|IMPORT)\b",
    re.I,
)

# Strip SQL comments before checking for DDL/DML
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _validate_sql_readonly(query: str) -> str | None:
    """Validate that SQL is read-only (SELECT/WITH only). Returns error message or None."""
    stripped = _SQL_LINE_COMMENT.sub("", query)
    stripped = _SQL_BLOCK_COMMENT.sub("", stripped)
    stripped = stripped.strip()
    if not stripped:
        return "Empty SQL query"
    # Block multi-statement queries (no legitimate need for semicolons)
    if ";" in stripped:
        return "Multi-statement queries are not allowed"
    if _DDL_DML_RE.search(stripped):
        return "Only SELECT and WITH (CTE) statements are allowed"
    # Check that the top-level statement starts with SELECT or WITH
    if not re.match(r"^\s*(?:SELECT|WITH)\b", stripped, re.I):
        return "Only SELECT and WITH (CTE) statements are allowed"
    return None


def execute_sql(data: Any, query: str) -> dict[str, Any]:
    """Execute SQL query on JSON data using DuckDB.

    Args:
        data: JSON data (dict or list) to query
        query: DuckDB SQL query (use table names matching top-level keys, e.g. 'posts')

    Returns:
        Dict with success/result or error
    """
    # Defense-in-depth: block DDL/DML before execution
    validation_error = _validate_sql_readonly(query)
    if validation_error:
        return {"success": False, "error": f"SQL blocked: {validation_error}"}
    temp_files = []
    conn = None
    try:
        conn = _connect_duckdb()

        # Register top-level keys as tables via temp JSON files
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list) and value:
                    safe_key = _safe_table_name(key)
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                        json.dump(value, f)
                        temp_files.append(f.name)
                    conn.execute(
                        f'CREATE TABLE "{safe_key}" AS SELECT * FROM read_json_auto(?)',
                        [f.name],
                    )
        elif isinstance(data, list):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(data, f)
                temp_files.append(f.name)
            conn.execute(
                'CREATE TABLE "data" AS SELECT * FROM read_json_auto(?)',
                [f.name],
            )

        # Sandbox: disable external access before running user-provided SQL
        _sandbox(conn)

        # Execute query
        result = conn.execute(query).fetchall()
        columns = [desc[0] for desc in conn.description or []]

        # Convert to list of dicts
        rows = [dict(zip(columns, row)) for row in result]

        return {"success": True, "result": rows}

    except duckdb.Error as e:
        return {"success": False, "error": f"SQL error: {e}"}
    except Exception as e:
        logger.exception("sql_execution_error")
        return {"success": False, "error": str(e)}
    finally:
        if conn:
            conn.close()
        # Cleanup temp files
        for temp_file in temp_files:
            try:
                os.unlink(temp_file)
            except OSError:
                pass


async def execute_graphql(
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute GraphQL query (wrapper around graphql client).

    Args:
        query: GraphQL query string
        variables: Optional query variables

    Returns:
        Dict with success/data or error
    """
    return await graphql_execute(query, variables)
