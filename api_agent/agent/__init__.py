"""Agents for NL to API conversion."""

from .graphql_agent import process_query
from .grpc_agent import process_grpc_query
from .rest_agent import process_rest_query

__all__ = ["process_query", "process_grpc_query", "process_rest_query"]
