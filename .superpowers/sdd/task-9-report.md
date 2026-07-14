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
