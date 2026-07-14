"""Validate the sibling AgentScope source archive against the demo lock."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


def _source_files(root: Path):
    yield root / "pyproject.toml"
    for path in sorted((root / "src" / "agentscope").rglob("*")):
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        ):
            yield path


def source_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def source_version(root: Path) -> str:
    version_file = root / "src" / "agentscope" / "_version.py"
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']',
        version_file.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise RuntimeError("AgentScope version could not be determined")
    return match.group(1)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    agentscope_root = (project_root.parent / "agentscope-main").resolve()
    lock_path = project_root / "agentscope-source.lock.json"
    if not agentscope_root.is_dir():
        print(f"AgentScope source not found: {agentscope_root}", file=sys.stderr)
        return 1

    expected = json.loads(lock_path.read_text(encoding="utf-8"))
    observed = {
        "version": source_version(agentscope_root),
        "source_tree_sha256": source_tree_sha256(agentscope_root),
    }
    if observed != expected:
        print("AgentScope source does not match agentscope-source.lock.json", file=sys.stderr)
        print(json.dumps(observed, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    print(
        "AgentScope source verified: "
        f"version {observed['version']}, tree {observed['source_tree_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
