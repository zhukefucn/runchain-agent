# Task 5 Report: AgentScope-Compatible SQLite Storage

## Result

Implemented `SQLiteStorage(StorageBase)` against the checked-in AgentScope source. The class is concrete and implements all 41 current abstract methods with the same parameter names and defaults.

Sessions and messages use the existing canonical `sessions` and `messages` tables, so `ManagerRepository` and AgentScope immediately see each other's writes without duplicate records. The remaining AgentScope record kinds use `agentscope_storage_records`; its primary key is `(owner_user_id, namespace, record_id)`, its owner is an indexed FK to `users.id` with `ON DELETE CASCADE`, and generic writes use SQLite `INSERT .. ON CONFLICT DO UPDATE`.

Session identity is `(owner_user_id, id)`, and every session child uses a composite owner/session FK. The same logical session ID can coexist for both managers while inconsistent parent/child owners are rejected by SQLite. Knowledge documents encode both knowledge-base and document identity.

## Contract source reviewed

- `StorageBase`: all 41 abstract methods and docstrings.
- `RedisStorage`: credentials, agent/session cascades, schedule restore, message last-id replacement, team role-aware cascade, knowledge-base/document lifecycle, and lease behavior.
- Storage Pydantic models: `CredentialRecord`, `AgentRecord`, `SessionRecord`, `ScheduleRecord`, `TeamRecord`, `KnowledgeBaseRecord`, and `KnowledgeDocumentRecord`.
- `_dump_with_secrets`: used directly for recoverable credential persistence. Tests use only a synthetic secret and never print it.

## Interfaces implemented

- Credentials: `upsert/list/get/delete_credential`
- Agents: `upsert/list/get/delete_agent`
- Sessions: `upsert`, `set_session_team_id`, `update_session_state`, `list/get/delete`, `list_sessions_by_schedule`
- Schedules: `upsert/get/list/delete`, `list_all_schedules`
- Messages: `upsert/get/list_messages`
- Teams: `upsert/get/list/delete_team`
- Knowledge bases: `upsert/get/list/delete_knowledge_base`
- Knowledge documents: `upsert/get/list/delete`, status update, acquire/renew/release lease, expired-lease scan, pending-since scan

`list_all_schedules` is documented in code as system-startup restore only. The two global knowledge-document scans are also required by the checked-in `StorageBase`; they are documented as internal index-worker recovery operations and are not exposed as business reads.

## TDD evidence

1. Initial concrete contract RED: test collection failed because `app.agentscope_ext` did not exist.
2. Dependency contract RED: AgentScope app imports exposed the two missing upstream `service` extra dependencies.
3. Concrete skeleton GREEN: all 41 method names made `__abstractmethods__` empty; setup/concrete contracts passed.
4. Group contracts added for credentials/agents, sessions/messages/schedules, teams, and KB/document leases.
5. Boundary RED: the first generic document key collided when one owner reused the same document ID across two KBs. The key was corrected to include `(owner, knowledge_base_id, document_id)`, and the three-way owner/KB isolation case passed.
6. Lease acquisition uses `BEGIN IMMEDIATE` so SQLite serializes compare-and-swap writers; a concurrent two-worker test proves exactly one acquisition succeeds.
7. Review RED cases covered cross-owner Team payload injection, session-id rebind, missing generic owner FK, concurrent generic upserts, concurrent message ordinal collisions, and the previous split-brain between repository and adapter tables.
8. Review GREEN behavior now includes:
   - Team member owners are validated at write time; cascade operations always use the trusted method `user_id`.
   - Existing session IDs preserve their original agent exactly like `RedisStorage`; get/update/delete remain keyed by owner/session even if a different agent argument is supplied.
   - Generic upserts are atomic; message append/last-id replacement is serialized with `BEGIN IMMEDIATE` and has unique owner/session ordinals.
   - Status updates re-read and mutate under `BEGIN IMMEDIATE`, preserving concurrent lease owner and deadline fields.
   - Adapter/repository session and message writes are visible in both directions, and a test asserts no generic `session`/`message` duplicates exist.
   - Generic owner FK, illegal-owner rejection, user cascade, logical document-parent validation, and KB-to-document cascade are tested.
