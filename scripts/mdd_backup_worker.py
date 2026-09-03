#!/usr/bin/env python3
"""Run one WebUI-requested data operation outside the services it must stop.

The host orchestrator starts this script in a transient systemd unit.  Arguments are deliberately
closed: the browser never supplies a host path or command, and mddctl remains the only component
that snapshots, verifies, switches, restarts, and rolls back managed data.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time


BACKUP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.tar\.gz\Z")
OPERATION_ID = re.compile(r"[0-9a-f]{16}\Z")


class WorkerError(RuntimeError):
    pass


def _canonical_directory(value: str, label: str) -> Path:
    if not value or not os.path.isabs(value) or value == "/" or any(ch.isspace() for ch in value):
        raise WorkerError(f"invalid {label}")
    path = Path(value)
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise WorkerError(f"unavailable {label}") from exc
    metadata = path.stat(follow_symlinks=False)
    if (path != canonical or path.is_symlink() or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
        raise WorkerError(f"unsafe {label}")
    return path


def _private_regular(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise WorkerError("backup archive and checksum are both required") from exc
    # main() requires root. Using the effective uid here also keeps the primitive testable in
    # an unprivileged temporary directory without weakening the production ownership check.
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
        raise WorkerError("backup archive permissions are unsafe")


def _read_status(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _publish(path: Path, operation_id: str, action: str, backup_name: str,
             state: str, error_code: str = "") -> None:
    current = _read_status(path)
    if current and current.get("operation_id") != operation_id:
        raise WorkerError("operation status belongs to another request")
    value = {
        "operation_id": operation_id,
        "action": action,
        "backup_name": backup_name,
        "state": state,
        "updated_at": int(time.time()),
    }
    if error_code:
        value["error_code"] = error_code
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def run_operation(*, action: str, operation_id: str, backup_name: str,
                  state_dir: str, backup_dir: str,
                  mddctl: str = "/usr/local/sbin/mddctl") -> int:
    if action not in {"create", "restore"}:
        raise WorkerError("unknown operation")
    if not OPERATION_ID.fullmatch(operation_id):
        raise WorkerError("invalid operation id")
    if not BACKUP_NAME.fullmatch(backup_name):
        raise WorkerError("invalid backup name")
    state_root = _canonical_directory(state_dir, "state directory")
    backup_root = _canonical_directory(backup_dir, "backup directory")
    status_path = state_root / "backup-operation-status.json"
    archive = backup_root / backup_name
    checksum = backup_root / f"{backup_name}.sha256"
    _publish(status_path, operation_id, action, backup_name, "running")
    try:
        if action == "create":
            if (archive.exists() or archive.is_symlink()
                    or checksum.exists() or checksum.is_symlink()):
                raise WorkerError("backup output already exists")
            command = [mddctl, "backup", "--output", str(archive)]
        else:
            _private_regular(archive)
            _private_regular(checksum)
            command = [mddctl, "restore", "--input", str(archive)]
    except WorkerError:
        _publish(status_path, operation_id, action, backup_name, "failed",
                 "backup.error.invalid_backup")
        return 1
    try:
        result = subprocess.run(command, check=False)
    except OSError:
        _publish(status_path, operation_id, action, backup_name, "failed",
                 "backup.error.launch")
        return 1
    if result.returncode:
        _publish(status_path, operation_id, action, backup_name, "failed",
                 f"backup.error.{action}_failed")
        return result.returncode
    _publish(status_path, operation_id, action, backup_name, "success")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", required=True, choices=("create", "restore"))
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--backup-name", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--backup-dir", required=True)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, "backup worker must run as root\n")
    try:
        return run_operation(
            action=arguments.action,
            operation_id=arguments.operation_id,
            backup_name=arguments.backup_name,
            state_dir=arguments.state_dir,
            backup_dir=arguments.backup_dir,
        )
    except (OSError, WorkerError) as exc:
        parser.exit(1, f"backup worker error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
