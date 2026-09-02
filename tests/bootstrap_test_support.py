"""Run public bootstrap behavior as an ordinary user, even in root test suites."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Mapping, Sequence

try:
    import pwd
except ImportError:  # pragma: no cover - imported by Windows-skipped tests
    pwd = None  # type: ignore[assignment]


def _bootstrap_user() -> tuple[int, int, str] | None:
    """Return the account that should exercise bootstrap's non-root entrypoint."""
    if pwd is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None

    sudo_uid = os.environ.get("SUDO_UID", "")
    if sudo_uid.isdecimal() and int(sudo_uid) > 0:
        try:
            account = pwd.getpwuid(int(sudo_uid))
            return account.pw_uid, account.pw_gid, account.pw_name
        except KeyError:
            pass

    candidate = pwd.getpwnam("nobody")
    if candidate.pw_uid == 0:
        raise RuntimeError("no unprivileged account is available for bootstrap tests")
    return candidate.pw_uid, candidate.pw_gid, candidate.pw_name


def handoff_test_tree_to_bootstrap_user(path: Path) -> None:
    """Give only a disposable test tree to the account used by the child process."""
    account = _bootstrap_user()
    if account is None:
        return
    uid, gid, _ = account

    for current, directories, files in os.walk(path, followlinks=False):
        os.chown(current, uid, gid, follow_symlinks=False)
        for name in directories:
            os.chown(
                Path(current) / name,
                uid,
                gid,
                follow_symlinks=False,
            )
        for name in files:
            os.chown(
                Path(current) / name,
                uid,
                gid,
                follow_symlinks=False,
            )


def run_bootstrap_as_user(
    command: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute bootstrap with the same privilege boundary as its public entrypoint."""
    environment = dict(os.environ if env is None else env)
    popen_options: dict[str, object] = {}
    account = _bootstrap_user()
    if account is not None:
        uid, gid, username = account
        for name in ("SUDO_GID", "SUDO_UID", "SUDO_USER"):
            environment.pop(name, None)
        environment.update({
            "HOME": str(cwd),
            "LOGNAME": username,
            "USER": username,
        })
        popen_options.update({
            "extra_groups": [],
            "group": gid,
            "user": uid,
        })

    return subprocess.run(
        list(command),
        cwd=cwd,
        env=environment,
        check=check,
        text=True,
        capture_output=True,
        **popen_options,
    )
