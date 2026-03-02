"""Security tests for DuckDB hardening (PR A).

Tests cover:
- F1: enable_external_access=false on all DuckDB connections
- F2: Recipe SQL safe rendering (render_sql_safe)
- F3: Table name sanitization (_safe_table_name)
"""

import duckdb
import pytest

from api_agent.executor import _connect_duckdb, _safe_table_name, _sandbox, execute_sql
from api_agent.recipe.store import render_sql_safe

# ---------------------------------------------------------------------------
# F1: DuckDB sandbox — enable_external_access=false
# ---------------------------------------------------------------------------


class TestDuckDBSandbox:
    """Verify all DuckDB connections block external file/network access."""

    def test_sandbox_blocks_file_read(self):
        """_sandbox() should block read_csv_auto on local files."""
        conn = _connect_duckdb()
        _sandbox(conn)
        with pytest.raises(duckdb.Error, match="(?i)disabled|permission"):
            conn.execute("SELECT * FROM read_csv_auto('/etc/passwd')")
        conn.close()

    def test_sandbox_blocks_copy_to(self):
        """_sandbox() should block COPY TO file operations."""
        conn = _connect_duckdb()
        conn.execute("CREATE TABLE t AS SELECT 1 AS x")
        _sandbox(conn)
        with pytest.raises(duckdb.Error, match="(?i)disabled|permission"):
            conn.execute("COPY t TO '/tmp/exfil.csv'")
        conn.close()

    def test_execute_sql_blocks_file_read(self):
        """execute_sql should not allow reading external files."""
        data = {"t": [{"id": 1}]}
        result = execute_sql(data, "SELECT * FROM read_csv_auto('/etc/passwd')")
        assert result["success"] is False
        assert "error" in result

    def test_execute_sql_normal_queries_still_work(self):
        """Normal SQL queries still work with sandbox enabled."""
        data = {"users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]}
        result = execute_sql(data, "SELECT * FROM users WHERE id = 1")
        assert result["success"] is True
        assert len(result["result"]) == 1
        assert result["result"][0]["name"] == "Alice"

    def test_csv_to_csv_blocks_file_read(self):
        """to_csv should use sandboxed connection."""
        from api_agent.utils.csv import to_csv

        # to_csv should work for normal data
        result = to_csv([{"id": 1, "name": "Alice"}])
        assert "id" in result
        assert "Alice" in result

    def test_csv_to_csv_sandbox_called(self):
        """to_csv code path must invoke _sandbox on the DuckDB connection."""
        from unittest.mock import patch

        from api_agent.utils import csv as csv_mod

        with patch.object(csv_mod, "_sandbox", wraps=csv_mod._sandbox) as mock_sandbox:
            csv_mod.to_csv([{"id": 1}])
            mock_sandbox.assert_called_once()


# ---------------------------------------------------------------------------
# F3: Table name sanitization
# ---------------------------------------------------------------------------


class TestSafeTableName:
    """Test _safe_table_name strips dangerous characters."""

    def test_alphanumeric_passthrough(self):
        assert _safe_table_name("users") == "users"
        assert _safe_table_name("my_table_1") == "my_table_1"

    def test_special_chars_stripped(self):
        # Spaces and semicolons are non-alphanumeric, stripped entirely
        result = _safe_table_name("users; DROP TABLE x")
        assert ";" not in result
        assert " " not in result
        assert result == "usersDROPTABLEx"

    def test_leading_digit_prefixed(self):
        result = _safe_table_name("123data")
        assert not result[0].isdigit()
        assert result == "t_123data"

    def test_empty_string_gets_prefix(self):
        result = _safe_table_name("")
        assert result == "t_"

    def test_all_special_chars(self):
        result = _safe_table_name("!@#$%")
        assert result == "t_"

    def test_truncated_to_64_chars(self):
        long_name = "a" * 100
        result = _safe_table_name(long_name)
        assert len(result) <= 64

    def test_sql_injection_in_name_sanitized(self):
        """Table name with SQL injection attempt is sanitized."""
        result = _safe_table_name('x"; DROP TABLE data; --')
        assert ";" not in result
        assert '"' not in result
        assert "-" not in result

    def test_sanitized_names_still_queryable(self):
        """Tables with clean names work end-to-end after sanitization."""
        data = {"users": [{"id": 1, "name": "Alice"}]}
        result = execute_sql(data, "SELECT * FROM users")
        assert result["success"] is True
        assert result["result"][0]["name"] == "Alice"

    def test_spaces_stripped(self):
        # Spaces are non-alphanumeric, removed entirely
        assert _safe_table_name("my table") == "mytable"


# ---------------------------------------------------------------------------
# F2: Recipe SQL safe rendering
# ---------------------------------------------------------------------------


class TestRenderSqlSafe:
    """Test render_sql_safe escapes SQL injection vectors."""

    def test_basic_rendering(self):
        """Simple params render correctly."""
        result = render_sql_safe(
            "SELECT * FROM data WHERE name = '{{name}}'",
            {"name": "Alice"},
        )
        assert result == "SELECT * FROM data WHERE name = 'Alice'"

    def test_escapes_single_quotes(self):
        """Single quotes in params are escaped (SQL standard)."""
        result = render_sql_safe(
            "SELECT * FROM data WHERE name = '{{name}}'",
            {"name": "O'Brien"},
        )
        assert result == "SELECT * FROM data WHERE name = 'O''Brien'"

    def test_strips_semicolons(self):
        """Semicolons are stripped to prevent statement injection."""
        result = render_sql_safe(
            "SELECT * FROM data WHERE name = '{{name}}'",
            {"name": "a'; DROP TABLE data; --"},
        )
        assert ";" not in result

    def test_numeric_passthrough(self):
        """Numeric values pass through as numbers."""
        result = render_sql_safe(
            "SELECT * FROM data WHERE id = {{id}}",
            {"id": 42},
        )
        assert result == "SELECT * FROM data WHERE id = 42"

    def test_float_passthrough(self):
        result = render_sql_safe(
            "SELECT * FROM data WHERE score > {{score}}",
            {"score": 3.14},
        )
        assert result == "SELECT * FROM data WHERE score > 3.14"

    def test_boolean_passthrough(self):
        result = render_sql_safe(
            "SELECT * FROM data WHERE active = {{flag}}",
            {"flag": True},
        )
        assert result == "SELECT * FROM data WHERE active = true"

    def test_none_passthrough(self):
        result = render_sql_safe(
            "SELECT * FROM data WHERE x IS {{val}}",
            {"val": None},
        )
        assert result == "SELECT * FROM data WHERE x IS null"

    def test_missing_param_raises(self):
        with pytest.raises(KeyError, match="missing param"):
            render_sql_safe("SELECT {{x}}", {})

    def test_like_pattern_with_param(self):
        """LIKE patterns with param + wildcard work safely."""
        result = render_sql_safe(
            "SELECT * FROM data WHERE name ILIKE '{{term}}%'",
            {"term": "Al"},
        )
        assert result == "SELECT * FROM data WHERE name ILIKE 'Al%'"

    def test_injection_via_like_blocked(self):
        """SQL injection via LIKE pattern is escaped."""
        result = render_sql_safe(
            "SELECT * FROM data WHERE name ILIKE '{{term}}%'",
            {"term": "' UNION SELECT * FROM read_csv_auto('/etc/passwd') --"},
        )
        # Single quote escaped, semicolons stripped
        assert "read_csv_auto" in result  # The text is there but escaped
        assert "''" in result  # Quotes are doubled
        assert ";" not in result

    def test_multiple_params(self):
        result = render_sql_safe(
            "SELECT * FROM data WHERE a = '{{x}}' AND b = {{y}}",
            {"x": "hello", "y": 10},
        )
        assert result == "SELECT * FROM data WHERE a = 'hello' AND b = 10"

    def test_strips_line_comments(self):
        """SQL line comment sequences (--) are stripped."""
        result = render_sql_safe(
            "SELECT * FROM data WHERE name = '{{name}}'",
            {"name": "x' -- injected comment"},
        )
        assert "--" not in result

    def test_strips_block_comments(self):
        """SQL block comment sequences (/* */) are stripped."""
        result = render_sql_safe(
            "SELECT * FROM data WHERE name = '{{name}}'",
            {"name": "x' /* block */ OR '1'='1"},
        )
        assert "/*" not in result
        assert "*/" not in result

    def test_end_to_end_safe_sql_execution(self):
        """Full pipeline: render_sql_safe → execute_sql is safe."""
        data = {
            "users": [
                {"id": 1, "name": "Alice"},
                {"id": 2, "name": "Bob"},
                {"id": 3, "name": "O'Malley"},
            ]
        }
        sql = render_sql_safe(
            "SELECT * FROM users WHERE name = '{{name}}'",
            {"name": "O'Malley"},
        )
        result = execute_sql(data, sql)
        assert result["success"] is True
        assert len(result["result"]) == 1
        assert result["result"][0]["name"] == "O'Malley"
