# Task 9 Implementation Report

## Scope

Implemented governed local stdio MCP registration, authorization, lifecycle,
discovery, health metadata, bounded calls, cleanup, and the deterministic mock
pickup planner.

## Design and security decisions

- Uses the installed official `mcp==1.28.1` SDK (`stdio_client`,
  `ClientSession`, `initialize`, `list_tools`, and `call_tool`). The SDK's
  Windows transport uses pywin32 Job Objects for process-tree termination.
- MCP context-manager entry and exit stay in one lifecycle task. Timeout,
  protocol failure, crash, stop, restart, and application shutdown all retire
  that task and its subprocess tree.
- A shared `McpRuntimeRegistry` makes concurrent starts idempotent across
  request-scoped service objects and shares the global call semaphore.
- No database transaction spans an MCP/process await. An injected
  `async_sessionmaker` creates independent short sessions; state changes and
  their audit record commit atomically in one session.
- Registration accepts structured command/args only: the canonical current
  virtual-environment Python, one explicitly allowlisted checked-in server
  script, no shell, no custom environment, no URL transport, no reparse point,
  and persisted executable/script hashes rechecked before every start.
- `business_admin` and `system_admin` govern servers. Managers can only call a
  server after a unique authorization to an active manager account.
- Tool calls require a discovered tool, bounded JSON input, advertised-schema
  validation with undeclared-field rejection, timeout, concurrency limit,
  structured JSON output, and output-size bound.
- Audit details are fixed metadata only; tool names, arguments, output,
  configuration values, environment values, secrets, and tracebacks are not
  recorded.

## Data model

- Replaced business ownership on global `McpServerRow` with
  `created_by_user_id`, lifecycle state, safe error code, configuration, and
  integrity hashes.
- Added `McpAuthorizationRow` / `mcp_tool_authorizations` with unique
  `(server_id, user_id)`.

## Mock server

`plan_pickup` validates a timezone-aware ISO timestamp, a fixed mock station
set, and `guest_count` 1..50, then returns a UTF-8 deterministic structured
plan containing `mock`, vehicle, driver, meeting point, and timeline fields.
No network, randomness, or model call is used.

## Verification

- Task-specific: `22 passed`
- Full suite: `245 passed, 2 skipped`
- `python -m compileall -q backend`: passed
- `python -m pip check`: no broken requirements
- `git diff --check`: passed (only Git's normal Windows LF/CRLF notices)

The eight warnings in the full suite are pre-existing AgentScope storage
deprecation warnings outside Task 9.

## Review remediation

The post-implementation review identified lifecycle ownership and generation
races. The corrected architecture now has an explicitly injected,
application-scoped `McpRuntimeRegistry` with a fixed namespace, canonical
root/Python fingerprint, shared call limit, hard running-server limit, and a
composite `(namespace, server_id, config_fingerprint)` runtime identity.
Request-scoped `McpService.aclose()` never stops shared processes; only registry
`shutdown_all()` / `aclose()` owns application process teardown.

Every start re-reads and integrity-checks the database before consulting the
registry. Spawn slots are reserved atomically before process creation and are
released by compare-and-remove on start failure, stop, timeout, cancellation,
protocol failure, idle crash, or shutdown. Runtime generations prevent stale
calls and late task exits from overwriting a newer running/stopped state.
Lifecycle exit notification uses the registry-owned session factory and writes
failed state plus audit in one short transaction. A heartbeat detects an idle
stdio subprocess exit even when no request is active.

Business tool calls are now restricted to active, explicitly authorized
managers; global administrators only govern and probe servers. `call_tool` has
one audit exit path, including denied, unavailable, validation, SDK, timeout,
protocol, output-bound, and cancelled paths. Cancellation releases the shared
semaphore, compare-retires the uncertain MCP session, completes one shielded
short audit task, and then propagates `CancelledError`.

Production `mock_pickup_server.py` contains no crash or sleep test hooks. Fault
injection lives only under `backend/tests/fixtures/`. Arrival timestamps are
parsed with `datetime.fromisoformat` and must be valid and timezone-aware. Raw
path components are checked for symlinks/reparse points before resolution and
containment/hash validation.

Review-remediation verification:

- Task-specific plus schema/setup contracts: `30 passed`
- Full suite: `253 passed, 2 skipped`
- `python -m compileall -q backend`: passed
- `python -m pip check`: no broken requirements
- `git diff --check`: passed (normal Windows LF/CRLF notices only)

### Terminal close and full-stage cancellation follow-up

Registry shutdown is now terminal. Under the registry global lock, the first
`shutdown_all()`/`aclose()` sets `closing` and creates one shared close task;
all concurrent close callers shield-wait that same task. `reserve()` rejects
both closing and closed registries, including the race where start has finished
database/hash validation but has not reserved a slot. The close task atomically
takes all reserved/current runtimes, waits for process and notification cleanup,
then marks the registry closed. Later starts remain rejected; a new application
must create a new registry. Running-slot usage is therefore zero at terminal
completion. Per-server locks are intentionally retained for the small Phase 1
database-bounded server set so lock identity cannot change during a generation.

Cancellation is handled by the outermost `call_tool` scope, including database
authorization, runtime lookup, JSON/schema validation, semaphore acquisition,
SDK request, and output parsing. Every cancellation writes exactly one
`cancelled` metadata audit through the uncancellable short audit task. The
shared runtime is compare-retired only after the protocol request was actually
started; cancelling while waiting for authorization or a call slot neither
kills the shared runtime nor leaks a semaphore slot. Repeated cancellation is
deferred until the single audit task completes. Input JSON encoding catches are
local to `json.dumps`; SDK/schema `TypeError`/`ValueError` are no longer
misreported as user input errors.

Final follow-up verification:

- MCP integration tests: `24 passed`
- Task-specific plus schema/setup contracts: `34 passed`
- Full suite: `257 passed, 2 skipped`
- `python -m compileall -q backend`: passed
- `python -m pip check`: no broken requirements
- `git diff --check`: passed (normal Windows LF/CRLF notices only)
