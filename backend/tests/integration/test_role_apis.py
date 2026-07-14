from __future__ import annotations

import asyncio
import io
import json
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select
from starlette.requests import Request

from app.agents.sse import StableEvent
from app.auth.models import Principal
from app.config import Settings
from app.db.models import AuditRecordRow, MessageRow, Role, SessionRecordRow, User
from app.main import create_root_app
from app.skills.package import MAX_UPLOAD_BYTES


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'role-api.db'}",
        workspace_root=tmp_path / "workspace",
        skill_root=tmp_path / "skills",
        runner_root=tmp_path / "runner",
        jwt_secret_key="role-api-test-secret-with-enough-entropy",
        model_api_key="",
    )


@asynccontextmanager
async def _client(tmp_path: Path):
    app = create_root_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client, app


async def _auth(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/api/auth/login",
        json={"username": username, "password": "12345678"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _skill_zip() -> bytes:
    output = io.BytesIO()
    manifest = {
        "id": "api-demo",
        "name": "api-demo",
        "version": "1.0.0",
        "type": "python",
        "entrypoint": "main.py",
        "description": "API demo",
    }
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("api-demo/SKILL.md", "# API demo")
        archive.writestr("api-demo/skill.json", json.dumps(manifest))
        archive.writestr("api-demo/main.py", "print('ok')")
    return output.getvalue()


@pytest.mark.parametrize(
    ("path", "allowed", "forbidden"),
    [
        ("/api/manager/sessions", "manager0001", "business_admin01"),
        ("/api/business/skills", "business_admin01", "system_admin01"),
        ("/api/system/users", "system_admin01", "business_admin01"),
    ],
)
def test_public_role_matrix_is_exact_and_uses_unified_errors(
    tmp_path, path, allowed, forbidden
):
    async def scenario():
        async with _client(tmp_path) as (client, _app):
            missing = await client.get(path)
            assert missing.status_code == 401
            assert set(missing.json()) == {"code", "message", "request_id"}
            assert missing.headers["X-Request-ID"] == missing.json()["request_id"]

            denied = await client.get(path, headers=await _auth(client, forbidden))
            assert denied.status_code == 403
            assert denied.json()["code"] == "ROLE_FORBIDDEN"

            response = await client.get(path, headers=await _auth(client, allowed))
            assert response.status_code == 200

    asyncio.run(scenario())


def test_both_managers_are_isolated_and_client_cannot_supply_owner(tmp_path):
    async def scenario():
        async with _client(tmp_path) as (client, _app):
            one = await _auth(client, "manager0001")
            two = await _auth(client, "manager0002")

            injected = await client.post(
                "/api/manager/sessions",
                headers=one,
                json={
                    "title": "forged",
                    "agent_id": "reception-leader",
                    "owner_user_id": "manager0002",
                },
            )
            assert injected.status_code == 422

            created = await client.post(
                "/api/manager/sessions",
                headers=one,
                json={"title": "manager one", "agent_id": "reception-leader"},
            )
            assert created.status_code == 201
            session_id = created.json()["id"]
            assert "owner_user_id" not in created.json()

            own = await client.get("/api/manager/sessions", headers=one)
            other = await client.get(
                "/api/manager/sessions?owner_user_id=manager0001", headers=two
            )
            assert [item["id"] for item in own.json()["items"]] == [session_id]
            assert other.json()["items"] == []

            hidden = await client.get(
                f"/api/manager/sessions/{session_id}/messages", headers=two
            )
            assert hidden.status_code == 404
            assert set(hidden.json()) == {"code", "message", "request_id"}
            hidden_files = await client.get(
                f"/api/manager/files?session_id={session_id}", headers=two
            )
            assert hidden_files.status_code == 404

            second = await client.post(
                "/api/manager/sessions",
                headers=two,
                json={"title": "manager two", "agent_id": "reception-leader"},
            )
            assert second.status_code == 201

    asyncio.run(scenario())


def test_messages_get_does_not_audit_chat_but_missing_post_chat_does(tmp_path):
    async def scenario():
        async with _client(tmp_path) as (client, app):
            headers = await _auth(client, "manager0001")
            read = await client.get(
                "/api/manager/sessions/missing/messages", headers=headers
            )
            posted = await client.post(
                "/api/manager/sessions/missing/chat",
                headers=headers,
                json={"prompt": "missing"},
            )
            assert read.status_code == posted.status_code == 404
            async with app.state.session_factory() as db:
                read_audits = list(
                    await db.scalars(
                        select(AuditRecordRow).where(
                            AuditRecordRow.request_id == read.headers["X-Request-ID"]
                        )
                    )
                )
                post_audits = list(
                    await db.scalars(
                        select(AuditRecordRow).where(
                            AuditRecordRow.request_id == posted.headers["X-Request-ID"]
                        )
                    )
                )
            assert read_audits == []
            assert len(post_audits) == 1
            assert post_audits[0].action == "manager.chat.start"
            assert post_audits[0].result == "failure"

    asyncio.run(scenario())


def test_post_chat_message_creation_race_is_audited(monkeypatch, tmp_path):
    async def scenario():
        from app.repositories.manager import ManagerRepository

        async with _client(tmp_path) as (client, app):
            headers = await _auth(client, "manager0001")
            created = await client.post(
                "/api/manager/sessions",
                headers=headers,
                json={"title": "race", "agent_id": "reception-leader"},
            )

            async def disappeared(*_args, **_kwargs):
                return None

            monkeypatch.setattr(ManagerRepository, "create_message", disappeared)
            response = await client.post(
                f"/api/manager/sessions/{created.json()['id']}/chat",
                headers=headers,
                json={"prompt": "race"},
            )
            assert response.status_code == 404
            async with app.state.session_factory() as db:
                audits = list(
                    await db.scalars(
                        select(AuditRecordRow).where(
                            AuditRecordRow.request_id == response.headers["X-Request-ID"]
                        )
                    )
                )
            assert len(audits) == 1 and audits[0].result == "failure"

    asyncio.run(scenario())


def test_manager_stream_sanitizes_runtime_errors_and_closes_nested_generator(tmp_path):
    class BrokenRuntime:
        def __init__(self):
            self.closed = asyncio.Event()

        async def chat(self, _owner, session_id, _prompt, *, request_id):
            try:
                yield StableEvent(
                    type="run_started",
                    request_id=request_id,
                    session_id=session_id,
                    run_id="run-broken",
                )
                raise RuntimeError("secret provider traceback")
            finally:
                self.closed.set()

    async def scenario():
        async with _client(tmp_path) as (client, app):
            runtime = BrokenRuntime()
            app.state.reception_runtime = runtime
            headers = await _auth(client, "manager0001")
            created = await client.post(
                "/api/manager/sessions",
                headers=headers,
                json={"title": "broken", "agent_id": "reception-leader"},
            )
            response = await client.post(
                f"/api/manager/sessions/{created.json()['id']}/chat",
                headers=headers,
                json={"prompt": "fail safely"},
            )
            assert response.status_code == 200
            assert "event: error\n" in response.text
            assert "secret provider traceback" not in response.text
            assert runtime.closed.is_set()

    asyncio.run(scenario())


def test_manager_stream_close_is_repeat_cancel_safe(tmp_path):
    class BlockingRuntime:
        def __init__(self):
            self.finalizing = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = asyncio.Event()

        async def chat(self, _owner, session_id, _prompt, *, request_id):
            try:
                yield StableEvent(
                    type="run_started",
                    request_id=request_id,
                    session_id=session_id,
                    run_id="run-cancel",
                )
                await asyncio.Event().wait()
            finally:
                self.finalizing.set()
                await self.release.wait()
                self.closed.set()

    async def scenario():
        from app.api.manager import ChatRequest, chat

        async with _client(tmp_path) as (_client_instance, app):
            runtime = BlockingRuntime()
            app.state.reception_runtime = runtime
            async with app.state.session_factory() as db:
                manager = await db.scalar(
                    select(User).where(User.username == "manager0001")
                )
                session = SessionRecordRow(
                    owner_user_id=manager.id,
                    agent_id="reception-leader",
                    title="cancel",
                )
                db.add(session)
                await db.commit()
                await db.refresh(session)
                request = Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/api/manager/chat",
                        "headers": [],
                        "query_string": b"",
                        "app": app,
                        "state": {"request_id": "repeat-cancel"},
                    }
                )
                response = await chat(
                    session.id,
                    ChatRequest(prompt="cancel"),
                    request,
                    Principal(manager.id, Role.MANAGER, "bank_demo"),
                    db,
                )
                iterator = response.body_iterator
                first = await iterator.__anext__()
                assert "run_started" in first
                closing = asyncio.create_task(iterator.aclose())
                await runtime.finalizing.wait()
                closing.cancel()
                await asyncio.sleep(0)
                closing.cancel()
                assert not closing.done()
                runtime.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await closing
                assert runtime.closed.is_set()

    asyncio.run(scenario())


