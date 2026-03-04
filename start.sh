#!/bin/sh
[ -n "${PORT:-}" ] || PORT=3000
[ -n "${HOST:-}" ] || HOST=0.0.0.0
export PORT HOST
exec uv run --frozen python -m api_agent
