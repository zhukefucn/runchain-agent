import asyncio
from pathlib import Path

import httpx
from app.config import Settings
from app.main import create_root_app


def _settings(frontend_dist: Path) -> Settings:
    return Settings(
        app_env="test",
        jwt_secret_key="test-secret-key-with-32-characters",
        database_url="sqlite+aiosqlite:///:memory:",
        frontend_dist=frontend_dist,
    )


def test_built_frontend_is_served_without_shadowing_api_routes(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<main>RunChain UI</main>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("window.runchain=true", encoding="utf-8")
    app = create_root_app(_settings(dist))

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return (
                await client.get("/"),
                await client.get("/manager/session/example"),
                await client.get("/assets/app.js"),
                await client.get("/api/not-a-route"),
                await client.get("/internal/agentscope/not-a-route"),
            )

    home, spa, asset, missing_api, protected_internal = asyncio.run(exercise())

    assert home.status_code == 200
    assert home.text == "<main>RunChain UI</main>"
    assert spa.status_code == 200
    assert spa.text == home.text
    assert asset.status_code == 200
    assert asset.text == "window.runchain=true"
    assert missing_api.status_code == 404
    assert missing_api.headers["content-type"].startswith("application/json")
    assert protected_internal.status_code == 401


def test_missing_frontend_build_returns_clear_503(tmp_path: Path) -> None:
    app = create_root_app(_settings(tmp_path / "missing"))
    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.get("/")

    response = asyncio.run(exercise())

    assert response.status_code == 503
    assert response.json()["code"] == "FRONTEND_NOT_BUILT"