def test_manager_chat_sse_hitl_and_terminal_messages_are_persisted(tmp_path):
    async def scenario():
        async with _client(tmp_path) as (client, app):
            headers = await _auth(client, "manager0001")
            created = await client.post(
                "/api/manager/sessions",
                headers=headers,
                json={"title": "reception", "agent_id": "reception-leader"},
            )
            session_id = created.json()["id"]
            response = await client.post(
                f"/api/manager/sessions/{session_id}/chat",
                headers=headers,
                json={"prompt": "接待四位远方客人"},
            )
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert "event: run_started\n" in response.text
            assert "event: hitl_pending\n" in response.text
            payloads = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            assert all(
                {"type", "request_id", "session_id", "run_id", "timestamp", "data"}
                <= set(payload)
                for payload in payloads
            )
            pending = next(item for item in payloads if item["type"] == "hitl_pending")
            decision = await client.post(
                f"/api/manager/hitl/{pending['data']['request_id']}/decision",
                headers=headers,
                json={"decision": "approve"},
            )
            assert decision.status_code == 200
            assert decision.json()["status"] == "approved"
            assert [event["type"] for event in decision.json()["events"]] == [
                "token",
                "complete",
            ]

            messages = await client.get(
                f"/api/manager/sessions/{session_id}/messages", headers=headers
            )
            assert [item["role"] for item in messages.json()["items"]] == [
                "user",
                "assistant",
            ]
            async with app.state.session_factory() as db:
                stored = list(
                    await db.scalars(
                        select(MessageRow).where(MessageRow.session_id == session_id)
                    )
                )
            assert len(stored) == 2

    asyncio.run(scenario())


