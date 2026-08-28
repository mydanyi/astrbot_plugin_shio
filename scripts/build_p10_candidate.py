from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import zipfile
from pathlib import Path
from typing import Any


PACKAGE_ROOT_NAME = "astrbot_plugin_shio"
RELEASE_ROOT_FILES = (
    "__init__.py",
    "_conf_schema.json",
    "main.py",
    "metadata.yaml",
)
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_VERSION_LINE = re.compile(r"(?m)^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _release_version(source_root: Path) -> str:
    metadata = (source_root / "metadata.yaml").read_text(encoding="utf-8")
    match = _VERSION_LINE.search(metadata)
    if match is None:
        raise ValueError("release_version_missing")
    return match.group(1)


def _checked_file(source_root: Path, relative: str) -> Path:
    source = source_root / Path(relative)
    if source.is_symlink() or not source.is_file():
        raise ValueError("release_source_file_invalid")
    resolved_root = source_root.resolve(strict=True)
    resolved_source = source.resolve(strict=True)
    if not resolved_source.is_relative_to(resolved_root):
        raise ValueError("release_source_outside_root")
    return source


def collect_release_files(source_root: Path) -> tuple[Path, ...]:
    root = Path(source_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("release_source_root_invalid")

    relative_paths = list(RELEASE_ROOT_FILES)
    relative_paths.extend(
        path.relative_to(root).as_posix()
        for path in sorted((root / "core").rglob("*.py"), key=lambda item: item.as_posix())
    )
    relative_paths.extend(
        path.relative_to(root).as_posix()
        for path in sorted((root / "assets" / "personas").glob("*.json"), key=lambda item: item.as_posix())
    )
    if len(relative_paths) != len(set(relative_paths)):
        raise ValueError("release_source_duplicate_path")
    return tuple(_checked_file(root, relative) for relative in relative_paths)


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, _ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.flag_bits |= 0x800
    return info


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_candidate(source_root: Path, archive_path: Path) -> dict[str, Any]:
    root = Path(source_root)
    archive = Path(archive_path)
    version = _release_version(root)
    expected_name = f"{PACKAGE_ROOT_NAME}_v{version}_upload.zip"
    if archive.name != expected_name:
        raise ValueError("candidate_archive_name_invalid")

    sources = collect_release_files(root)
    entries: list[dict[str, Any]] = []
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary_archive = archive.with_name(f".{archive.name}.tmp")
    try:
        with zipfile.ZipFile(
            temporary_archive,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as package:
            for source in sorted(sources, key=lambda item: item.relative_to(root).as_posix()):
                relative = source.relative_to(root).as_posix()
                package_path = f"{PACKAGE_ROOT_NAME}/{relative}"
                payload = source.read_bytes()
                package.writestr(_zip_info(package_path), payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
                entries.append(
                    {
                        "path": package_path,
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
        os.replace(temporary_archive, archive)
    finally:
        if temporary_archive.exists():
            temporary_archive.unlink()

    archive_payload = archive.read_bytes()
    manifest: dict[str, Any] = {
        "schema": "shio.release_candidate.v1",
        "release_version": version,
        "package_name": archive.name,
        "package_sha256": hashlib.sha256(archive_payload).hexdigest(),
        "package_bytes": len(archive_payload),
        "file_count": len(entries),
        "uncompressed_bytes": sum(entry["bytes"] for entry in entries),
        "files": entries,
    }
    manifest_payload = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(archive.with_suffix(".manifest.json"), manifest_payload)
    return manifest


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("candidate_manifest_duplicate_key")
        result[key] = value
    return result


def _is_release_member(relative: str) -> bool:
    if relative in RELEASE_ROOT_FILES:
        return True
    path = Path(relative)
    parts = path.parts
    if len(parts) >= 2 and parts[0] == "core" and path.suffix == ".py":
        return True
    return len(parts) == 3 and parts[:2] == ("assets", "personas") and path.suffix == ".json"


def verify_candidate(archive_path: Path, manifest_path: Path) -> dict[str, Any]:
    archive = Path(archive_path)
    manifest_file = Path(manifest_path)
    try:
        manifest = json.loads(
            manifest_file.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("candidate_manifest_non_finite")),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate_manifest_invalid") from exc
    if type(manifest) is not dict or set(manifest) != {
        "schema",
        "release_version",
        "package_name",
        "package_sha256",
        "package_bytes",
        "file_count",
        "uncompressed_bytes",
        "files",
    }:
        raise ValueError("candidate_manifest_shape_invalid")
    version = manifest["release_version"]
    expected_name = f"{PACKAGE_ROOT_NAME}_v{version}_upload.zip"
    if (
        manifest["schema"] != "shio.release_candidate.v1"
        or type(version) is not str
        or _VERSION_LINE.fullmatch(f"version: {version}") is None
        or type(manifest["package_name"]) is not str
        or manifest["package_name"] != expected_name
        or archive.name != expected_name
        or type(manifest["package_sha256"]) is not str
        or _SHA256.fullmatch(manifest["package_sha256"]) is None
        or type(manifest["package_bytes"]) is not int
        or type(manifest["file_count"]) is not int
        or type(manifest["uncompressed_bytes"]) is not int
        or type(manifest["files"]) is not list
    ):
        raise ValueError("candidate_manifest_value_invalid")
    archive_payload = archive.read_bytes()
    if len(archive_payload) != manifest["package_bytes"]:
        raise ValueError("candidate_package_size_mismatch")
    if hashlib.sha256(archive_payload).hexdigest() != manifest["package_sha256"]:
        raise ValueError("candidate_package_digest_mismatch")

    with zipfile.ZipFile(archive) as package:
        infos = package.infolist()
        names = tuple(info.filename for info in infos)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("candidate_package_member_order_invalid")
        verified_files: list[dict[str, Any]] = []
        for info in infos:
            name = info.filename
            prefix = f"{PACKAGE_ROOT_NAME}/"
            if not name.startswith(prefix) or name.endswith("/") or "\\" in name:
                raise ValueError("candidate_package_member_path_invalid")
            relative = name[len(prefix) :]
            if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                raise ValueError("candidate_package_member_path_invalid")
            if not _is_release_member(relative):
                raise ValueError("candidate_package_member_not_allowed")
            mode = info.external_attr >> 16
            if info.date_time != _ZIP_TIMESTAMP or not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o644:
                raise ValueError("candidate_package_member_metadata_invalid")
            payload = package.read(info)
            verified_files.append(
                {
                    "path": name,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        metadata = package.read(f"{PACKAGE_ROOT_NAME}/metadata.yaml").decode("utf-8")
    if _VERSION_LINE.search(metadata) is None or _VERSION_LINE.search(metadata).group(1) != version:
        raise ValueError("candidate_package_version_mismatch")
    if verified_files != manifest["files"]:
        raise ValueError("candidate_package_file_manifest_mismatch")
    if len(verified_files) != manifest["file_count"]:
        raise ValueError("candidate_package_file_count_mismatch")
    if sum(item["bytes"] for item in verified_files) != manifest["uncompressed_bytes"]:
        raise ValueError("candidate_package_uncompressed_size_mismatch")
    return {
        "schema": manifest["schema"],
        "release_version": version,
        "package_name": archive.name,
        "package_sha256": manifest["package_sha256"],
        "file_count": manifest["file_count"],
        "verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the deterministic Shio P10 release candidate.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_candidate(args.source, args.output)
    print(
        json.dumps(
            {
                "schema": manifest["schema"],
                "release_version": manifest["release_version"],
                "package_name": manifest["package_name"],
                "package_sha256": manifest["package_sha256"],
                "package_bytes": manifest["package_bytes"],
                "file_count": manifest["file_count"],
                "uncompressed_bytes": manifest["uncompressed_bytes"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
