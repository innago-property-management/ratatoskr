"""Recipe execution outside agent context (for MCP recipe tools)."""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from ..agent.graphql_agent import fetch_graphql_schema_raw
from ..context import RequestContext
from ..executor import extract_tables_from_response
from ..graphql import execute_query as graphql_execute
from ..grpc.client import (
    execute_bidi_streaming_rpc,
    execute_client_streaming_rpc,
    execute_server_streaming_rpc,
    execute_unary_rpc,
)
from ..grpc.reflection import fetch_schema as fetch_grpc_schema
from ..rest.client import execute_request
from ..rest.schema_loader import fetch_schema_context
from ..utils.csv import to_csv
from .common import (
    build_api_id,
    error_json,
    execute_recipe_steps,
    format_recipe_response,
    validate_recipe_params,
)
from .store import RECIPE_STORE, render_param_refs, render_text_template, sha256_hex


async def load_schema_and_base_url(ctx: RequestContext) -> tuple[str, str]:
    """Load raw schema and base URL. Returns (raw_schema, base_url)."""
    if ctx.api_type == "graphql":
        raw_schema = await fetch_graphql_schema_raw(ctx.target_url, ctx.target_headers)
        return raw_schema, ""

    if ctx.api_type == "grpc":
        metadata = [(k, v) for k, v in ctx.target_headers.items()] if ctx.target_headers else None
        schema = await fetch_grpc_schema(ctx.target_url, metadata=metadata)
        return schema.raw_schema_text, ""

    _, spec_base_url, raw_spec_json = await fetch_schema_context(ctx.target_url, ctx.target_headers)
    base_url = ctx.base_url or spec_base_url or ""
    return raw_spec_json, base_url


