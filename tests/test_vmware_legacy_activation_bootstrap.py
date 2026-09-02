"""Dynamic bootstrap-update, install-boundary and Git-operation regressions."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.bootstrap_test_support import (
    handoff_test_tree_to_bootstrap_user,
    run_bootstrap_as_user,
)
from tests.test_mddctl_active_generation import ActiveGenerationFixture


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "bootstrap.sh"
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
MDDCTL = (ROOT / "scripts/mddctl").read_text(encoding="utf-8")
ORIGIN_URL = "https://github.com/suyi-92/mdd-sim-gateway.git"
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "MDD Test",
    "GIT_AUTHOR_EMAIL": "mdd-test@example.invalid",
    "GIT_COMMITTER_NAME": "MDD Test",
    "GIT_COMMITTER_EMAIL": "mdd-test@example.invalid",
}


def shell_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.find("\n}\n\n", start)
    if end < 0:
        raise AssertionError(f"could not bound shell function {name}")
    return source[start:end] + "\n}"


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env or GIT_ENV,
        check=check,
        text=True,
        capture_output=True,
    )


def init_repo(path: Path) -> str:
    path.mkdir()
    run(["git", "init", "--initial-branch=vmware", "."], cwd=path)
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    run(["git", "add", "."], cwd=path)
    run(["git", "commit", "-m", "initial"], cwd=path)
    return run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()


@unittest.skipIf(os.name == "nt" or not shutil.which("bash") or not shutil.which("git"),
                 "bootstrap regressions require Bash and Git")
class BootstrapUpdateTests(unittest.TestCase):
    def test_update_runs_downloaded_mddctl_passes_options_and_cleans_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            fakebin = root / "fakebin"
            fakebin.mkdir()
            source.mkdir()
            run(["git", "init", "--initial-branch=vmware", "."], cwd=source)
            (source / "scripts").mkdir()
            (source / "install.sh").write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            (source / "scripts/mddctl").write_text(
                """#!/bin/sh
printf '%s\n' "$0" > "$NEW_MDDCTL_PATH_LOG"
printf '%s\n' "$@" > "$NEW_MDDCTL_ARGS_LOG"
exit 0
""",
                encoding="ascii",
            )
            run(["git", "add", "."], cwd=source)
            run(["git", "commit", "-m", "downloaded update source"], cwd=source)

            real_git = shutil.which("git") or "git"
            (fakebin / "git").write_text(
                """#!/usr/bin/env bash