def test_business_skill_lifecycle_authorization_metadata_and_audit(tmp_path):
    async def scenario():
        async with _client(tmp_path) as (client, app):
            admin = await _auth(client, "business_admin01")
            manager = await _auth(client, "manager0001")
            async with app.state.session_factory() as db:
                target = await db.scalar(
                    select(User).where(User.username == "manager0001")
                )

            upload = await client.post(
                "/api/business/skills/upload",
                headers={**admin, "Content-Type": "application/zip"},
                content=_skill_zip(),
            )
            assert upload.status_code == 201
            skill_id = upload.json()["id"]
            assert "install_path" not in upload.json()
            assert "upload_sha256" not in upload.json()

            published = await client.post(
                f"/api/business/skills/{skill_id}/publish", headers=admin
            )
            assert published.status_code == 200
            authorized = await client.post(
                f"/api/business/skills/{skill_id}/authorizations",
                headers=admin,
                json={"manager_user_id": target.id},
            )
            assert authorized.status_code == 200

            visible = await client.get("/api/manager/skills", headers=manager)
            assert [item["id"] for item in visible.json()["items"]] == [skill_id]

            invocations = await client.get(
                "/api/business/skill-invocations", headers=admin
            )
            assert invocations.status_code == 200
            assert "input_data" not in json.dumps(invocations.json())
            assert "output_data" not in json.dumps(invocations.json())

            disabled = await client.post(
                f"/api/business/skills/{skill_id}/disable", headers=admin
            )
            assert disabled.status_code == 200
            async with app.state.session_factory() as db:
                actions = set(await db.scalars(select(AuditRecordRow.action)))
            assert {"skill.install", "skill.publish", "skill.authorize", "skill.disable"} <= actions

    asyncio.run(scenario())


def test_business_skill_upload_rejects_oversized_body_before_materializing_it(tmp_path):
    async def scenario():
        async with _client(tmp_path) as (client, app):
            admin = await _auth(client, "business_admin01")
            response = await client.post(
                "/api/business/skills/upload",
                headers={
                    **admin,
                    "Content-Type": "application/zip",
                    "Content-Length": str(MAX_UPLOAD_BYTES + 1),
                },
                content=b"not-read-as-a-zip",
            )
            assert response.status_code == 413
            assert response.json()["code"] == "UPLOAD_TOO_LARGE"
            async with app.state.session_factory() as db:
                audit = await db.scalar(
                    select(AuditRecordRow).where(
                        AuditRecordRow.request_id == response.headers["X-Request-ID"]
                    )
                )
            assert audit is not None and audit.result == "failure"

    asyncio.run(scenario())


