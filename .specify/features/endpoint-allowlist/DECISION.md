# Endpoint Allowlist

**Status:** Decided
**Date:** 2026-02-27
**Complexity:** 8 (new component requiring research + touches all 3 protocol agents)

## Use Case

Ratatoskr currently exposes ALL endpoints from a target API's schema. For large APIs
(500+ endpoints), this causes:

1. **LLM confusion** — massive schemas waste context window, increase hallucination,
   cause wrong endpoint selection and extra turns
2. **Lack of operational control** — ops teams cannot restrict which endpoints are
   exposed through the MCP proxy
3. **Defense in depth** — no way to limit blast radius when the proxy faces untrusted
   clients

## Business Value

- **Better answers** — fewer endpoints = more focused agent = higher accuracy
- **Deployment confidence** — ops can lock down what's exposed per-environment
- **Composability** — clean primitive that works alongside existing safety headers

## Scope Decision

**Allowlist only. No blocklist.**

Blocklist is a footgun with large or evolving APIs — any new endpoint is automatically
exposed. Allowlist enforces principle of least privilege: nothing is visible unless
explicitly opted in.

When the allowlist is not configured, behavior is unchanged (all endpoints exposed).
This preserves full backwards compatibility.

## Design (from Delphi Panel 2026-02-27)

### Configuration: Config + Header, Intersection Semantics

| Mechanism | Who | Purpose | Analogy |
|-----------|-----|---------|---------|
| `API_AGENT_ALLOW_ENDPOINTS` env/config | Ops/platform engineer | Deploy-time ceiling | Firewall rules |
| `X-Allow-Endpoints` header | MCP client developer | Per-session focus lens | Query filter |

**Resolution rules:**
- If both config and header are set: effective set = intersection (header can only narrow)
- If only config is set: config allowlist applies
- If only header is set: header allowlist applies (unrestricted)
- If neither is set: all endpoints exposed (backwards compatible)

### Pattern Format: Glob

| Pattern | Matches |
|---------|---------|
| `GET /users/*` | REST: GET /users/123, GET /users/search |
| `*/users*` | REST: any method on /users paths |
| `Query.user*` | GraphQL: Query.user, Query.users, Query.userById |
| `Mutation.createUser` | GraphQL: exact match (only if mutations are also allowed) |
| `helloworld.Greeter/*` | gRPC: all methods on Greeter service |
| `helloworld.Greeter/SayHello` | gRPC: exact method |

Regex was rejected as overkill and error-prone. Exact match was rejected as too rigid.
Glob (fnmatch-style) hits the sweet spot for all three protocols.

### Filter Point: Schema-Level (Before LLM Sees It)

The filter MUST strip non-allowed endpoints from the schema before the agent is
constructed. This means filtering:

1. **The DSL/schema text** passed to the LLM system prompt
2. **The `_raw_schema` ContextVar** used by `search_schema` — if raw schema is
   unfiltered, `search_schema` leaks blocked endpoints back to the LLM
3. **Tool construction** — don't create tools for methods outside the allowlist

This is architecturally different from the existing `X-Allow-Unsafe-Paths` which is
execution-time filtering (LLM sees the endpoint but the call is blocked).

### Protocol-Specific Filtering

| Protocol | Match Target | Examples |
|----------|-------------|----------|
| REST | `METHOD /path` | `GET /users/*`, `POST /orders` |
| GraphQL | `Type.field` | `Query.users`, `Mutation.createOrder` |
| gRPC | `package.Service/Method` | `helloworld.Greeter/*` |

### Interaction with Existing Headers

- **`X-Allow-Unsafe-Paths`**: Both must allow. An endpoint must pass the allowlist
  AND the unsafe-paths check for write operations. The allowlist controls visibility;
  unsafe-paths controls mutability.
- **`X-Poll-Paths`**: Polling only works on allowed endpoints (natural consequence
  of schema filtering).

### Recipe Filtering

Recipes associated with non-allowed endpoints:
- MUST NOT appear in recipe suggestions (`search_recipes`)
- MUST NOT be executable via recipe tools
- Recipes are keyed by `(api_id, schema_hash)` — the schema hash will change when
  the schema is filtered, so recipes naturally won't match. This may be sufficient
  without explicit recipe filtering logic.

## Resolved Questions

1. **Config format for multi-protocol**: Separate env vars per protocol.
   `API_AGENT_ALLOW_ENDPOINTS_REST`, `API_AGENT_ALLOW_ENDPOINTS_GRAPHQL`,
   `API_AGENT_ALLOW_ENDPOINTS_GRPC`. Clean separation, no parsing ambiguity.

2. **Schema hash implications**: Filtered schema = different hash = separate recipe
   cache. This is the desired behavior. Mixing filtered and unfiltered recipe caches
   is unwise. If a user needs both filtered and unfiltered access, they register two
   separate sessions — but there's no practical reason to want this.

3. **GraphQL type dependencies**: Allowing `Query.user` includes referenced types
   (`User`, `Address`) in the filtered schema so the LLM can build valid queries.
   However, `Query.user` does NOT imply `Query.address` is allowed — the allowlist
   controls root-level entry points, not transitive type reachability. Referenced
   types are included for schema completeness, not as additional query roots.

## Pre-Existing Security Context

The Security Engineer panelist flagged these existing concerns (not in scope for the
allowlist feature, but relevant context for design):

| Finding | Severity | Note |
|---------|----------|------|
| SSRF via `X-Target-URL` (no URL validation) | Critical | Allowlist partially mitigates |
| Recipe `{{param}}` string interpolation into DuckDB SQL | High | Orthogonal |
| No gRPC mutation blocking (unlike GraphQL/REST) | High | Allowlist partially mitigates |
| Global `RECIPE_STORE` cross-session | Medium | Allowlist partially mitigates |

## Skeptic's Challenge

> "Do we actually need this? If schema truncation were smarter (priority-ranked
> instead of alphabetical), would that solve the LLM confusion problem without
> the allowlist complexity?"

Panel conclusion: smart truncation helps but doesn't *restrict*. The allowlist is a
different primitive — it's about control, not presentation. Both could coexist.
However, if the primary motivation is LLM quality and not security, consider whether
smarter schema ranking is a cheaper first step.

## Recommended Next Steps

1. Write DESIGN.md with implementation plan
3. Implement in phases: config parsing -> schema filtering -> protocol agents -> recipes
4. Estimated complexity: 8 points (touches context.py, config.py, all 3 agents,
   schema_search, recipe search)
