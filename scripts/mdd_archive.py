#!/usr/bin/env python3
"""Testable archive and SQLite safety primitives used by mddctl.

The shell management entry point deliberately delegates archive parsing to this
module.  In particular, no call site uses ``tar.extractall``: backup archives
may contain credentials and are restored as root, so every member type and path
must be checked before any data is written.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tarfile


DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
EXCLUDED_DIRECTORIES = {"cache", "update", "tmp", "backups"}
MANIFEST_FIELDS = {"format", "kind", "version", "source_commit", "created_at"}


class ArchiveError(RuntimeError):
    pass


def _data_entries(root: Path):
    """Yield a sorted, non-following walk of regular files and directories."""
    pending = [root]
    while pending:
        base = pending.pop()
        try:
            entries = sorted(os.scandir(base), key=lambda item: item.name)
        except OSError as exc:
            raise ArchiveError(f"could not inspect data directory: {base}") from exc
        child_directories = []
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ArchiveError(f"could not inspect data path: {path}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise ArchiveError(f"symbolic links are not allowed in backups: {path.relative_to(root)}")
            if stat.S_ISDIR(metadata.st_mode):
                if entry.name in EXCLUDED_DIRECTORIES:
                    continue
                child_directories.append(path)
                yield path, metadata
            elif stat.S_ISREG(metadata.st_mode):
                yield path, metadata
            else:
                raise ArchiveError(f"unsupported data path type: {path.relative_to(root)}")
        pending.extend(reversed(child_directories))


def sqlite_check_tree(root: Path) -> list[str]:
    """Checkpoint and integrity-check every recognized SQLite file under root."""
    if root.is_symlink():
        raise ArchiveError("the data root may not be a symbolic link")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ArchiveError("the data root is not a directory")
    checked: list[str] = []
    for path, metadata in _data_entries(root):
        if not stat.S_ISREG(metadata.st_mode) or not path.name.lower().endswith(DATABASE_SUFFIXES):
            continue
        relative = str(path.relative_to(root))
        connection = None
        try:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=15)
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and checkpoint[0] != 0:
                raise ArchiveError(f"SQLite WAL checkpoint was busy: {relative}")
            result = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            raise ArchiveError(f"SQLite validation failed: {relative}: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()
        if not result or result[0] != "ok":
            raise ArchiveError(f"SQLite integrity check failed: {relative}")
        checked.append(relative)
    return checked


def _manifest(*, version: str, source_commit: str, created_at: str) -> dict:
    if not isinstance(version, str) or not version or len(version) > 128:
        raise ArchiveError("version must be a non-empty string")
    if not isinstance(created_at, str) or not created_at or len(created_at) > 128:
        raise ArchiveError("created_at must be a non-empty string")
    if not source_commit or len(source_commit) != 40 or any(
            character not in "0123456789abcdef" for character in source_commit.lower()):
        raise ArchiveError("source commit must be an exact 40-character hexadecimal id")
    return {
        "format": 1,
        "kind": "mdd-sim-gateway-data",
        "version": version,
        "source_commit": source_commit.lower(),
        "created_at": created_at,
    }


def write_manifest(path: Path, *, version: str, source_commit: str, created_at: str) -> None:
    payload = _manifest(version=version, source_commit=source_commit, created_at=created_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, indent=2)
        stream.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def read_manifest(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ArchiveError("backup manifest is missing or invalid") from exc
    if not isinstance(value, dict) or set(value) != MANIFEST_FIELDS:
        raise ArchiveError("backup manifest fields are invalid")
    if value.get("format") != 1 or value.get("kind") != "mdd-sim-gateway-data":
        raise ArchiveError("unsupported backup manifest")
    commit = str(value.get("source_commit") or "")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ArchiveError("backup manifest has an invalid source commit")
    if not isinstance(value.get("version"), str) or not value["version"] or len(value["version"]) > 128:
        raise ArchiveError("backup manifest has an invalid version")
    if not isinstance(value.get("created_at"), str) or not value["created_at"] or len(value["created_at"]) > 128:
        raise ArchiveError("backup manifest has an invalid creation time")
    return value


def _tar_info(name: str, metadata: os.stat_result, *, directory: bool) -> tarfile.TarInfo:
    item = tarfile.TarInfo(name)
    item.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    item.mode = stat.S_IMODE(metadata.st_mode) & 0o777
    item.uid = item.gid = 0
    item.uname = item.gname = "root"
    item.mtime = int(metadata.st_mtime)
    if directory:
        item.size = 0
    return item


def create_backup(data_root: Path, archive_path: Path, *, version: str,
                  source_commit: str, created_at: str) -> list[str]:
    """Create a root-owned portable archive from a quiesced data directory."""
    if data_root.is_symlink():
        raise ArchiveError("the data root may not be a symbolic link")
    data_root = data_root.resolve(strict=True)
    if not data_root.is_dir():
        raise ArchiveError("the data root is not a directory")
    checked = sqlite_check_tree(data_root)
    payload = _manifest(version=version, source_commit=source_commit, created_at=created_at)
    manifest = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    root_metadata = data_root.stat()

    with tarfile.open(archive_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.mode = 0o600
        manifest_info.uid = manifest_info.gid = 0
        manifest_info.uname = manifest_info.gname = "root"
        manifest_info.size = len(manifest)
        manifest_info.mtime = int(root_metadata.st_mtime)
        archive.addfile(manifest_info, io.BytesIO(manifest))
        archive.addfile(_tar_info("data", root_metadata, directory=True))

        for path, metadata in _data_entries(data_root):
            relative = path.relative_to(data_root).as_posix()
            archive_name = f"data/{relative}"
            if stat.S_ISDIR(metadata.st_mode):
                archive.addfile(_tar_info(archive_name, metadata, directory=True))
                continue
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            try:
                current = os.fstat(descriptor)
                inode_changed = os.name != "nt" and current.st_ino != metadata.st_ino
                if not stat.S_ISREG(current.st_mode) or inode_changed:
                    raise ArchiveError(f"data path changed while backing up: {relative}")
                item = _tar_info(archive_name, current, directory=False)
                item.size = current.st_size
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    archive.addfile(item, stream)
                after = os.fstat(descriptor)
                # Windows may refresh the sub-second timestamp representation after a file read.
                # Linux is the supported runtime and provides the exact nanosecond comparison.
                timestamp_changed = os.name != "nt" and after.st_mtime_ns != current.st_mtime_ns
                if after.st_size != current.st_size or timestamp_changed:
                    raise ArchiveError(f"data file changed while backing up: {relative}")
            finally:
                os.close(descriptor)
    os.chmod(archive_path, 0o600)
    return checked


def _safe_member_path(destination: Path, member: tarfile.TarInfo) -> Path:
    pure = PurePosixPath(member.name)
    normalized = "/".join(pure.parts)
    if (not member.name or pure.is_absolute() or ".." in pure.parts or
            member.name.rstrip("/") != normalized):
        raise ArchiveError(f"unsafe archive member: {member.name}")
    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
        raise ArchiveError(f"unsupported archive member type: {member.name}")
    target = destination.joinpath(*pure.parts).resolve()
    if target != destination and destination not in target.parents:
        raise ArchiveError(f"archive path escapes destination: {member.name}")
    return target


def safe_extract(archive_path: Path, destination: Path) -> dict:
    """Extract regular files/directories only, then validate manifest and SQLite."""
    if destination.is_symlink():
        raise ArchiveError("archive destination may not be a symbolic link")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ArchiveError("archive destination must be empty")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        names: set[str] = set()
        for member in members:
            name = member.name.rstrip("/")
            if name in names:
                raise ArchiveError(f"duplicate archive member: {member.name}")
            names.add(name)
        if "manifest.json" not in names or "data" not in names:
            raise ArchiveError("backup manifest/data is missing")
        manifest_member = next(member for member in members if member.name.rstrip("/") == "manifest.json")
        data_member = next(member for member in members if member.name.rstrip("/") == "data")
        if not manifest_member.isfile() or not data_member.isdir():
            raise ArchiveError("backup manifest/data has an invalid type")
        directory_modes: list[tuple[Path, int]] = []
        for member in members:
            name = member.name.rstrip("/")
            if name != "manifest.json" and name != "data" and not name.startswith("data/"):
                raise ArchiveError(f"unexpected top-level archive member: {member.name}")
            target = _safe_member_path(destination, member)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                directory_modes.append((target, member.mode & 0o777))
                continue
            if not member.isfile():
                raise ArchiveError(f"unsupported archive member type: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ArchiveError(f"could not read archive member: {member.name}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, member.mode & 0o777)
        for target, mode in reversed(directory_modes):
            os.chmod(target, mode)
    manifest = read_manifest(destination / "manifest.json")
    sqlite_check_tree(destination / "data")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    sqlite_parser = subparsers.add_parser("sqlite-check")
    sqlite_parser.add_argument("root", type=Path)
    manifest_parser = subparsers.add_parser("write-manifest")
    manifest_parser.add_argument("path", type=Path)
    manifest_parser.add_argument("version")
    manifest_parser.add_argument("source_commit")
    manifest_parser.add_argument("created_at")
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("data_root", type=Path)
    create_parser.add_argument("archive", type=Path)
    create_parser.add_argument("version")
    create_parser.add_argument("source_commit")
    create_parser.add_argument("created_at")
    extract_parser = subparsers.add_parser("verify-extract")
    extract_parser.add_argument("archive", type=Path)
    extract_parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()

    try:
        if arguments.command == "sqlite-check":
            sqlite_check_tree(arguments.root)
        elif arguments.command == "write-manifest":
            write_manifest(arguments.path, version=arguments.version,
                           source_commit=arguments.source_commit,
                           created_at=arguments.created_at)
        elif arguments.command == "create":
            create_backup(arguments.data_root, arguments.archive, version=arguments.version,
                          source_commit=arguments.source_commit, created_at=arguments.created_at)
        else:
            safe_extract(arguments.archive, arguments.destination)
    except (ArchiveError, OSError, sqlite3.Error, tarfile.TarError) as exc:
        parser.exit(1, f"mdd archive error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
