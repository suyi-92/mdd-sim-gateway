"""Dynamic safety tests for managed active-generation validation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
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
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=GIT_ENV,
        check=check,
        text=True,
        input=input_text,
        capture_output=True,
    )


VERIFY_INSTALLER = r"""#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${1:-} == verify ]] || exit 81
shift
source_dir="" build_root="" sha=""
while (($#)); do
  case "$1" in
    --source) source_dir=$2; shift 2 ;;
    --build-root) build_root=$2; shift 2 ;;
    --sha) sha=$2; shift 2 ;;
    *) exit 82 ;;
  esac
done
[[ $(git -C "$source_dir" rev-parse HEAD) == "$sha" ]]
[[ -f "$build_root/READY" && -f "$build_root/manifest.json" ]]
[[ -x "$build_root/venv/bin/python" && -f "$build_root/venv/identity" ]]
[[ -f "$build_root/webui/index.html" && -f "$build_root/engine-revision" ]]
python3 - "$build_root" "$sha" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
sha = sys.argv[2]
value = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
webui = (root / "webui/index.html").read_bytes()
venv = (root / "venv/identity").read_bytes()
assert value == {
    "source_commit": sha,
    "webui_sha256": hashlib.sha256(webui).hexdigest(),
    "venv_sha256": hashlib.sha256(venv).hexdigest(),
    "engine_revision": (root / "engine-revision").read_text(encoding="ascii").strip(),
}
assert value["engine_revision"] == sha
PY
"""


class ActiveGenerationFixture:
    def __init__(self, root: Path, *, legacy_ignore: bool = False):
        self.root = root
        self.repo = root / "managed"
        self.state = root / "state"
        self.cache = root / "cache"
        self.service_state = root / "service-state"
        self.stable_revision = root / "stable-revision"
        self.data_state = root / "data-state"
        self.repo.mkdir()
        self.state.mkdir()
        run(["git", "init", "--initial-branch=vmware", "."], cwd=self.repo)
        run(["git", "remote", "add", "origin", ORIGIN_URL], cwd=self.repo)
        ignore = ".venv/\ncontrol/.venv/\nwebui/dist/\n" if legacy_ignore else (
            "/.venv\n/control/.venv\n/webui/dist\n"
        )
        (self.repo / ".gitignore").write_text(ignore, encoding="utf-8")
        (self.repo / "install.sh").write_text(VERIFY_INSTALLER, encoding="utf-8")
        (self.repo / "VERSION").write_text("1.7.0-vmware.1\n", encoding="ascii")
        (self.repo / "webui").mkdir()
        (self.repo / "webui/tracked.txt").write_text("tracked\n", encoding="utf-8")
        (self.repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        run(["git", "add", "."], cwd=self.repo)
        run(["git", "commit", "-m", "managed generation"], cwd=self.repo)
        self.sha = run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        (self.state / "active-commit").write_text(f"{self.sha}\n", encoding="ascii")
        self.build = self.make_build(self.sha)
        os.symlink(self.build / "venv", self.repo / ".venv")
        os.symlink(self.build / "webui", self.repo / "webui/dist")
        self.service_state.write_text("running-old\n", encoding="ascii")
        self.stable_revision.write_text(f"{self.sha}\n", encoding="ascii")
        self.data_state.write_text("old-data\n", encoding="ascii")

    def make_build(self, commit: str) -> Path:
        build = self.cache / "builds" / commit
        (build / "venv/bin").mkdir(parents=True)
        (build / "webui").mkdir()
        (build / "READY").write_text("ready\n", encoding="ascii")
        python = build / "venv/bin/python"
        python.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        python.chmod(0o755)
        venv_identity = b"verified-venv\n"
        webui = b"verified-webui\n"
        (build / "venv/identity").write_bytes(venv_identity)
        (build / "webui/index.html").write_bytes(webui)
        (build / "engine-revision").write_text(f"{commit}\n", encoding="ascii")
        manifest = {
            "source_commit": commit,
            "webui_sha256": hashlib.sha256(webui).hexdigest(),
            "venv_sha256": hashlib.sha256(venv_identity).hexdigest(),
            "engine_revision": commit,
        }
        (build / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )
        return build

    def link(self, component: str) -> Path:
        return self.repo / (".venv" if component == "venv" else "webui/dist")

    @staticmethod
    def path_identity(path: Path) -> tuple[object, ...]:
        if not os.path.lexists(path):
            return ("missing",)
        metadata = path.lstat()
        kind = stat.S_IFMT(metadata.st_mode)
        target = os.readlink(path) if path.is_symlink() else None
        return (kind, metadata.st_dev, metadata.st_ino, target)

    def snapshot(self) -> tuple[object, ...]:
        return (
            run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip(),
            (self.state / "active-commit").read_bytes(),
            self.path_identity(self.repo / ".venv"),
            self.path_identity(self.repo / "webui/dist"),
            self.service_state.read_bytes(),
            self.stable_revision.read_bytes(),
            self.data_state.read_bytes(),
        )

    def validation_result(self) -> subprocess.CompletedProcess[str]:
        functions = "\n\n".join(
            shell_function(MDDCTL, name)
            for name in (
                "git_operation_in_progress",
                "managed_checkout_status_kind",
                "validate_managed_checkout",
                "validate_active_generation",
            )
        )
        script = f"""
