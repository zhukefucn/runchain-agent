from __future__ import annotations

from pathlib import Path

from app.runner.protocol import (
    ResolvedSkill,
    SkillExecutionRequest,
    SkillResolutionError,
)
from app.skills.service import SkillConflictError, SkillNotFoundError, SkillService


class SkillServiceResolver:
    """Bridge Task 7 governance/integrity checks to the runner protocol."""

    def __init__(self, service: SkillService) -> None:
        self._service = service

    async def resolve(self, request: SkillExecutionRequest) -> ResolvedSkill:
        try:
            effective = await self._service.resolve_execution_skill(
                request.user_id, request.skill_id, request.version
            )
        except (SkillNotFoundError, SkillConflictError) as exc:
            raise SkillResolutionError("Skill is unavailable") from exc
        root = Path(effective.install_path).resolve()
        return ResolvedSkill(
            skill_id=effective.id,
            version=effective.version,
            type=effective.type,
            install_root=root,
            entrypoint=(root / effective.entrypoint).resolve(),
        )
