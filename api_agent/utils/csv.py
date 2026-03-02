"""CSV conversion helpers."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from typing import Any

from ..executor import _connect_duckdb, _sandbox


def to_csv(data: Any) -> str:
    """Convert data to CSV via DuckDB."""
    if not data:
        return ""
    if not isinstance(data, list):
        data = [data]

    temp_file = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            temp_file = f.name

        conn = _connect_duckdb()
        conn.execute("CREATE TABLE t AS SELECT * FROM read_json_auto(?)", [temp_file])
        _sandbox(conn)  # Lock down after data load
        result = conn.execute("SELECT * FROM t")

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([desc[0] for desc in result.description])
        writer.writerows(result.fetchall())
        conn.close()
        return output.getvalue()
    finally:
        if temp_file:
            try:
                os.unlink(temp_file)
            except OSError:
                pass
