"""Disposable, isolated FastAPI server used only by Playwright."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import sys

import uvicorn


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
E2E_ROOT = (ROOT / ".run" / "e2e").resolve()


def main() -> None:
    """Reset only the dedicated E2E root, then run in the foreground."""
    run_root = (ROOT / ".run").resolve()
    if E2E_ROOT.parent != run_root or E2E_ROOT.name != "e2e":
        raise RuntimeError("refusing to reset a non-E2E path")
    if E2E_ROOT.exists():
        shutil.rmtree(E2E_ROOT)
    E2E_ROOT.mkdir(parents=True)

    # Import-time compatibility for the legacy module-level session factory.
    os.environ["APP_ENV"] = "test"
    os.environ["JWT_SECRET_KEY"] = (
        "playwright-only-jwt-secret-at-least-32-chars"
    )
    os.environ["DATABASE_URL"] = (
        f"sqlite+aiosqlite:///{E2E_ROOT / 'demo.db'}"
    )
    sys.path.insert(0, str(BACKEND))
    from app.config import Settings
    from app.main import create_root_app

    settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{E2E_ROOT / 'demo.db'}",
        workspace_root=E2E_ROOT / "workspace",
        skill_root=E2E_ROOT / "skills",
        runner_root=E2E_ROOT / "runner",
        mcp_root=ROOT / "backend" / "app" / "mcp",
        frontend_dist=ROOT / "frontend" / "dist",
        jwt_secret_key="playwright-only-jwt-secret-at-least-32-chars",
    )
    uvicorn.run(
        create_root_app(settings),
        host="127.0.0.1",
        port=18080,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
