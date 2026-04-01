# Feature Specification: Recipe Persistence

**Feature Branch**: `001-recipe-persistence`
**Created**: 2026-03-20
**Status**: Draft
**Input**: Pluggable recipe persistence with SQLiteBackend default. Persists learned API call + SQL pipeline recipes across process restarts.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Recipes Survive Restarts (Priority: P1)

A deployment operator restarts the ratatoskr server (e.g., rolling deployment, pod restart, process crash). Previously learned recipes — parameterized API call + SQL pipelines — are automatically restored on startup. MCP clients see the same recipe tools as before the restart without any user action.

**Why this priority**: This is the core value proposition. Without it, every restart forces the LLM to re-discover patterns through expensive reasoning, wasting tokens and time.

**Independent Test**: Start server, trigger recipe extraction via a query, restart server, verify recipe tools are available without re-querying.

**Acceptance Scenarios**:

1. **Given** a server with 3 learned recipes, **When** the process is restarted, **Then** all 3 recipes are available as MCP tools within 2 seconds of startup.
2. **Given** a server with persisted recipes, **When** the server starts, **Then** recipe tools are registered before the first client connection is accepted.
3. **Given** a server with no previous recipes (fresh install), **When** the server starts, **Then** startup completes normally with zero recipes loaded and no errors.

---

### User Story 2 - Zero-Config Default (Priority: P1)

A developer installs ratatoskr from PyPI (`pip install api-agent-ratatoskr`) and runs it. Recipe persistence works automatically without configuring any external infrastructure, databases, or env vars. The system chooses a sensible storage location and just works.

**Why this priority**: Open-source users expect zero-friction setup. Requiring external config would be a barrier to adoption.

**Independent Test**: Install from PyPI, run server, generate a recipe, restart, verify recipe persisted — with zero configuration.

**Acceptance Scenarios**:

1. **Given** a fresh installation with no configuration, **When** a recipe is learned, **Then** it is persisted to a local file automatically.
2. **Given** a fresh installation, **When** the server starts, **Then** the storage location is created automatically if it doesn't exist.
3. **Given** a containerized deployment with a read-only root filesystem, **When** the storage location is not writable, **Then** the server starts normally but logs a warning and falls back to in-memory-only behavior.

---

### User Story 3 - Schema Change Invalidation (Priority: P2)

When a target API's schema changes (e.g., new version deployed), previously learned recipes may reference stale endpoints or types. The system automatically detects schema changes and invalidates affected recipes, preventing stale recipe execution.

**Why this priority**: Stale recipes that reference removed fields would produce errors. Automatic invalidation ensures recipes are always consistent with the current schema.

**Independent Test**: Persist a recipe, change the schema hash (simulating API schema change), verify recipe is purged on next interaction.

**Acceptance Scenarios**:

1. **Given** persisted recipes for API "example" with schema hash "abc123", **When** the schema changes to hash "def456", **Then** all recipes for "example/abc123" are removed from both memory and persistent storage.
2. **Given** recipes for two different APIs, **When** one API's schema changes, **Then** only that API's recipes are invalidated; the other API's recipes remain intact.

---

### User Story 4 - Opt-Out to Memory-Only (Priority: P3)

An operator running ratatoskr in a stateless environment (e.g., serverless, ephemeral containers) explicitly configures the system to skip persistence and use in-memory-only storage, matching the current behavior.

**Why this priority**: Not all deployments benefit from persistence. Operators should be able to opt out without side effects.

**Independent Test**: Set config to memory-only, verify no files are created on disk, verify recipes work normally within a session.

**Acceptance Scenarios**:

1. **Given** persistence configured as "memory", **When** recipes are learned, **Then** no files are created on disk.
2. **Given** persistence configured as "memory", **When** the server restarts, **Then** no recipes are loaded (clean slate).

---

### Edge Cases

- What happens when the storage file is corrupted or contains invalid data? System should log a warning and start with an empty recipe cache (graceful degradation).
- What happens when two server processes write to the same storage file concurrently? Storage mechanism must handle concurrent access safely.
- What happens when the storage location runs out of disk space? Write failures should be logged but never crash the server or block recipe execution.
- What happens when a persisted recipe's data is malformed? Individual malformed recipes should be skipped with a warning; valid recipes should still load.
- What happens when the number of persisted recipes exceeds the configured cache size? Only the most recently used recipes (up to cache size limit) should be loaded.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST persist learned recipes to local storage automatically when a new recipe is extracted.
- **FR-002**: System MUST restore persisted recipes on startup before accepting client connections.
- **FR-003**: System MUST register restored recipes as MCP tools (via tool list notification) before the first client connection.
- **FR-004**: System MUST invalidate persisted recipes when the associated API schema changes (detected via schema hash mismatch).
- **FR-005**: System MUST work with zero configuration using sensible defaults for storage location.
- **FR-006**: System MUST support configurable storage backends ("memory" for no persistence, "sqlite" for local file persistence).
- **FR-007**: System MUST NOT block recipe execution or agent operations if persistence writes fail (fire-and-forget semantics).
- **FR-008**: System MUST validate recipe data integrity when loading from persistent storage, skipping corrupted entries with a warning.
- **FR-009**: System MUST handle concurrent access to persistent storage safely (multiple agent completions writing simultaneously).
- **FR-010**: System MUST fall back gracefully to memory-only behavior if the configured storage location is not writable, logging a warning.
- **FR-011**: System MUST respect the configured cache size limit when loading persisted recipes (load most recent, discard oldest).

### Key Entities

- **Recipe**: A parameterized API call + SQL pipeline extracted from a successful agent run. Contains query template, SQL template, parameter definitions, and metadata (api_id, schema_hash, creation time).
- **RecipeBackend**: The persistence interface. Implementations handle storage mechanics while the recipe store handles caching and MCP tool registration.
- **RecipeStore**: The existing in-memory LRU cache, extended with write-through persistence. Remains the fast-path for all lookups.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Recipes survive a server restart — 100% of persisted recipes are available as MCP tools within 2 seconds of startup.
- **SC-002**: Zero-config experience — a fresh `pip install` + server start persists recipes without any environment variables or configuration files.
- **SC-003**: No performance regression — recipe lookup latency remains unchanged (in-memory cache is still the hot path; persistence is write-through only).
- **SC-004**: Graceful degradation — server starts and operates normally even when persistent storage is unavailable or corrupted.
- **SC-005**: All existing tests continue to pass with no modifications (backward compatibility).
- **SC-006**: Schema invalidation removes 100% of stale recipes within one request cycle of detecting a schema change.

## Assumptions

- The default persistence backend uses a built-in local storage mechanism (no external infrastructure required).
- The storage location follows platform-standard conventions for cache data.
- Recipe serialization uses a safe, standard data format (no executable code in persisted data).
- A network-accessible backend (e.g., Redis) is intentionally deferred until user demand materializes.
- Eager startup load is a correctness requirement, not just a performance optimization (tool registration timing).
- The existing recipe store's in-memory cache and locking behavior remain unchanged.

## Dependencies

- Existing recipe store with in-memory LRU caching and thread-safe operations.
- Existing MCP tool list notification mechanism for client awareness.
- No new external dependencies required (uses platform built-ins only).