set -Eeuo pipefail
{functions}
die() {{ printf '%s\\n' "$*" >&2; exit 1; }}
docker() {{
  [[ "$1" == image && "$2" == inspect && "$3" == "$ENGINE_STABLE_IMAGE" ]] || return 91
  cat "$stable_revision_file"
}}
INSTALL_DIR=$1
STATE_DIR=$2
CACHE_DIR=$3
stable_revision_file=$4
ORIGIN_URL={ORIGIN_URL!r}
BRANCH=vmware
ENGINE_STABLE_IMAGE=mdd-sim-gateway/engine:latest
ACTIVE_GENERATION_COMMIT=
ACTIVE_GENERATION_BUILD=
MANAGED_CHECKOUT_STATUS_KIND=
validate_managed_checkout
validate_active_generation
printf '%s|%s|%s\\n' "$MANAGED_CHECKOUT_STATUS_KIND" "$ACTIVE_GENERATION_COMMIT" "$ACTIVE_GENERATION_BUILD"
"""
        return run(
            ["bash", "-c", script, "active-generation", str(self.repo), str(self.state),
             str(self.cache), str(self.stable_revision)],
            check=False,
        )


@unittest.skipIf(os.name == "nt" or not shutil.which("bash") or not shutil.which("git"),
                 "active-generation tests require Bash and Git")
class ActiveGenerationValidationTests(unittest.TestCase):
    def test_root_ignore_rules_cover_directories_and_symlinks_without_hiding_nested_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            targets = root / "targets"
            repo.mkdir()
            targets.mkdir()
            run(["git", "init", "--initial-branch=vmware", "."], cwd=repo)
            shutil.copyfile(ROOT / ".gitignore", repo / ".gitignore")
            (repo / "webui").mkdir()
            (repo / "control").mkdir()
            (repo / "webui/tracked.txt").write_text("tracked\n", encoding="utf-8")
            (repo / "control/tracked.txt").write_text("tracked\n", encoding="utf-8")
            run(["git", "add", "."], cwd=repo)
            run(["git", "commit", "-m", "ignore contract"], cwd=repo)
            paths = (repo / ".venv", repo / "webui/dist", repo / "control/.venv")
            for path in paths:
                path.mkdir(parents=True)
                (path / "artifact").write_text("ignored\n", encoding="utf-8")
            self.assertEqual(
                run(["git", "status", "--porcelain=v1"], cwd=repo).stdout, ""
            )
            for index, path in enumerate(paths):
                shutil.rmtree(path)
                target = targets / str(index)
                target.mkdir()
                os.symlink(target, path)
            self.assertEqual(
                run(["git", "status", "--porcelain=v1"], cwd=repo).stdout, ""
            )
            nested = repo / "nested/.venv"
            nested.mkdir(parents=True)
            (nested / "visible").write_text("visible\n", encoding="utf-8")
            self.assertIn("?? nested/", run(["git", "status", "--porcelain=v1"], cwd=repo).stdout)

    def assert_rejected_unchanged(self, fixture: ActiveGenerationFixture) -> str:
        before = fixture.snapshot()
        result = fixture.validation_result()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(fixture.snapshot(), before)
        return result.stderr

    def test_valid_fixed_and_legacy_activation_links_pass(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                fixture = ActiveGenerationFixture(Path(directory), legacy_ignore=legacy)
                result = fixture.validation_result()
                self.assertEqual(result.returncode, 0, result.stderr)
                expected_kind = "legacy-activation-links" if legacy else "clean"
                self.assertTrue(result.stdout.startswith(f"{expected_kind}|{fixture.sha}|"))

    def test_each_link_rejects_missing_directory_dangling_relative_outside_and_other_commit(self):
        mutations = ("missing", "directory", "dangling", "relative", "outside", "other-commit")
        for component in ("venv", "webui"):
            for mutation in mutations:
                with self.subTest(component=component, mutation=mutation), \
                     tempfile.TemporaryDirectory() as directory:
                    fixture = ActiveGenerationFixture(Path(directory))
                    link = fixture.link(component)
                    link.unlink()
                    expected = fixture.build / component
                    if mutation == "directory":
                        link.mkdir()
                    elif mutation == "dangling":
                        shutil.rmtree(expected)
                        os.symlink(expected, link)
                    elif mutation == "relative":
                        os.symlink(os.path.relpath(expected, link.parent), link)
                    elif mutation == "outside":
                        outside = fixture.root / f"outside-{component}"
                        outside.mkdir()
                        os.symlink(outside, link)
                    elif mutation == "other-commit":
                        other = fixture.make_build("f" * 40)
                        os.symlink(other / component, link)
                    self.assert_rejected_unchanged(fixture)

    def test_links_from_different_commits_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ActiveGenerationFixture(Path(directory))
            other = fixture.make_build("e" * 40)
            fixture.link("webui").unlink()
            os.symlink(other / "webui", fixture.link("webui"))
            self.assert_rejected_unchanged(fixture)

    def test_build_and_engine_identity_failures_are_rejected(self):
        mutations = ("READY", "manifest", "venv", "webui", "engine", "stable-engine")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                fixture = ActiveGenerationFixture(Path(directory))
                if mutation == "READY":
                    (fixture.build / "READY").unlink()
                elif mutation == "manifest":
                    (fixture.build / "manifest.json").write_text("{}\n", encoding="utf-8")
                elif mutation == "venv":
                    (fixture.build / "venv/identity").write_text("changed\n", encoding="utf-8")
                elif mutation == "webui":
                    (fixture.build / "webui/index.html").write_text("changed\n", encoding="utf-8")
                elif mutation == "engine":
                    (fixture.build / "engine-revision").write_text("d" * 40 + "\n", encoding="ascii")
                else:
                    fixture.stable_revision.write_text("d" * 40 + "\n", encoding="ascii")
                self.assert_rejected_unchanged(fixture)

    def test_active_commit_must_be_exact_head(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ActiveGenerationFixture(Path(directory))
            (fixture.state / "active-commit").write_text("c" * 40 + "\n", encoding="ascii")
            self.assert_rejected_unchanged(fixture)

    def test_staged_tracked_extra_untracked_and_conflicted_index_are_rejected(self):
        for mutation in ("tracked", "staged", "untracked", "conflict"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                fixture = ActiveGenerationFixture(Path(directory))
                if mutation in ("tracked", "staged"):
                    (fixture.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
                    if mutation == "staged":
                        run(["git", "add", "tracked.txt"], cwd=fixture.repo)
                elif mutation == "untracked":
                    (fixture.repo / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
                else:
                    blob = run(["git", "hash-object", "tracked.txt"], cwd=fixture.repo).stdout.strip()
                    run(["git", "update-index", "--force-remove", "tracked.txt"], cwd=fixture.repo)
                    entries = "".join(
                        f"100644 {blob} {stage}\ttracked.txt\n" for stage in (1, 2, 3)
                    )
                    run(["git", "update-index", "--index-info"], cwd=fixture.repo, input_text=entries)
                self.assert_rejected_unchanged(fixture)


if __name__ == "__main__":
    unittest.main()
