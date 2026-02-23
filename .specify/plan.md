# Plan: TOON Format Integration

This plan outlines the technical steps required to integrate the TOON format into the Ratatoskr agent, based on the approved specification.

## Phase 1: Setup and Core Integration (1-2 days)

**Objective:** Integrate the `toon` library and implement the core encoding logic for tool results.

1.  **Dependency:** Add `toon` to `pyproject.toml`.
    *   Research current stability and decide whether to pin to a specific beta version or allow for minor updates.
2.  **Configuration:** Add `USE_TOON` boolean flag to `api_agent/config.py`.
    *   Default to `False`.
    *   This will act as the global kill-switch for all TOON-related functionality.
3.  **Core Encoding Logic:** In `api_agent/llm/provider.py`, modify the `_execute_tools` method.
    *   When a tool returns a result, check the `USE_TOON` flag.
    *   If `True`, attempt to encode the `content` (which is JSON or a JSON-serializable object) into TOON format.
    *   Implement a `try...except` block. If TOON encoding fails, log the error and fall back to the original JSON string.
    *   The `ToolResult.content` field will then contain either a TOON string or a JSON string.

## Phase 2: Schema and DuckDB Integration (1 day)

**Objective:** Extend TOON encoding to API schemas and DuckDB results.

1.  **GraphQL Schema:** In `api_agent/agent/graphql_agent.py`, modify `_build_schema_context`.
    *   After generating the compact SDL-like context, if `USE_TOON` is `True`, encode the generated string or the underlying schema object into TOON format.
2.  **REST Schema:** In `api_agent/rest/schema_loader.py`, modify `fetch_schema_context`.
    *   Similar to the GraphQL agent, encode the generated schema context into TOON format if `USE_TOON` is `True`.
3.  **DuckDB Results:** In `api_agent/executor.py`, modify the `execute_sql` function.
    *   When SQL execution is successful, and before returning the JSON result, check `USE_TOON`.
    *   If `True`, format the list of dictionaries into a TOON tabular array.

## Phase 3: Observability and Metrics (1 day)

**Objective:** Implement logging and metrics to measure the effectiveness of TOON.

1.  **Token Counting:**
    *   In `api_agent/llm/provider.py`, before sending the payload to the LLM, calculate the token count for both the TOON-encoded context and the equivalent JSON context.
    *   Log these values for comparison.
2.  **OpenTelemetry:**
    *   Create a new utility function to add span attributes.
    *   In the TOON encoding logic (in `provider.py`, `graphql_agent.py`, etc.), add attributes to the active span, such as `toon.encoded=true`, `toon.bytes_saved`, `toon.token_reduction_ratio`.
    *   If an encoding error occurs, add an event to the span with the error details.
3.  **A/B Testing Support:**
    *   The global `USE_TOON` flag serves as the primary A/B test mechanism.
    *   For per-request A/B testing, introduce a context variable that can be set by a middleware based on an incoming header (e.g., `X-Toon-Enable: true`). The encoding functions will check this context variable first, then fall back to the global `USE_TOON` setting.

## Phase 4: Optional MCP Output (0.5 days)

**Objective:** Implement the optional TOON output format for MCP clients.

1.  **Middleware/Decorator:** In the main server file (where the web framework is configured), add a middleware or decorator to inspect the `X-Output-Format` header.
2.  **Conditional Encoding:** If the header is present and set to `toon`, and the agent's execution is successful, encode the final response data in TOON format before sending it to the client.
3.  **Content-Type Header:** Set the `Content-Type` header to `application/toon` or `text/plain` as appropriate.

## Phase 5: Testing and Documentation (1 day)

**Objective:** Ensure the integration is robust and well-documented.

1.  **Unit Tests:**
    *   Write unit tests for the TOON encoding functions, including the fallback-to-JSON logic.
    *   Add tests for the schema encoding functions.
2.  **Integration Tests:**
    *   Run the existing integration tests with `USE_TOON=True` to ensure that the agent's performance and accuracy are not negatively affected.
3.  **Documentation:**
    *   Update `README.md` to explain the new `USE_TOON` setting and the `X-Output-Format` header.
    *   Add a section to the developer documentation explaining the TOON integration and the observability metrics.
