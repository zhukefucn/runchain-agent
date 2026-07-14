from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


SkillType = Literal["prompt", "python", "mcp"]


@dataclass(frozen=True, slots=True)
class SkillManifest:
    id: str
    name: str
    version: str
    type: SkillType
    entrypoint: str
    description: str = ""
    parameters: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ValidatedSkillPackage:
    manifest: SkillManifest
    root: str
    files: dict[str, bytes]
    upload_sha256: str
    content_sha256: str
    skill_md_sha256: str
    file_sha256: dict[str, str]
    warnings: tuple[str, ...] = ()
