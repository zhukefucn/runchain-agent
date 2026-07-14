"""Disposable, isolated FastAPI server used only by Playwright."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import stat
import sys

import uvicorn


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
E2E_ROOT = ROOT / ".run" / "e2e"


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = os.lstat(path).st_file_attributes
    except (FileNotFoundError, AttributeError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_safe_e2e_root(root: Path, e2e_root: Path) -> None:
    lexical_root = _absolute_without_resolving(root)
    expected_run_root = lexical_root / ".run"
    expected_e2e_root = expected_run_root / "e2e"
    lexical_e2e_root = _absolute_without_resolving(e2e_root)
    if lexical_e2e_root != expected_e2e_root:
        raise RuntimeError("refusing to reset a non-E2E path")
    for candidate in (expected_run_root, expected_e2e_root):
        if _is_reparse_point(candidate):
            raise RuntimeError(f"refusing to reset an E2E reparse point: {candidate}")


def main() -> None:
    """Reset only the dedicated E2E root, then run in the foreground."""
    _assert_safe_e2e_root(ROOT, E2E_ROOT)
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
