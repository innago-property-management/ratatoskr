"""API Agent MCP Server - Universal GraphQL/REST to MCP gateway."""

import argparse
import logging
import os
from typing import Literal, cast

import uvicorn
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import settings
from .middleware import DynamicToolNamingMiddleware
from .tools import register_all_tools

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("openai", "anthropic", "openai-compat")


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments. Values override env vars."""
    parser = argparse.ArgumentParser(
        prog="api-agent",
        description="Universal MCP server — turn any API into an MCP endpoint. "
        "Query in natural language, get results even when the API can't.",
    )
    parser.add_argument(
        "--provider",
        choices=SUPPORTED_PROVIDERS,
        default=None,
        help="LLM provider (default: openai). Overrides API_AGENT_PROVIDER env var.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name (default: provider-specific). Overrides API_AGENT_MODEL_NAME.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key. Overrides API_AGENT_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Custom LLM endpoint URL. Required for openai-compat provider.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Server port (default: {settings.PORT}).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help=f"Server host (default: {settings.HOST}).",
    )
    parser.add_argument(
        "--transport",
        choices=("http", "streamable-http", "sse"),
        default=None,
        help=f"MCP transport (default: {settings.TRANSPORT}).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug logging.",
    )
    return parser.parse_args()


def apply_cli_overrides(args: argparse.Namespace) -> None:
    """Apply CLI arguments as env var overrides before settings are used.

    CLI args take highest precedence: CLI > env vars > .env file > defaults.
    We set env vars so that the pydantic Settings() picks them up when
    modules import `settings` and create providers.
    """
    if args.provider:
        os.environ["API_AGENT_PROVIDER"] = args.provider
    if args.model:
        os.environ["API_AGENT_MODEL_NAME"] = args.model
    if args.api_key:
        os.environ["API_AGENT_API_KEY"] = args.api_key
    if args.base_url:
        os.environ["API_AGENT_BASE_URL"] = args.base_url
    if args.port:
        os.environ["API_AGENT_PORT"] = str(args.port)
    if args.host:
        os.environ["API_AGENT_HOST"] = args.host
    if args.transport:
        os.environ["API_AGENT_TRANSPORT"] = args.transport
    if args.debug:
        os.environ["API_AGENT_DEBUG"] = "true"


def create_app():
    """Create MCP server application."""
    mcp = FastMCP(settings.MCP_NAME)
    register_all_tools(mcp)
    mcp.add_middleware(DynamicToolNamingMiddleware())

    cors_origins = [o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",")]
    middleware = [
        Middleware(
            CORSMiddleware,  # type: ignore[arg-type]  # Starlette middleware typing
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=[
                "Content-Type",
                "Authorization",
                "MCP-Session-Id",
                "mcp-protocol-version",
            ],
            allow_credentials=True,
            max_age=600,
        ),
    ]

    transport = cast(Literal["http", "streamable-http", "sse"], settings.TRANSPORT)
    app = mcp.http_app(middleware=middleware, transport=transport)

    async def health(request):
        return JSONResponse({"status": "ok"})

    app.router.routes.append(Route("/health", health, methods=["GET"]))
    return app


def main():
    """Run server with CLI argument support."""
    args = parse_args()
    apply_cli_overrides(args)

    # Reload settings after env overrides
    from .config import Settings

    reloaded = Settings()
    host = reloaded.HOST
    port = reloaded.PORT

    if reloaded.DEBUG:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"Starting API Agent on {host}:{port}")
    logger.info(f"Provider: {reloaded.PROVIDER} | Model: {reloaded.MODEL_NAME or '(default)'}")
    logger.info("Endpoint config via headers: X-Target-URL, X-API-Type, X-Target-Headers")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
