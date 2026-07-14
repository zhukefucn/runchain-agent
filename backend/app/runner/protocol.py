from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Literal, Protocol


ExecutionStatus = Literal[
    "success",
    "failed",
    "cleanup_failed",
    "timeout",
    "output_limit",
    "stderr_limit",
    "invalid_output",
    "invalid_input",
    "queue_timeout",
    "not_authorized",
    "unsupported_skill_type",
]


class SkillResolutionError(PermissionError):
    """Fail-closed result of authorization, publication, or integrity checks."""


@dataclass(frozen=True, slots=True)
class SkillExecutionRequest:
    """Caller-controlled data accepted by the executor.

    There is deliberately no script path, executable, command, environment, or
    working-directory field. Those values can only come from a trusted resolver.
    """

    user_id: str
    skill_id: str
    input_data: dict[str, Any]
    request_id: str
    version: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("user_id", self.user_id),
            ("skill_id", self.skill_id),
            ("request_id", self.request_id),
        ):
            if type(value) is not str or not value or len(value) > 160:
                raise ValueError(f"{name} must be a non-empty bounded string")
        if self.version is not None and (
            type(self.version) is not str or not self.version or len(self.version) > 64
        ):
            raise ValueError("version must be a non-empty bounded string")
        if type(self.input_data) is not dict:
            raise TypeError("input_data must be a JSON object")
        try:
            json.dumps(self.input_data, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("input_data must contain only finite JSON values") from exc


@dataclass(frozen=True, slots=True)
class ResolvedSkill:
    """Trusted, authorized metadata produced after an on-disk integrity check."""

    skill_id: str
    version: str
    type: str
    install_root: Path
    entrypoint: Path


class TrustedSkillResolver(Protocol):
    async def resolve(self, request: SkillExecutionRequest) -> ResolvedSkill:
        """Resolve one currently authorized Skill and verify its disk contents."""


@dataclass(frozen=True, slots=True)
class SkillExecutionResult:
    status: ExecutionStatus
    output: Any | None
    stderr_summary: str
    duration_ms: int
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class RunnerAuditEvent:
    """Low-risk invocation metadata; never carries input, output, env, or paths."""

    user_id: str
    skill_id: str
    request_id: str
    status: ExecutionStatus
    duration_ms: int
    exit_code: int | None
