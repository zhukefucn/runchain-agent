from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text


router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health() -> dict[str, str]:
    """Process liveness only; it deliberately performs no dependency I/O."""
    return {"status": "ok"}


def _safe_component(ok: bool, detail: str) -> dict[str, str]:
    return {"status": "ok" if ok else "failed", "detail": detail}


@router.get("/api/ready")
async def ready(request: Request):
    """Check local dependencies without contacting the external model API."""
    components: dict[str, dict[str, str]] = {}
    try:
        async with request.app.state.session_factory() as db:
            await db.execute(text("SELECT 1"))
        components["database"] = _safe_component(True, "query succeeded")
    except Exception:
        components["database"] = _safe_component(False, "query failed")

    for key, root in (
        ("workspace", request.app.state.settings.workspace_root),
        ("runner", request.app.state.settings.runner_root),
    ):
        try:
            path = Path(root)
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".ready-probe"
            probe.write_bytes(b"ok")
            probe.unlink()
            components[key] = _safe_component(True, "local root writable")
        except OSError:
            components[key] = _safe_component(False, "local root unavailable")

    settings = request.app.state.settings
    model_ok = bool(settings.model_name) and (
        settings.app_env == "test" or bool(settings.model_api_key.get_secret_value())
    )
    components["model"] = _safe_component(
        model_ok,
        "fake model configured" if settings.app_env == "test" else "real model configured",
    )
    components["agentscope"] = _safe_component(
        request.app.state.agentscope_app is not None,
        "runtime assembled",
    )
    components["runner_capability"] = _safe_component(
        bool(getattr(request.app.state, "runner_capabilities_verified", False)),
        "runner isolation controls verified",
    )
    ok = all(item["status"] == "ok" for item in components.values())
    payload = {"status": "ready" if ok else "not_ready", "components": components}
    return JSONResponse(payload, status_code=200 if ok else 503)


__all__ = ["router"]
