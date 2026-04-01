# Quickstart: Recipe Persistence

## Default (zero-config)

```bash
# Install and run — recipes persist automatically
pip install api-agent-ratatoskr
uv run api-agent --provider anthropic --api-key sk-ant-...

# Recipes saved to ~/.cache/ratatoskr/recipes.db (created automatically)
# Restart the server — recipes are restored instantly
```

## Configuration

```bash
# Use SQLite persistence (default when sqlite backend available)
API_AGENT_RECIPE_PERSISTENCE=sqlite

# Custom storage path
API_AGENT_RECIPE_SQLITE_PATH=/data/ratatoskr/recipes.db

# Disable persistence (in-memory only, current behavior)
API_AGENT_RECIPE_PERSISTENCE=memory
```

## Docker

```bash
# Mount a volume for recipe persistence
docker run -p 3000:3000 \
  -e OPENAI_API_KEY="..." \
  -v ratatoskr-cache:/home/appuser/.cache/ratatoskr \
  ghcr.io/innago-property-management/ratatoskr:latest
```

## Verify

```bash
# Check if recipes are persisted
sqlite3 ~/.cache/ratatoskr/recipes.db "SELECT api_id, recipe_id, datetime(created_at, 'unixepoch') FROM recipes"
```
