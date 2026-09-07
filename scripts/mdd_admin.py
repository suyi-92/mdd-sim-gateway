#!/usr/bin/env python3
"""Interactive, host-only administrator recovery invoked by mddctl."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time

UNITS = ("mdd-sim-gateway-orchestrator.service", "mdd-sim-gateway-control.service")


def private_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (path != path.resolve(strict=True) or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
        raise ValueError("administrator recovery requires private managed directories")


def read_private(path: Path, *, optional: bool = False) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if optional:
            return None
        raise ValueError("administrator account is not configured") from None
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077
                or metadata.st_size > 16 * 1024 * 1024):
            raise ValueError("administrator file permissions or size are unsafe")
        return stream.read()


def preflight(data: Path, state: Path) -> dict:
    private_directory(data)
    private_directory(state)
    value = json.loads(read_private(data / "auth.json"))
    if (not isinstance(value, dict) or not value.get("salt") or not value.get("password_hash")
            or not isinstance(value.get("username"), str)):
        raise ValueError("administrator account is invalid; no changes made")
    read_private(data / "sessions.json", optional=True)
    return value


def atomic_write(path: Path, value: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".admin-reset-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def systemctl(action: str, unit: str) -> bool:
    result = subprocess.run(["systemctl", action, unit], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False, timeout=60)
    if action != "is-active" and result.returncode:
        raise RuntimeError("administrator recovery service operation failed")
    return result.returncode == 0


def healthy() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = subprocess.run([
            "curl", "--fail", "--silent", "--insecure", "--max-time", "2",
            "https://127.0.0.1:8443/api/auth/status"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False)
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("administrator recovery HTTPS health check failed")


def reset_admin(data: Path, state: Path, password: str, derive,
                service=systemctl, health=healthy) -> None:
    if not 10 <= len(password) <= 256:
        raise ValueError("new password must contain 10-256 characters")
    preflight(data, state)
    active = {unit: service("is-active", unit) for unit in UNITS}
    originals = {}
    changed = False
    try:
        # Quiesce both writers while preserving Engine containers and previous service state.
        for unit in UNITS:
            if active[unit]:
                service("stop", unit)
            if service("is-active", unit):
                raise RuntimeError("administrator recovery could not stop management services")
        account = preflight(data, state)
        for name in ("auth.json", "sessions.json"):
            originals[name] = read_private(data / name, optional=name == "sessions.json")
        backup = Path(tempfile.mkdtemp(prefix="admin-reset-", dir=state))
        for name, content in originals.items():
            if content is not None:
                atomic_write(backup / name, content)
        salt = secrets.token_bytes(16)
        account.update(salt=salt.hex(), password_hash=derive(password, salt).hex(),
                       changed_at=int(time.time()))
        changed = True
        atomic_write(data / "sessions.json", b'{"version":1,"sessions":{}}\n')
        atomic_write(data / "auth.json", json.dumps(account).encode("utf-8"))
        for unit in UNITS:
            if active[unit]:
                service("start", unit)
        if active[UNITS[1]]:
            health()
    except BaseException:
        if changed:
            for unit in UNITS:
                service("stop", unit)
            for name, content in originals.items():
                if content is None:
                    (data / name).unlink(missing_ok=True)
                else:
                    atomic_write(data / name, content)
        for unit in UNITS:
            if active[unit]:
                service("start", unit)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, "run administrator recovery through sudo mddctl reset-admin\n")
    try:
        preflight(arguments.data_dir, arguments.state_dir)
        if arguments.dry_run:
            print("Administrator recovery preflight passed; no password or service changes.")
            return 0
        if not sys.stdin.isatty():
            raise ValueError("run reset-admin in an interactive terminal; passwords are never command arguments")
        password = getpass.getpass("New administrator password (10-256 characters): ")
        if password != getpass.getpass("Confirm new password: "):
            raise ValueError("passwords do not match; no changes made")
        # Import the active generation's hashing implementation, without a separate algorithm.
        from app.auth import _derive
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt
        for signum in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(signum, interrupted)
        reset_admin(arguments.data_dir, arguments.state_dir, password, _derive)
        print("Administrator password reset; previous login sessions revoked. Gateway data retained.")
    except KeyboardInterrupt:
        parser.exit(1, "administrator recovery interrupted; previous state restored when possible\n")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        parser.exit(1, "administrator recovery failed; check permissions, password length and management services\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
