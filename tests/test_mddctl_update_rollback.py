"""Behavioral simulation of the mddctl update activation rollback transaction."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_mddctl_active_generation import ActiveGenerationFixture


ROOT = Path(__file__).resolve().parent.parent
MDDCTL = (ROOT / "scripts/mddctl").read_text(encoding="utf-8")
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
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=GIT_ENV,
        check=check,
        text=True,
        capture_output=True,
    )


@unittest.skipIf(os.name == "nt" or not shutil.which("bash") or not shutil.which("git"),
                 "rollback simulation requires Bash and Git")
class UpdateRollbackTransactionTests(unittest.TestCase):
    def test_health_failure_restores_complete_old_generation_and_run_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = ActiveGenerationFixture(root)
            old = fixture.sha
            remote = root / "remote.git"
            producer = root / "producer"
            run(["git", "clone", "--quiet", "--bare", str(fixture.repo), str(remote)])
            run(["git", "clone", "--quiet", str(remote), str(producer)])
            (producer / "tracked.txt").write_text("new generation\n", encoding="utf-8")
            run(["git", "add", "tracked.txt"], cwd=producer)
            run(["git", "commit", "-m", "new generation"], cwd=producer)
            run(["git", "push", "origin", "vmware"], cwd=producer)
            new = run(["git", "rev-parse", "HEAD"], cwd=producer).stdout.strip()
            run(["git", "remote", "set-url", "origin", str(remote)], cwd=fixture.repo)

            backup = root / "backups"
            backup.mkdir()
            images = root / "images"
            images.mkdir()
            runtime = root / "runtime-state"
            runtime.write_text("old-running\n", encoding="ascii")
            data = root / "managed-data"
            data.write_text("old-data\n", encoding="ascii")

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
die() {{ printf 'DIE:%s\\n' "$*" >&2; exit 1; }}
info() {{ printf 'INFO:%s\\n' "$*" >&2; }}
warn() {{ printf 'WARN:%s\\n' "$*" >&2; }}
acquire_lock() {{ :; }}
driver_reprobe_native() {{ :; }}
image_path() {{
  local key
  key=$(printf '%s' "$1" | tr '/:' '__')
  printf '%s/%s' "$images_dir" "$key"
}}
write_image() {{ printf '%s\\n' "$2" > "$(image_path "$1")"; }}
docker() {{
  if [[ "$1" == image && "$2" == inspect ]]; then
    local path
    path=$(image_path "$3")
    [[ -f "$path" ]] || return 1
    if [[ " $* " == *' --format '* ]]; then cat "$path"; fi
    return 0
  fi
  if [[ "$1" == tag ]]; then
    local source_image destination_image
    source_image=$(image_path "$2")
    destination_image=$(image_path "$3")
    cp "$source_image" "$destination_image"
    return 0
  fi
  if [[ "$1" == image && "$2" == ls ]]; then return 0; fi
  if [[ "$1" == image && "$2" == rm ]]; then return 0; fi
  return 92
}}
bash() {{
  if [[ "$1" == -n ]]; then return 0; fi
  local script=$1 action=${{2:-}}
  if [[ "$action" == prepare ]]; then
    shift 2
    local source="" root=""
    while (($#)); do
      case "$1" in
        --source) source=$2; shift 2 ;;
        --build-root) root=$2; shift 2 ;;
        --no-cache) shift ;;
        *) return 83 ;;
      esac
    done
    local revision
    revision=$(command git -C "$source" rev-parse HEAD)
    mkdir -p "$root/venv/bin" "$root/webui"
    printf '#!/bin/sh\\nexit 0\\n' > "$root/venv/bin/python"
    chmod +x "$root/venv/bin/python"
    printf 'ready\\n' > "$root/READY"
    printf '{{}}\\n' > "$root/manifest.json"
    printf 'new webui\\n' > "$root/webui/index.html"
    write_image "mdd-sim-gateway/engine:$revision" "$revision"
    return 0
  fi
  if [[ "$action" == activate ]]; then
    shift 2
    local source="" root="" revision=""
    while (($#)); do
      case "$1" in
        --source) source=$2; shift 2 ;;
        --build-root) root=$2; shift 2 ;;
        --sha) revision=$2; shift 2 ;;
        *) return 84 ;;
      esac
    done
    ln -sfn "$root/venv" "$source/.venv.new"
    mv -Tf "$source/.venv.new" "$source/.venv"
    ln -sfn "$root/webui" "$source/webui/dist.new"
    mv -Tf "$source/webui/dist.new" "$source/webui/dist"
    printf '%s\\n' "$revision" > "$STATE_DIR/active-commit"
    write_image "$ENGINE_STABLE_IMAGE" "$revision"
    if [[ "$revision" != "$old_revision" ]]; then printf 'new-data\\n' > "$data_file"; fi
    return 0
  fi
  command bash "$@"
}}
remember_run_state() {{
  [[ $(cat "$runtime_file") == old-running ]] || return 85
  CONTROL_WAS_ACTIVE=1
  ORCHESTRATOR_WAS_ACTIVE=1
}}
stop_runtime() {{ printf 'stopped\\n' > "$runtime_file"; }}
start_runtime() {{ printf 'new-running\\n' > "$runtime_file"; }}
restore_run_state() {{ printf 'old-running\\n' > "$runtime_file"; }}
service_is_active() {{ [[ $(cat "$runtime_file") == *running ]]; }}
backup_archive() {{
  local output=$1
  mkdir -p "$(dirname "$output")"
  cp "$data_file" "$output.data"
  : > "$output"
  : > "$output.sha256"
  stop_runtime
}}
restore_archive() {{ cp "$1.data" "$data_file"; }}
health_check() {{
  local active
  active=$(cat "$STATE_DIR/active-commit")
  [[ "$active" == "$old_revision" ]]
}}
INSTALL_DIR=$1
STATE_DIR=$2
CACHE_DIR=$3
BACKUP_DIR=$4
DATA_DIR=$5
runtime_file=$6
data_file=$7
images_dir=$8
ORIGIN_URL=$9
old_revision=${{10}}
BRANCH=vmware
ENGINE_STABLE_IMAGE=mdd-sim-gateway/engine:latest
CONTROL_UNIT=mdd-sim-gateway-control.service
ORCHESTRATOR_UNIT=mdd-sim-gateway-orchestrator.service
ACTIVE_GENERATION_COMMIT=
ACTIVE_GENERATION_BUILD=
MANAGED_CHECKOUT_STATUS_KIND=
write_image "mdd-sim-gateway/engine:$old_revision" "$old_revision"
write_image "$ENGINE_STABLE_IMAGE" "$old_revision"
cmd_update --yes
"""
            result = run(
                ["bash", "-c", script, "rollback-transaction", str(fixture.repo),
                 str(fixture.state), str(fixture.cache), str(backup), str(root / "data-dir"),
                 str(runtime), str(data), str(images), str(remote), old],
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("rolled back completely", result.stderr)
            self.assertEqual(
                run(["git", "rev-parse", "HEAD"], cwd=fixture.repo).stdout.strip(), old
            )
            self.assertNotEqual(old, new)
            self.assertEqual((fixture.state / "active-commit").read_text(encoding="ascii").strip(), old)
            self.assertEqual(os.readlink(fixture.repo / ".venv"), str(fixture.build / "venv"))
            self.assertEqual(
                os.readlink(fixture.repo / "webui/dist"), str(fixture.build / "webui")
            )
            stable_key = "mdd-sim-gateway_engine_latest"
            self.assertEqual((images / stable_key).read_text(encoding="ascii").strip(), old)
            self.assertEqual(data.read_text(encoding="ascii"), "old-data\n")
            self.assertEqual(runtime.read_text(encoding="ascii"), "old-running\n")


if __name__ == "__main__":
    unittest.main()