def test_failed_and_idempotent_skill_mutation_attempts_keep_http_request_ids(tmp_path):
    async def scenario():
        async with _client(tmp_path) as (client, app):
            admin = await _auth(client, "business_admin01")
            invalid = await client.post(
                "/api/business/skills/upload",
                headers={**admin, "Content-Type": "application/zip"},
                content=b"invalid",
            )
            assert invalid.status_code == 409
            package = _skill_zip()
            first = await client.post(
                "/api/business/skills/upload",
                headers={**admin, "Content-Type": "application/zip"},
                content=package,
            )
            second = await client.post(
                "/api/business/skills/upload",
                headers={**admin, "Content-Type": "application/zip"},
                content=package,
            )
            assert first.status_code == second.status_code == 201
            expected_ids = {
                invalid.headers["X-Request-ID"],
                first.headers["X-Request-ID"],
                second.headers["X-Request-ID"],
            }
            async with app.state.session_factory() as db:
                rows = list(
                    await db.scalars(
                        select(AuditRecordRow).where(
                            AuditRecordRow.action == "skill.install"
                        )
                    )
                )
            assert {row.request_id for row in rows} == expected_ids
            assert {row.result for row in rows} == {"success", "failure"}
            repeated = next(
                row
                for row in rows
                if row.request_id == second.headers["X-Request-ID"]
            )
            assert repeated.details["status"] == "idempotent"

    asyncio.run(scenario())


def test_missing_mcp_test_is_404_and_failure_is_audited(tmp_path):
    async def scenario():
        async with _client(tmp_path) as (client, app):
            admin = await _auth(client, "business_admin01")
            response = await client.post(
                "/api/business/mcp-servers/missing/test", headers=admin
            )
            assert response.status_code == 404
            async with app.state.session_factory() as db:
                row = await db.scalar(
                    select(AuditRecordRow).where(
                        AuditRecordRow.request_id == response.headers["X-Request-ID"]
                    )
                )
            assert row is not None and row.result == "failure"

    asyncio.run(scenario())


def test_mcp_test_all_start_failures_have_exactly_one_request_audit(tmp_path):
    from app.mcp.service import (
        McpConflictError,
        McpNotFoundError,
        McpUnavailableError,
    )

    class FailingMcp:
        error: Exception

        async def start(self, _actor, _server_id, request_id=None):
            raise self.error

    async def scenario():
        async with _client(tmp_path) as (client, app):
            headers = await _auth(client, "business_admin01")
            fake = FailingMcp()
            app.state.mcp_service = fake
            cases = [
                (McpConflictError("key conflict"), 409),
                (McpUnavailableError("not ready"), 503),
                (McpNotFoundError("missing"), 404),
            ]
            for error, status_code in cases:
                fake.error = error
                response = await client.post(
                    "/api/business/mcp-servers/server/test", headers=headers
                )
                assert response.status_code == status_code
                async with app.state.session_factory() as db:
                    audits = list(
                        await db.scalars(
                            select(AuditRecordRow).where(
                                AuditRecordRow.request_id
                                == response.headers["X-Request-ID"],
                                AuditRecordRow.result == "failure",
                            )
                        )
                    )
                assert len(audits) == 1
                assert "key conflict" not in json.dumps(audits[0].details)

    asyncio.run(scenario())


