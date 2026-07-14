from __future__ import annotations

import io
import json
import stat
import zipfile

import pytest

from app.skills.package import (
    MAX_ENTRIES,
    SkillPackageError,
    canonical_content_sha256,
    validate_skill_zip,
)


def skill_zip(
    files: dict[str, bytes | str] | None = None,
    *,
    manifest: dict | None = None,
) -> bytes:
    manifest = manifest or {
        "id": "private-demo",
        "name": "private-demo",
        "version": "1.0.0",
        "type": "python",
        "entrypoint": "main.py",
        "description": "demo",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        },
    }
    members: dict[str, bytes | str] = {
        "private-demo/SKILL.md": "# Private demo",
        "private-demo/skill.json": json.dumps(manifest),
        "private-demo/main.py": "print('ok')",
    }
    if files is not None:
        members = files
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return output.getvalue()


def test_valid_skill_zip_returns_manifest_files_and_upload_hash():
    package = validate_skill_zip(skill_zip())

    assert package.manifest.name == "private-demo"
    assert package.manifest.version == "1.0.0"
    assert package.manifest.type == "python"
    assert package.root == "private-demo"
    assert set(package.files) == {"SKILL.md", "skill.json", "main.py"}
    assert len(package.upload_sha256) == 64
    assert len(package.skill_md_sha256) == 64
    assert package.manifest.parameters["type"] == "object"


@pytest.mark.parametrize(
    "member",
    [
        "../escape.py",
        "/absolute.py",
        "C:\\escape.py",
        "\\\\server\\share\\x",
        "root/..\\escape.py",
        "root//escape.py",
        "root/./escape.py",
        "root/file.txt:stream",
        "root/CON.txt",
        "root/trailing. ",
        "root/bad?.py",
        "root/bad|name.py",
        "root/control\x1f.py",
    ],
)
def test_unsafe_zip_paths_are_rejected(member):
    with pytest.raises(SkillPackageError, match="unsafe path"):
        validate_skill_zip(skill_zip({member: "x"}))


def test_nul_in_raw_member_name_is_rejected():
    upload = skill_zip({"root/ab": "x"}).replace(b"root/ab", b"root/a\x00")
    with pytest.raises(SkillPackageError, match="unsafe path"):
        validate_skill_zip(upload)


def test_windows_safe_path_length_limits_are_enforced():
    long_component = "a" * 256
    with pytest.raises(SkillPackageError, match="unsafe path"):
        validate_skill_zip(skill_zip({f"root/{long_component}": "x"}))


@pytest.mark.parametrize(
    "files",
    [
        {"a/SKILL.md": "x", "b/skill.json": "{}"},
        {"root/SKILL.md": "x"},
        {"root/skill.json": "{}"},
        {"root/SKILL.md": "x", "root/skill.json": "{}", "root/Skill.md": "x"},
        {"root/SKILL.md": "x", "root/skill.json": "{}", "root/e\u0301.py": "x", "root/\u00e9.py": "x"},
        {"root/SKILL.md": "x", "root/skill.json": "{}", "root/a": "x", "root/a/b": "x"},
    ],
)
def test_invalid_layout_and_aliases_are_rejected(files):
    with pytest.raises(SkillPackageError):
        validate_skill_zip(skill_zip(files))


@pytest.mark.parametrize(
    "change",
    [
        {"version": "1.0"},
        {"version": "01.0.0"},
        {"name": "Bad Name"},
        {"id": "different"},
        {"type": "shell"},
        {"entrypoint": "missing.py"},
        {"entrypoint": "../main.py"},
    ],
)
def test_manifest_fields_and_entrypoint_are_strict(change):
    manifest = {
        "id": "private-demo",
        "name": "private-demo",
        "version": "1.0.0",
        "type": "python",
        "entrypoint": "main.py",
    }
    manifest.update(change)
    with pytest.raises(SkillPackageError):
        validate_skill_zip(skill_zip(manifest=manifest))


def test_manifest_rejects_unknown_fields_and_invalid_parameter_schema():
    base = {
        "id": "private-demo", "name": "private-demo", "version": "1.0.0",
        "type": "python", "entrypoint": "main.py",
    }
    with pytest.raises(SkillPackageError, match="invalid fields"):
        validate_skill_zip(skill_zip(manifest={**base, "unknown": True}))
    with pytest.raises(SkillPackageError, match="parameters"):
        validate_skill_zip(skill_zip(manifest={**base, "parameters": {"type": 123}}))


