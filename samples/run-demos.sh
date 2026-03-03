#!/usr/bin/env bash
# Launch demo ratatoskr instances for MCP Inspector
#
# Prerequisites:
#   export ANTHROPIC_API_KEY="..."  (or set API_AGENT_PROVIDER=openai with OPENAI_API_KEY)
#
# Usage:
#   ./samples/run-demos.sh
#
# Then connect MCP Inspector (Streamable HTTP):
#   Star Wars:  http://localhost:3941/mcp
#   Dad Jokes:  http://localhost:3942/mcp
#   NASA APOD:  http://localhost:3943/mcp
#
# Or launch MCP Inspector directly:
#   npx @modelcontextprotocol/inspector

set -euo pipefail
cd "$(dirname "$0")/.."

# Use Anthropic provider by default (set ANTHROPIC_API_KEY before running)
export API_AGENT_PROVIDER="${API_AGENT_PROVIDER:-anthropic}"

echo "Starting demo ratatoskr instances (provider: $API_AGENT_PROVIDER)..."
echo ""

# Star Wars GraphQL — port 3941
echo "[starwars] GraphQL on :3941"
API_AGENT_DEFAULT_TARGET_URL="https://swapi-graphql.netlify.app/.netlify/functions/graphql" \
API_AGENT_DEFAULT_API_TYPE="graphql" \
API_AGENT_MCP_NAME="Star Wars" \
  uv run python -m api_agent --port 3941 &

# Dad Jokes GraphQL — port 3942
echo "[dadjokes] GraphQL on :3942"
API_AGENT_DEFAULT_TARGET_URL="https://icanhazdadjoke.com/graphql" \
API_AGENT_DEFAULT_API_TYPE="graphql" \
API_AGENT_MCP_NAME="Dad Jokes" \
  uv run python -m api_agent --port 3942 &

# NASA APOD REST — port 3943
echo "[nasa]     REST on :3943"
API_AGENT_DEFAULT_TARGET_URL="https://raw.githubusercontent.com/APIs-guru/openapi-directory/main/APIs/nasa.gov/apod/1.0.0/openapi.yaml" \
API_AGENT_DEFAULT_API_TYPE="rest" \
API_AGENT_DEFAULT_BASE_URL="https://api.nasa.gov/planetary?api_key=DEMO_KEY" \
API_AGENT_DEFAULT_TARGET_HEADERS='{"Accept":"application/json"}' \
API_AGENT_MCP_NAME="NASA APOD" \
  uv run python -m api_agent --port 3943 &

echo ""
echo "Instances starting on ports 3941-3943."
echo ""
echo "Connect via MCP Inspector (Streamable HTTP):"
echo "  Star Wars:  http://localhost:3941/mcp"
echo "  Dad Jokes:  http://localhost:3942/mcp"
echo "  NASA APOD:  http://localhost:3943/mcp"
echo ""
echo "Press Ctrl+C to stop all."
wait
