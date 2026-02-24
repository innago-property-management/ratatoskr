# Spec: TOON Format Integration

**User Story:** As a user of Ratatoskr, I want to leverage the TOON format for data serialization within the agent's context to significantly reduce token consumption, leading to lower operational costs and potentially faster LLM interactions.

## Acceptance Criteria

1.  **Tool Result Encoding:** All data returned from tool calls (GraphQL, REST, SQL) and fed back into the LLM context must be encoded in the TOON format.
2.  **Schema Compression:** API schemas (GraphQL introspection, OpenAPI specs) provided in the system prompt must be encoded in the TOON format.
3.  **DuckDB Result Formatting:** Results from DuckDB SQL post-processing must be formatted as TOON tabular arrays before being passed to the LLM.
4.  **Conditional MCP Output:** The MCP server can optionally provide TOON-formatted output if the client requests it via an `X-Output-Format: toon` header.
5.  **Observability:** The system provides metrics to measure the impact and performance of TOON integration, including token savings and any potential impact on output quality.
6.  **Configuration:** A global configuration flag (`USE_TOON`) allows enabling or disabling TOON encoding for all processing stages.
7.  **Fallback Mechanism:** In case of a TOON encoding error, the system gracefully falls back to JSON and logs the error, ensuring agent execution is not halted.
8.  **Dependency Management:** The `toon` python library is added as a project dependency, with considerations for its beta status.

## Out of Scope

*   This feature is focused on internal context compression and optional output formatting. It does not include supporting TOON for incoming MCP requests.
*   Changes to the LLM's core logic beyond data serialization are not included.

## Risks

*   **Dependency Risk:** The `toon-python` library is in beta (v0.9.x). This introduces a risk of bugs, API changes, or performance issues. A fallback mechanism to JSON is critical.
*   **LLM Performance:** While TOON is designed for LLMs, there's a possibility that some models may perform worse with this new format compared to JSON. A/B testing and quality monitoring are necessary.
*   **Encoding Overhead:** The process of encoding data into TOON format might introduce a small amount of latency. This should be monitored.

## Open Questions

1.  Should the `toon` library be vendored into the project to mitigate the risks of a beta dependency?
2.  What is the best way to implement the A/B testing capability (per-request or global)?
3.  Are there any specific parts of the schemas that are not well-suited for TOON encoding?