def test_python_entrypoint_is_parsed_without_execution_and_warnings_are_structured():
    upload = skill_zip({
        "private-demo/SKILL.md": "# Demo",
        "private-demo/skill.json": json.dumps({
            "id": "private-demo", "name": "private-demo", "version": "1.0.0",
            "type": "python", "entrypoint": "main.py",
        }),
        "private-demo/main.py": "import os\nvalue = eval('1')\n",
    })
    package = validate_skill_zip(upload)
    assert "dangerous_import:os" in package.warnings
    assert "dangerous_call:eval" in package.warnings

    broken = skill_zip({
        "private-demo/SKILL.md": "# Demo",
        "private-demo/skill.json": json.dumps({
            "id": "private-demo", "name": "private-demo", "version": "1.0.0",
            "type": "python", "entrypoint": "main.py",
        }),
        "private-demo/main.py": "def broken(",
    })
    with pytest.raises(SkillPackageError, match="syntax"):
        validate_skill_zip(broken)


def test_prompt_skill_requires_skill_md_entrypoint_and_mcp_requires_json_entrypoint():
    prompt = {
        "id": "prompt-demo", "name": "prompt-demo", "version": "1.0.0",
        "type": "prompt", "entrypoint": "SKILL.md",
    }
    assert validate_skill_zip(skill_zip(manifest=prompt)).manifest.type == "prompt"

    bad = dict(prompt, entrypoint="main.py")
    with pytest.raises(SkillPackageError):
        validate_skill_zip(skill_zip(manifest=bad))

    mcp = dict(prompt, id="mcp-demo", name="mcp-demo", type="mcp", entrypoint="server.json")
    files = {
        "mcp-demo/SKILL.md": "# MCP",
        "mcp-demo/skill.json": json.dumps(mcp),
        "mcp-demo/server.json": "not json",
    }
    with pytest.raises(SkillPackageError, match="mcp entrypoint"):
        validate_skill_zip(skill_zip(files))


def test_symlink_encrypted_and_duplicate_members_are_rejected():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("root/SKILL.md", "x")
        archive.writestr("root/skill.json", "{}")
        link = zipfile.ZipInfo("root/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "target")
    with pytest.raises(SkillPackageError, match="regular file"):
        validate_skill_zip(output.getvalue())

    duplicate = io.BytesIO()
    with pytest.warns(UserWarning, match="Duplicate"):
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("root/SKILL.md", "x")
            archive.writestr("root/SKILL.md", "x")
    with pytest.raises(SkillPackageError, match="duplicate"):
        validate_skill_zip(duplicate.getvalue())

    encrypted = bytearray(skill_zip())
    central = encrypted.index(b"PK\x01\x02")
    flags = int.from_bytes(encrypted[central + 8:central + 10], "little") | 1
    encrypted[central + 8:central + 10] = flags.to_bytes(2, "little")
    with pytest.raises(SkillPackageError, match="encrypted"):
        validate_skill_zip(encrypted)


def test_corrupt_member_content_is_reported_as_package_error():
    output = io.BytesIO()
    manifest = {
        "id": "demo", "name": "demo", "version": "1.0.0",
        "type": "prompt", "entrypoint": "SKILL.md",
    }
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("demo/SKILL.md", "unique-skill-body")
        archive.writestr("demo/skill.json", json.dumps(manifest))
    corrupt = bytearray(output.getvalue())
    offset = corrupt.index(b"unique-skill-body")
    corrupt[offset] ^= 1
    with pytest.raises(SkillPackageError, match="corrupt"):
        validate_skill_zip(corrupt)


def test_streaming_limits_reject_high_ratio_and_large_content():
    huge = "0" * (2 * 1024 * 1024)
    with pytest.raises(SkillPackageError, match="compression ratio|too large"):
        validate_skill_zip(skill_zip({"root/SKILL.md": huge, "root/skill.json": "{}"}))


def test_entry_limit_counts_directories_and_file_directory_ancestor_conflicts():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for index in range(MAX_ENTRIES + 1):
            archive.writestr(f"root/empty-{index}/", b"")
    with pytest.raises(SkillPackageError, match="entries"):
        validate_skill_zip(output.getvalue())

    conflict = skill_zip({
        "root/a": "file",
        "root/a/b/": b"",
        "root/SKILL.md": "# x",
        "root/skill.json": "{}",
    })
    with pytest.raises(SkillPackageError, match="conflict"):
        validate_skill_zip(conflict)


def test_unsupported_compression_is_a_domain_error():
    upload = bytearray(skill_zip())
    local = upload.index(b"PK\x03\x04")
    central = upload.index(b"PK\x01\x02")
    upload[local + 8:local + 10] = (99).to_bytes(2, "little")
    upload[central + 10:central + 12] = (99).to_bytes(2, "little")
    with pytest.raises(SkillPackageError, match="compression"):
        validate_skill_zip(upload)


def test_canonical_hash_uses_unambiguous_length_prefixes():
    # The old `path + NUL + content + NUL` framing collides for these maps.
    left = {"a": b"b\x00c", "d": b"e"}
    right = {"a": b"b", "c\x00d": b"e"}
    assert canonical_content_sha256(left) != canonical_content_sha256(right)
