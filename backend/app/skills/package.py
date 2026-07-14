from __future__ import annotations

import hashlib
import io
import json
import ast
from pathlib import PurePosixPath
import re
import stat
import unicodedata
import zipfile

from jsonschema import Draft202012Validator, SchemaError

from app.skills.models import SkillManifest, ValidatedSkillPackage


MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_ZIP_MEMBERS = 128
MAX_MATERIALIZED_ENTRIES = 256
MAX_PATH_DEPTH = 16
MAX_FILES = 128
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_COMPRESSION_RATIO = 100
_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_DEVICE_NAMES = {
    "con", "prn", "aux", "nul", "clock$",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_MANIFEST_FIELDS = {
    "id", "name", "version", "type", "entrypoint", "description", "parameters"
}
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_WINDOWS_INVALID = set('<>"|?*')


class SkillPackageError(ValueError):
    pass


def _safe_member_parts(name: str) -> tuple[str, ...]:
    if not name or len(name) > 1000 or "\x00" in name or "\\" in name:
        raise SkillPackageError("unsafe path in ZIP member")
    if name.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", name):
        raise SkillPackageError("unsafe path in ZIP member")
    raw_parts = name.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise SkillPackageError("unsafe path in ZIP member")
    path = PurePosixPath(name)
    parts = path.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise SkillPackageError("unsafe path in ZIP member")
    for part in parts:
        if part.casefold() == ".runchain-uncommitted":
            raise SkillPackageError("reserved internal install marker name")
        if len(part) > 255:
            raise SkillPackageError("unsafe path component is too long")
        if unicodedata.normalize("NFC", part) != part:
            # Requiring canonical NFC makes aliases deterministic across filesystems.
            raise SkillPackageError("unsafe path uses non-canonical Unicode")
        if part.endswith((".", " ")) or ":" in part:
            raise SkillPackageError("unsafe path in ZIP member")
        if any(character in _WINDOWS_INVALID or ord(character) < 32 for character in part):
            raise SkillPackageError("unsafe path contains Windows-invalid characters")
        stem = part.split(".", 1)[0].casefold()
        if stem in _DEVICE_NAMES:
            raise SkillPackageError("unsafe path uses Windows device name")
    return parts


def _check_member_type(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        raise SkillPackageError("encrypted ZIP members are not supported")
    if info.compress_type not in _ALLOWED_COMPRESSION:
        raise SkillPackageError("unsupported ZIP compression method")
    if info.create_system != 3:
        return
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR):
        raise SkillPackageError("ZIP member must be a regular file or directory")


def _read_upload(source: bytes | bytearray | memoryview | io.BufferedIOBase) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    elif hasattr(source, "read"):
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = source.read(64 * 1024)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise SkillPackageError("Skill upload must be a binary stream")
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise SkillPackageError("Skill upload is too large")
            chunks.append(chunk)
        data = b"".join(chunks)
    else:
        raise SkillPackageError("Skill upload must be bytes or a binary stream")
    if len(data) > MAX_UPLOAD_BYTES:
        raise SkillPackageError("Skill upload is too large")
    return data


def _parse_manifest(raw: bytes, files: dict[str, bytes]) -> SkillManifest:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise SkillPackageError("skill.json is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillPackageError("skill.json must be valid UTF-8 JSON") from exc
    if type(value) is not dict or set(value) - _MANIFEST_FIELDS:
        raise SkillPackageError("skill.json contains invalid fields")
    required = ("id", "name", "version", "type", "entrypoint")
    if any(type(value.get(field)) is not str for field in required):
        raise SkillPackageError("skill.json is missing required string fields")
    name = value["name"]
    if not _NAME.fullmatch(name) or value["id"] != name:
        raise SkillPackageError("manifest id and name must be the same lowercase slug")
    if len(value["version"]) > 64 or not _SEMVER.fullmatch(value["version"]):
        raise SkillPackageError("manifest version must be strict semantic versioning")
    skill_type = value["type"]
    if skill_type not in ("prompt", "python", "mcp"):
        raise SkillPackageError("manifest type must be prompt, python, or mcp")
    entrypoint = value["entrypoint"]
    if len(entrypoint) > 500:
        raise SkillPackageError("manifest entrypoint is too long")
    parts = _safe_member_parts(entrypoint)
    if len(parts) != len(PurePosixPath(entrypoint).parts) or entrypoint not in files:
        raise SkillPackageError("manifest entrypoint must be a file inside the package")
    if skill_type == "prompt" and entrypoint != "SKILL.md":
        raise SkillPackageError("prompt Skill entrypoint must be SKILL.md")
    if skill_type == "python" and not entrypoint.endswith(".py"):
        raise SkillPackageError("python Skill entrypoint must be a Python file")
    if skill_type == "mcp" and not entrypoint.endswith(".json"):
        raise SkillPackageError("mcp Skill entrypoint must be a JSON file")
    if skill_type == "mcp":
        try:
            mcp_configuration = json.loads(files[entrypoint].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkillPackageError("mcp entrypoint must contain valid JSON") from exc
        if type(mcp_configuration) is not dict:
            raise SkillPackageError("mcp entrypoint must contain a JSON object")
    description = value.get("description", "")
    if type(description) is not str or len(description) > 1000:
        raise SkillPackageError("manifest description is invalid")
    parameters = value.get("parameters", {"type": "object", "properties": {}})
    if type(parameters) is not dict:
        raise SkillPackageError("manifest parameters must be a JSON Schema object")
    try:
        Draft202012Validator.check_schema(parameters)
    except SchemaError as exc:
        raise SkillPackageError("manifest parameters is not a valid JSON Schema") from exc
    return SkillManifest(
        id=value["id"], name=name, version=value["version"], type=skill_type,
        entrypoint=entrypoint, description=description, parameters=parameters,
    )


def _python_warnings(source: bytes) -> tuple[str, ...]:
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise SkillPackageError("python entrypoint has invalid syntax or encoding") from exc
    dangerous_imports = {"os", "subprocess", "socket", "ctypes", "winreg"}
    dangerous_calls = {"eval", "exec", "compile", "__import__", "open"}
    warnings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in dangerous_imports:
                    warnings.add(f"dangerous_import:{root}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root in dangerous_imports:
                warnings.add(f"dangerous_import:{root}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in dangerous_calls:
                warnings.add(f"dangerous_call:{node.func.id}")
    return tuple(sorted(warnings))


def canonical_content_sha256(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        encoded_name = name.encode("utf-8")
        content = files[name]
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def validate_skill_zip(
    source: bytes | bytearray | memoryview | io.BufferedIOBase,
) -> ValidatedSkillPackage:
    """Validate a local upload and read bounded file content without extracting it."""
    upload = _read_upload(source)
    try:
        archive = zipfile.ZipFile(io.BytesIO(upload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise SkillPackageError("upload is not a valid ZIP file") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise SkillPackageError("too many ZIP entries in Skill package")
        aliases: dict[str, bool] = {}
        raw_names: set[str] = set()
        file_paths: set[tuple[str, ...]] = set()
        directory_paths: set[tuple[str, ...]] = set()
        roots: set[str] = set()
        file_infos: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        for info in infos:
            _check_member_type(info)
            if "\x00" in info.orig_filename:
                raise SkillPackageError("unsafe path in ZIP member")
            parts = _safe_member_parts(info.filename.rstrip("/"))
            if len(parts) - 1 > MAX_PATH_DEPTH:
                raise SkillPackageError("Skill path depth exceeds the allowed limit")
            roots.add(parts[0])
            alias = "/".join(part.casefold() for part in parts)
            is_directory = info.is_dir()
            if info.filename in raw_names:
                raise SkillPackageError("duplicate ZIP member or filesystem alias")
            if alias in aliases:
                if aliases[alias] != is_directory:
                    raise SkillPackageError("file and directory paths conflict")
                raise SkillPackageError("duplicate ZIP member or filesystem alias")
            raw_names.add(info.filename)
            aliases[alias] = is_directory
            if is_directory:
                directory_paths.add(parts)
                continue
            file_paths.add(parts)
            file_infos.append((info, parts))
            if len(file_infos) > MAX_FILES:
                raise SkillPackageError("too many files in Skill package")
            if info.file_size > MAX_FILE_BYTES:
                raise SkillPackageError("Skill file is too large")
            if info.compress_size == 0 and info.file_size:
                raise SkillPackageError("invalid compression ratio")
            if info.file_size and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
                raise SkillPackageError("compression ratio is too high")
        if len(roots) != 1 or not file_infos:
            raise SkillPackageError("Skill ZIP must contain one root directory")
        all_paths = file_paths | directory_paths
        for path in all_paths:
            if any(path[:index] in file_paths for index in range(1, len(path))):
                raise SkillPackageError("file and directory paths conflict")
        for directory_path in directory_paths:
            if not any(
                len(file_path) > len(directory_path)
                and file_path[: len(directory_path)] == directory_path
                for file_path in file_paths
            ):
                raise SkillPackageError("explicit empty directory is not allowed")

        root = next(iter(roots))
        files: dict[str, bytes] = {}
        total = 0
        for info, parts in file_infos:
            relative = "/".join(parts[1:])
            if not relative:
                raise SkillPackageError("files must be inside the root directory")
            chunks: list[bytes] = []
            actual = 0
            try:
                with archive.open(info, "r") as member:
                    while True:
                        chunk = member.read(64 * 1024)
                        if not chunk:
                            break
                        actual += len(chunk)
                        total += len(chunk)
                        if actual > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                            raise SkillPackageError("uncompressed Skill content is too large")
                        chunks.append(chunk)
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                raise SkillPackageError("corrupt ZIP member content") from exc
            if actual != info.file_size:
                raise SkillPackageError("ZIP member size does not match its content")
            files[relative] = b"".join(chunks)

    if "SKILL.md" not in files or "skill.json" not in files:
        raise SkillPackageError("package requires exactly SKILL.md and skill.json")
    expected_directories = {
        "/".join(parts[:index])
        for name in files
        for parts in (name.split("/"),)
        for index in range(1, len(parts))
    }
    if len(files) + len(expected_directories) > MAX_MATERIALIZED_ENTRIES:
        raise SkillPackageError("Skill materialized entry budget is exceeded")
    try:
        files["SKILL.md"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillPackageError("SKILL.md must be UTF-8 text") from exc
    manifest = _parse_manifest(files["skill.json"], files)
    warnings = _python_warnings(files[manifest.entrypoint]) if manifest.type == "python" else ()
    file_sha256 = {
        name: hashlib.sha256(content).hexdigest() for name, content in files.items()
    }
    return ValidatedSkillPackage(
        manifest=manifest,
        root=root,
        files=files,
        upload_sha256=hashlib.sha256(upload).hexdigest(),
        content_sha256=canonical_content_sha256(files),
        skill_md_sha256=file_sha256["SKILL.md"],
        file_sha256=file_sha256,
        expected_directories=tuple(sorted(expected_directories)),
        warnings=warnings,
    )
