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

from app.config import Settings
from app.db.models import AuditRecordRow, MessageRow, Role, User
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

            second = await client.post(
                "/api/manager/sessions",
                headers=two,
                json={"title": "manager two", "agent_id": "reception-leader"},
            )
            assert second.status_code == 201

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
        async with _client(tmp_path) as (client, _app):
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

        async def start(self, actor, server_id):
            self.calls.append(("start", actor, server_id))

        async def health(self, actor, server_id):
            self.calls.append(("health", actor, server_id))
            return True

        async def list_tools(self, actor, server_id):
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