set -e
for argument in "$@"; do
  if [[ "$argument" == clone ]]; then
    destination=${!#}
    "$REAL_GIT" clone --single-branch --branch vmware "$BOOTSTRAP_SOURCE" "$destination" >/dev/null
    "$REAL_GIT" -C "$destination" remote set-url origin "$EXPECTED_ORIGIN"
    exit 0
  fi
done
exec "$REAL_GIT" "$@"
""",
                encoding="ascii",
            )
            (fakebin / "sudo").write_text(
                """#!/bin/sh
if [ "${1:-}" = -v ]; then exit 0; fi
if [ "${1:-}" = -H ]; then shift; fi
exec "$@"
""",
                encoding="ascii",
            )
            (fakebin / "mddctl").write_text(
                "#!/bin/sh\nprintf 'old-installed-mddctl-called\\n' > \"$OLD_MDDCTL_LOG\"\nexit 99\n",
                encoding="ascii",
            )
            for path in fakebin.iterdir():
                path.chmod(0o755)

            new_path_log = root / "new-path.log"
            new_args_log = root / "new-args.log"
            old_log = root / "old.log"
            bootstrap = root / "bootstrap.sh"
            shutil.copy2(BOOTSTRAP, bootstrap)
            bootstrap.chmod(0o755)
            environment = {
                **GIT_ENV,
                "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
                "REAL_GIT": real_git,
                "BOOTSTRAP_SOURCE": str(source),
                "EXPECTED_ORIGIN": ORIGIN_URL,
                "NEW_MDDCTL_PATH_LOG": str(new_path_log),
                "NEW_MDDCTL_ARGS_LOG": str(new_args_log),
                "OLD_MDDCTL_LOG": str(old_log),
            }
            handoff_test_tree_to_bootstrap_user(root)
            result = run_bootstrap_as_user(
                ["bash", str(bootstrap), "update", "--no-cache", "--yes", "--dry-run"],
                cwd=root,
                check=False,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            invoked = Path(new_path_log.read_text(encoding="utf-8").strip())
            self.assertEqual(invoked.name, "mddctl")
            self.assertEqual(invoked.parent.name, "scripts")
            self.assertEqual(
                new_args_log.read_text(encoding="utf-8").splitlines(),
                ["update", "--no-cache", "--yes", "--dry-run"],
            )
            self.assertFalse(old_log.exists())
            self.assertFalse(invoked.exists(), "bootstrap temporary checkout was not cleaned")


@unittest.skipIf(os.name == "nt" or not shutil.which("bash") or not shutil.which("git"),
                 "install boundary tests require Bash and Git")
class InstallCheckoutBoundaryTests(unittest.TestCase):
    def test_install_refuses_to_fast_forward_an_existing_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            run(["git", "init", "--initial-branch=vmware", "."], cwd=source)
            run(["git", "remote", "add", "origin", ORIGIN_URL], cwd=source)
            (source / ".gitignore").write_text("/.venv\n/webui/dist\n", encoding="utf-8")
            (source / "webui").mkdir()
            (source / "webui/tracked.txt").write_text("old\n", encoding="utf-8")
            (source / "tracked.txt").write_text("old\n", encoding="utf-8")
            run(["git", "add", "."], cwd=source)
            run(["git", "commit", "-m", "old"], cwd=source)
            old = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
            (source / "tracked.txt").write_text("new\n", encoding="utf-8")
            run(["git", "add", "."], cwd=source)
            run(["git", "commit", "-m", "new"], cwd=source)
            new = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
            run(["git", "update-ref", "refs/remotes/origin/vmware", new], cwd=source)

            managed = root / "managed"
            run(["git", "clone", "--quiet", str(source), str(managed)])
            run(["git", "switch", "-C", "vmware", old], cwd=managed)
            run(["git", "remote", "set-url", "origin", ORIGIN_URL], cwd=managed)
            cache = root / "cache/builds" / old
            (cache / "venv").mkdir(parents=True)
            (cache / "webui").mkdir()
            os.symlink(cache / "venv", managed / ".venv")
            os.symlink(cache / "webui", managed / "webui/dist")
            before = (
                run(["git", "rev-parse", "HEAD"], cwd=managed).stdout.strip(),
                (managed / ".venv").lstat().st_ino,
                os.readlink(managed / ".venv"),
                (managed / "webui/dist").lstat().st_ino,
                os.readlink(managed / "webui/dist"),
            )

            functions = "\n\n".join(
                shell_function(INSTALL, name)
                for name in ("git_operation_in_progress_at", "install_source_checkout")
            )
            script = f"""
set -Eeuo pipefail
{functions}
die() {{ printf '%s\\n' "$*" >&2; exit 1; }}
ORIGIN_URL={ORIGIN_URL!r}
source_dir=$1
install_dir=$2
ref=vmware
sha=
install_source_checkout
"""
            result = run(
                ["bash", "-c", script, "install-boundary", str(source), str(managed)],
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bootstrap update", result.stderr)
            after = (
                run(["git", "rev-parse", "HEAD"], cwd=managed).stdout.strip(),
                (managed / ".venv").lstat().st_ino,
                os.readlink(managed / ".venv"),
                (managed / "webui/dist").lstat().st_ino,
                os.readlink(managed / "webui/dist"),
            )
            self.assertEqual(after, before)


@unittest.skipIf(os.name == "nt" or not shutil.which("bash") or not shutil.which("git"),
                 "Git operation tests require Bash and Git")
class GitOperationPathTests(unittest.TestCase):
    def operation_result(self, repository: Path) -> subprocess.CompletedProcess[str]:
        helper = shell_function(MDDCTL, "git_operation_in_progress")
        script = f"""
set -Eeuo pipefail
{helper}
INSTALL_DIR=$1
git_operation_in_progress
"""
        return run(["bash", "-c", script, "git-operation", str(repository)], check=False)

    def mark_operation(self, repository: Path) -> Path:
        path = Path(
            run(
                ["git", "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD"],
                cwd=repository,
            ).stdout.strip()
        )
        path.write_text(run(["git", "rev-parse", "HEAD"], cwd=repository).stdout, encoding="ascii")
        return path

    def test_plain_gitdir_gitfile_and_linked_worktree_resolve_their_own_operation_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            init_repo(source)
            plain = root / "plain"
            run(["git", "clone", "--quiet", str(source), str(plain)])
            separate = root / "separate"
            separate_git = root / "separate.gitdir"
            run(["git", "clone", "--quiet", "--separate-git-dir", str(separate_git),
                 str(source), str(separate)])
            self.assertTrue((separate / ".git").is_file())
            linked = root / "linked"
            run(["git", "worktree", "add", "-q", "-b", "linked-test", str(linked)], cwd=source)
            self.assertTrue((linked / ".git").is_file())

            for repository in (plain, separate, linked):
                with self.subTest(repository=repository.name):
                    marker = self.mark_operation(repository)
                    self.assertTrue(marker.is_absolute())
                    result = self.operation_result(repository)
                    self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipIf(os.name == "nt" or not shutil.which("bash") or not shutil.which("git"),
                 "update order tests require Bash and Git")
class UpdatePreflightOrderTests(unittest.TestCase):
    def update_result(
        self,
        fixture: ActiveGenerationFixture,
        arguments: list[str],
        *,
        stop_after_fetch: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        log = fixture.root / "order.log"
        remote = fixture.root / "remote.git"
        run(["git", "clone", "--quiet", "--bare", str(fixture.repo), str(remote)])
        if stop_after_fetch:
            producer = fixture.root / "producer"
            run(["git", "clone", "--quiet", str(remote), str(producer)])
            (producer / "tracked.txt").write_text("remote update\n", encoding="utf-8")
            run(["git", "add", "tracked.txt"], cwd=producer)
            run(["git", "commit", "-m", "remote update"], cwd=producer)
            run(["git", "push", "origin", "vmware"], cwd=producer)
        run(["git", "remote", "set-url", "origin", str(remote)], cwd=fixture.repo)
        functions = "\n\n".join(
            shell_function(MDDCTL, name)
            for name in (
                "git_operation_in_progress",
                "managed_checkout_status_kind",
                "validate_managed_checkout",
                "validate_active_generation",
                "cmd_update",
            )
        )
        script = f"""
set -Eeuo pipefail
{functions}
die() {{ printf '%s\\n' "$*" >&2; exit 1; }}
info() {{ :; }}
warn() {{ :; }}
acquire_lock() {{ :; }}
driver_reprobe_native() {{ :; }}
bash() {{
  if [[ "$1" == "$INSTALL_DIR/install.sh" && "${{2:-}}" == verify ]]; then printf 'VERIFY\\n' >> "$order_log"; fi
  command bash "$@"
}}
git() {{
  local argument
  for argument in "$@"; do
    if [[ "$argument" == fetch ]]; then printf 'FETCH\\n' >> "$order_log"; fi
  done
  if [[ "$stop_after_fetch" == 1 && " $* " == *' merge-base '* ]]; then return 1; fi
  command git "$@"
}}
docker() {{
  [[ "$1" == image && "$2" == inspect && "$3" == "$ENGINE_STABLE_IMAGE" ]] || return 91
  cat "$stable_revision_file"
}}
INSTALL_DIR=$1
STATE_DIR=$2
CACHE_DIR=$3
stable_revision_file=$4
order_log=$5
stop_after_fetch=$6
ORIGIN_URL=$7
BRANCH=vmware
ENGINE_STABLE_IMAGE=mdd-sim-gateway/engine:latest
ACTIVE_GENERATION_COMMIT=
ACTIVE_GENERATION_BUILD=
MANAGED_CHECKOUT_STATUS_KIND=
shift 7
cmd_update "$@"
"""
        result = run(
            ["bash", "-c", script, "update-order", str(fixture.repo), str(fixture.state),
             str(fixture.cache), str(fixture.stable_revision), str(log),
             "1" if stop_after_fetch else "0", str(remote), *arguments],
            check=False,
        )
        events = log.read_text(encoding="ascii").splitlines() if log.exists() else []
        return result, events

    def test_dry_run_validates_before_return_without_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ActiveGenerationFixture(Path(directory))
            result, events = self.update_result(fixture, ["--dry-run"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("VERIFY", events)
            self.assertNotIn("FETCH", events)

    def test_noop_validates_before_real_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ActiveGenerationFixture(Path(directory))
            result, events = self.update_result(fixture, [])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(events.index("VERIFY"), events.index("FETCH"))

    def test_non_noop_real_fetch_still_occurs_after_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ActiveGenerationFixture(Path(directory))
            result, events = self.update_result(fixture, ["--yes"], stop_after_fetch=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertLess(events.index("VERIFY"), events.index("FETCH"))


if __name__ == "__main__":
    unittest.main()