def test_business_mcp_endpoints_are_local_metadata_and_delegate_test(tmp_path):
    class FakeMcpService:
        def __init__(self):
            self.calls = []

        async def register_local(self, actor, **configuration):
            self.calls.append(("register", actor, configuration))
            return SimpleNamespace(
                id="mcp-1",
                name=configuration["name"],
                transport="stdio",
                status="stopped",
                last_error=None,
                created_at=None,
            )

        async def start(self, actor, server_id, request_id=None):
            self.calls.append(("start", actor, server_id))

        async def health(self, actor, server_id, request_id=None):
            self.calls.append(("health", actor, server_id))
            return True

        async def list_tools(self, actor, server_id, request_id=None):
            self.calls.append(("tools", actor, server_id))
            return [{"name": "pickup", "description": "mock", "input_schema": {}}]

    async def scenario():
        async with _client(tmp_path) as (client, app):
            fake = FakeMcpService()
            app.state.mcp_service = fake
            headers = await _auth(client, "business_admin01")
            created = await client.post(
                "/api/business/mcp-servers",
                headers=headers,
                json={
                    "name": "pickup-api",
                    "command": str(Path(__file__).resolve()),
                    "args": ["mock_pickup_server.py"],
                    "env": {},
                },
            )
            assert created.status_code == 201
            tested = await client.post(
                "/api/business/mcp-servers/mcp-1/test", headers=headers
            )
            assert tested.status_code == 200
            assert tested.json() == {
                "server_id": "mcp-1",
                "healthy": True,
                "tools": [
                    {"name": "pickup", "description": "mock", "input_schema": {}}
                ],
            }
            assert [call[0] for call in fake.calls] == [
                "register",
                "start",
                "health",
                "tools",
            ]

    asyncio.run(scenario())


def test_system_user_model_and_audit_apis_never_return_secrets(tmp_path):
    async def scenario():
        async with _client(tmp_path) as (client, app):
            headers = await _auth(client, "system_admin01")
            created = await client.post(
                "/api/system/users",
                headers=headers,
                json={
                    "username": "manager0003",
                    "password": "another-password",
                    "role": "manager",
                    "is_active": True,
                },
            )
            assert created.status_code == 201
            user_id = created.json()["id"]
            assert "password" not in json.dumps(created.json()).lower()

            patched = await client.patch(
                f"/api/system/users/{user_id}",
                headers=headers,
                json={"is_active": False},
            )
            assert patched.status_code == 200
            assert patched.json()["is_active"] is False

            users = await client.get("/api/system/users?limit=2", headers=headers)
            assert users.status_code == 200
            assert len(users.json()["items"]) == 2
            assert "password" not in json.dumps(users.json()).lower()

            model = await client.get("/api/system/model/status", headers=headers)
            assert model.status_code == 200
            assert set(model.json()) == {
                "configured",
                "model",
                "base_url",
                "connectivity",
            }
            serialized = json.dumps(model.json())
            assert "api_key" not in serialized
            assert "secret" not in serialized.lower()

            logs = await client.get("/api/system/audit-logs", headers=headers)
            assert logs.status_code == 200
            assert {item["action"] for item in logs.json()["items"]} >= {
                "system.user.create",
                "system.user.patch",
            }
            assert "details" in logs.json()["items"][0]
            assert "password" not in json.dumps(logs.json()).lower()

            async with app.state.session_factory() as db:
                row = await db.get(User, user_id)
                assert row.password_hash != "another-password"

    asyncio.run(scenario())


def test_system_model_status_sanitizes_url_and_failed_users_are_audited(tmp_path):
    async def scenario():
        settings = _settings(tmp_path).model_copy(
            update={
                "model_base_url": "https://safe.example/v1?api_key=query-secret#fragment"
            }
        )
        app = create_root_app(settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                headers = await _auth(client, "system_admin01")
                model = await client.get("/api/system/model/status", headers=headers)
                assert "query-secret" not in json.dumps(model.json())
                assert model.json()["base_url"] == "https://safe.example/v1"
                assert model.json()["connectivity"] in {
                    "not_checked",
                    "not_configured",
                }
                app.state.model_connectivity = "reachable"
                observed = await client.get(
                    "/api/system/model/status", headers=headers
                )
                assert observed.json()["connectivity"] == "reachable"
                duplicate = await client.post(
                    "/api/system/users",
                    headers=headers,
                    json={
                        "username": "manager0001",
                        "password": "duplicate-password",
                        "role": "manager",
                    },
                )
                missing = await client.patch(
                    "/api/system/users/missing",
                    headers=headers,
                    json={"is_active": False},
                )
                assert duplicate.status_code == 409
                assert missing.status_code == 404
                async with app.state.session_factory() as db:
                    request_ids = set(
                        await db.scalars(
                            select(AuditRecordRow.request_id).where(
                                AuditRecordRow.result == "failure"
                            )
                        )
                    )
                assert {
                    duplicate.headers["X-Request-ID"],
                    missing.headers["X-Request-ID"],
                } <= request_ids

    asyncio.run(scenario())