async def execute_recipe_tool(
    ctx: RequestContext,
    recipe_id: str,
    params: dict[str, Any] | None,
    return_directly: bool = True,
    *,
    raw_schema: str = "",
    base_url: str = "",
) -> str:
    """Execute a recipe by id and return JSON string."""
    if not raw_schema:
        raw_schema, base_url = await load_schema_and_base_url(ctx)
    if not raw_schema:
        return error_json("schema not loaded")

    meta = RECIPE_STORE.get_recipe_meta(recipe_id)
    if not meta:
        return error_json(f"recipe not found: {recipe_id}")

    schema_hash = sha256_hex(raw_schema)
    api_id = build_api_id(ctx, ctx.api_type, base_url)
    if meta.get("schema_hash") != schema_hash or meta.get("api_id") != api_id:
        return error_json("recipe does not match current API or schema")

    recipe = meta.get("recipe") or {}
    params_spec = recipe.get("params", {})
    provided = params or {}
    validated_params, error = validate_recipe_params(params_spec, provided)
    if error:
        return error

    # Initialize storage for results
    query_results_var: ContextVar[dict[str, Any]] = ContextVar("recipe_query_results")
    last_result_var: ContextVar[list[Any]] = ContextVar("recipe_last_result")
    query_results_var.set({})
    last_result_var.set([None])

    if ctx.api_type == "graphql":
        executed_queries: list[str] = []

        async def graphql_step_executor(step_idx, step, params, results):
            if not isinstance(step, dict) or step.get("kind") != "graphql":
                return False, None, error_json("invalid recipe step"), None

            name = step.get("name") or "data"
            tmpl = step.get("query_template")
            if not isinstance(tmpl, str):
                return False, None, error_json("missing query_template"), None

            query = render_text_template(tmpl, params)
            res = await graphql_execute(query, None, ctx.target_url, ctx.target_headers)
            if not res.get("success"):
                return False, None, error_json(res.get("error", "query failed")), None

            data = res.get("data", {})
            tables, _ = extract_tables_from_response(data, str(name))
            results.update(tables)
            query_results_var.set(results)
            return True, tables.get(str(name)), "", query

        success, last_data, executed_sql, error = await execute_recipe_steps(
            recipe,
            validated_params or {},
            query_results_var,
            last_result_var,
            graphql_step_executor,
            executed_queries,
        )
        if not success:
            return error

        if return_directly:
            return to_csv(last_data)
        return format_recipe_response(
            last_result_var, executed_queries, executed_sql, "executed_queries"
        )

    if ctx.api_type == "grpc":
        # gRPC execution
        metadata = [(k, v) for k, v in ctx.target_headers.items()] if ctx.target_headers else None
        # Fetch schema for descriptor pool (needed by gRPC client)
        grpc_schema = await fetch_grpc_schema(ctx.target_url, metadata=metadata)

        executed_rpcs: list[dict[str, Any]] = []

        async def grpc_step_executor(step_idx, step, params, results):
            if not isinstance(step, dict) or step.get("kind") != "grpc":
                return False, None, error_json("invalid recipe step"), None

            method_path = str(step.get("method", ""))
            name = str(step.get("name") or "data")

            try:
                request_obj = render_param_refs(step.get("request") or {}, params)
            except KeyError as e:
                return False, None, error_json(str(e)), None

            # Find method in schema to determine streaming type
            method_info = None
            for svc in grpc_schema.services:
                for m in svc.methods:
                    if m.full_method_path.lstrip("/") == method_path.lstrip("/"):
                        method_info = m
                        break

            if not method_info:
                return False, None, error_json(f"method not found: {method_path}"), None

            # Route by streaming type
            if method_info.client_streaming and method_info.server_streaming:
                req_list = request_obj if isinstance(request_obj, list) else [request_obj]
                res = await execute_bidi_streaming_rpc(
                    target_url=ctx.target_url, method_path=method_info.full_method_path,
                    requests_json=req_list, pool=grpc_schema.pool,
                    input_type_name=method_info.input_type, output_type_name=method_info.output_type,
                    metadata=metadata,
                )
            elif method_info.client_streaming:
                req_list = request_obj if isinstance(request_obj, list) else [request_obj]
                res = await execute_client_streaming_rpc(
                    target_url=ctx.target_url, method_path=method_info.full_method_path,
                    requests_json=req_list, pool=grpc_schema.pool,
                    input_type_name=method_info.input_type, output_type_name=method_info.output_type,
                    metadata=metadata,
                )
            elif method_info.server_streaming:
                res = await execute_server_streaming_rpc(
                    target_url=ctx.target_url, method_path=method_info.full_method_path,
                    request_json=request_obj if isinstance(request_obj, dict) else {},
                    pool=grpc_schema.pool,
                    input_type_name=method_info.input_type, output_type_name=method_info.output_type,
                    metadata=metadata,
                )
            else:
                res = await execute_unary_rpc(
                    target_url=ctx.target_url, method_path=method_info.full_method_path,
                    request_json=request_obj if isinstance(request_obj, dict) else {},
                    pool=grpc_schema.pool,
                    input_type_name=method_info.input_type, output_type_name=method_info.output_type,
                    metadata=metadata,
                )

            if not res.get("success"):
                return False, None, error_json(res.get("error", "RPC failed")), None

            data = res.get("data", {})
            if isinstance(data, list):
                tables = {name: data}
            else:
                tables, _ = extract_tables_from_response(data, name)
            results.update(tables)
            query_results_var.set(results)

            rpc_rec = {"method": method_path, "request": json.dumps(request_obj), "name": name, "success": True}
            return True, tables.get(name), "", rpc_rec

        success, last_data, executed_sql, error = await execute_recipe_steps(
            recipe, validated_params or {}, query_results_var, last_result_var,
            grpc_step_executor, executed_rpcs,
        )
        if not success:
            return error

        if return_directly:
            return to_csv(last_data)
        return format_recipe_response(last_result_var, executed_rpcs, executed_sql, "executed_rpcs")

    # REST execution
    if not base_url:
        return error_json("Could not determine base URL for REST API")

    executed_calls: list[dict[str, Any]] = []

    async def rest_step_executor(step_idx, step, params, results):
        if not isinstance(step, dict) or step.get("kind") != "rest":
            return False, None, error_json("invalid recipe step"), None

        method = str(step.get("method", "GET")).upper()
        path = str(step.get("path", ""))
        name = str(step.get("name") or "data")

        try:
            pp = render_param_refs(step.get("path_params") or {}, params)
            qp = render_param_refs(step.get("query_params") or {}, params)
            bd = render_param_refs(step.get("body") or {}, params)
        except KeyError as e:
            return False, None, error_json(str(e)), None

        path_params = pp if isinstance(pp, dict) else None
        query_params = qp if isinstance(qp, dict) else None
        body = bd if isinstance(bd, dict) and bd else None

        res = await execute_request(
            method,
            path,
            path_params,
            query_params,
            body,
            base_url=base_url,
            headers=ctx.target_headers,
            allow_unsafe_paths=list(ctx.allow_unsafe_paths),
        )
        if not res.get("success"):
            return False, None, error_json(res.get("error", "request failed")), None

        data = res.get("data", {})
        tables, _ = extract_tables_from_response(data, name)
        results.update(tables)
        query_results_var.set(results)

        call_rec = {
            "method": method,
            "path": path,
            "path_params": json.dumps(path_params) if path_params else "",
            "query_params": json.dumps(query_params) if query_params else "",
            "body": json.dumps(body) if body else "",
            "name": name,
            "success": True,
        }
        return True, tables.get(name), "", call_rec

    success, last_data, executed_sql, error = await execute_recipe_steps(
        recipe,
        validated_params or {},
        query_results_var,
        last_result_var,
        rest_step_executor,
        executed_calls,
    )
    if not success:
        return error

    if return_directly:
        return to_csv(last_data)
    return format_recipe_response(last_result_var, executed_calls, executed_sql, "executed_calls")