9. Second-review RED cases reproduced mixed Repository/adapter message ordinal collisions, Repository deletion bypassing Team cascade, and a KB-delete/document-register orphan race.
10. Second-review GREEN behavior now includes:
   - `ManagerRepository.create_message` begins `BEGIN IMMEDIATE` from a clean boundary, then performs owner/session validation, MAX ordinal allocation, insert, and commit in one transaction. Twelve mixed adapter/Repository concurrent writes complete with unique ordinals.
   - `ManagerRepository` accepts an injected `SessionStorage` protocol and delegates Team-associated session deletion to the one canonical role-aware storage cascade. Without injection it fails closed. Tests cover leader deletion, created-agent deletion, invited-agent preservation/session detach, schedule-session cleanup, and cross-owner refusal.
   - Generic `_put_with_session` supports caller-owned transactions. Knowledge-document registration performs parent check and upsert in one `BEGIN IMMEDIATE`; KB deletion removes all children and the parent in one `BEGIN IMMEDIATE`. Repeated two-connection event-gated races cannot create an orphan.
11. Third-review RED cases reproduced Repository write methods committing or rolling back their caller's active transaction, and exercised ordinary session deletion racing both adapter and Repository message writers.
12. Third-review GREEN behavior now includes:
   - `ManagerRepository` accepts an explicit `write_session_factory`. `create_message` and `delete_session` use only a short-lived independent writer session and never inspect, commit, or roll back the caller's `self._db` transaction. Without the factory both write entrypoints fail closed.
   - A flushed, uncommitted caller title update remains invisible to another connection while the independent message writer waits for SQLite's lock; after caller rollback the message write completes and the title update is absent.
   - Message creation executes `BEGIN IMMEDIATE`, owner/session validation, ordinal allocation, insert, and commit in the same writer transaction.
   - Ordinary deletion executes `BEGIN IMMEDIATE`, reads owner/session/team from that transaction's snapshot, deletes and commits there. Team deletion rolls that writer transaction back to release the lock before delegating to the injected canonical `SessionStorage`.
   - Event-gated ordinary deletion races against both adapter and Repository message writers produce only serialized outcomes: deletion cascades any earlier message, while a later write is rejected. No orphan messages or `SQLITE_BUSY` errors remain.

## Dependency closure

The checked-in AgentScope `service` extra declares FastAPI, Uvicorn, APScheduler, and `ag-ui-protocol>=0.1.10`. Added or confirmed exact direct pins required by the demo:

- `APScheduler==3.11.3`
- `ag-ui-protocol==0.1.19`
- `uvicorn==0.51.0`

The resolved lock also records `tzlocal==5.4.4` and `tzdata==2026.3`. `pip check` reports no broken requirements. Redis was not added.

## Verification

- Storage contract: `13 passed`
- Storage/schema/security/setup affected suite: `50 passed`
- Full demo suite: `78 passed`
- `python -m compileall -q backend/app backend/tests`: passed
- `python -m pip check`: no broken requirements
- `git diff --check`: passed

The warnings in the team contract are expected AgentScope deprecation warnings produced while explicitly exercising the required legacy `member_ids` migration path.

## Credential-at-rest limitation

Credential values must remain recoverable for AgentScope model construction, so the adapter deliberately uses AgentScope `_dump_with_secrets`. In Phase 1 SQLite this means credential secrets are stored in plaintext inside the local database file. The database and its backups must be restricted to the demo OS account, must not be committed or copied into logs/test artifacts, and should be deleted when the test environment is retired. API responses and logs must never emit the stored payload. Production deployment requires encrypted secret storage or application-level encryption; this task does not claim SQLite-file encryption.

## Upstream test limitation

Command attempted:

`python -m pytest ..\agentscope-main\tests\service_team_tools_test.py -q`

Collection stops at `ModuleNotFoundError: fakeredis`. That upstream test is written around its Redis/fakeredis fixture and has no SQLite adapter fixture. Per the Phase 1 no-Redis scope, neither fakeredis nor Redis runtime dependencies were added and no upstream code/test was changed. This is a fixture dependency limitation; no adapter product-code failure was observed.
