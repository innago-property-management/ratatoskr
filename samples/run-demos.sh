#!/usr/bin/env bash
# Launch demo ratatoskr instances for MCP Inspector
#
# Usage:
#   ./samples/run-demos.sh
#
# Then add to mcp-langchain-bridge:
#   mcp-bridge add sse starwars    → http://localhost:3941/mcp
#   mcp-bridge add sse dadjokes    → http://localhost:3942/mcp
#   mcp-bridge add sse nasa        → http://localhost:3943/mcp
#
# Then launch inspector:
#   npx @modelcontextprotocol/inspector --transport stdio -- ../mcp-langchain-bridge/start-bridge.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "Starting demo ratatoskr instances..."
echo ""

# Star Wars GraphQL — port 3941
echo "[starwars] GraphQL on :3941"
API_AGENT_DEFAULT_TARGET_URL="https://swapi-graphql.netlify.app/.netlify/functions/graphql" \
API_AGENT_DEFAULT_API_TYPE="graphql" \
API_AGENT_MCP_NAME="Star Wars" \
  uv run python -m api_agent --port 3941 &

# Dad Jokes REST — port 3942
echo "[dadjokes] REST on :3942"
API_AGENT_DEFAULT_TARGET_URL="https://raw.githubusercontent.com/innago-property-management/ratatoskr/main/samples/dad-jokes-openapi.json" \
API_AGENT_DEFAULT_API_TYPE="rest" \
API_AGENT_DEFAULT_TARGET_HEADERS='{"Accept":"application/json","User-Agent":"ratatoskr-demo"}' \
API_AGENT_MCP_NAME="Dad Jokes" \
  uv run python -m api_agent --port 3942 &

# NASA APOD REST — port 3943
echo "[nasa]     REST on :3943"
API_AGENT_DEFAULT_TARGET_URL="https://raw.githubusercontent.com/APIs-guru/openapi-directory/main/APIs/nasa.gov/apod/1.0.0/openapi.yaml" \
API_AGENT_DEFAULT_API_TYPE="rest" \
API_AGENT_DEFAULT_BASE_URL="https://api.nasa.gov/planetary" \
API_AGENT_DEFAULT_TARGET_HEADERS='{"Accept":"application/json"}' \
API_AGENT_MCP_NAME="NASA APOD" \
  uv run python -m api_agent --port 3943 &

echo ""
echo "Instances starting on ports 3941-3943."
echo ""
echo "Add to mcp-langchain-bridge:"
echo "  mcp-bridge add sse starwars    # url: http://localhost:3941/mcp"
echo "  mcp-bridge add sse dadjokes    # url: http://localhost:3942/mcp"
echo "  mcp-bridge add sse nasa        # url: http://localhost:3943/mcp"
echo ""
echo "Then: npx @modelcontextprotocol/inspector --transport stdio -- ../mcp-langchain-bridge/start-bridge.sh"
echo ""
echo "Press Ctrl+C to stop all."
wait
