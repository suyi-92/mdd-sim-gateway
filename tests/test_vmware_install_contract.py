"""Static safety contracts for the VMware-only source-build installer."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.bootstrap_test_support import (
    handoff_test_tree_to_bootstrap_user,
    run_bootstrap_as_user,
)


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
MDDCTL = (ROOT / "scripts/mddctl").read_text(encoding="utf-8")
ARCHIVE = (ROOT / "scripts/mdd_archive.py").read_text(encoding="utf-8")
GITIGNORE = (ROOT / ".gitignore").read_text(encoding="utf-8")


def shell_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    next_function = source.find("\n}\n\n", start)
    if next_function < 0:
        raise AssertionError(f"could not bound shell function {name}")
    return source[start:next_function]


def run_public_bootstrap(arguments: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        bootstrap = workspace / "bootstrap.sh"
        shutil.copy2(ROOT / "bootstrap.sh", bootstrap)
        bootstrap.chmod(0o755)
        handoff_test_tree_to_bootstrap_user(workspace)
        return run_bootstrap_as_user(
            ["bash", str(bootstrap), *arguments],
            cwd=workspace,
            check=check,
        )


class BootstrapContractTests(unittest.TestCase):
    def test_stream_entry_downloads_as_user_then_invokes_a_local_root_script(self):
        self.assertIn('[[ ${EUID:-$(id -u)} -ne 0 ]]', BOOTSTRAP)
        self.assertIn('sudo -n true 2>/dev/null || sudo -v', BOOTSTRAP)
        self.assertLess(BOOTSTRAP.index("git -c advice.detachedHead=false clone"),
                        BOOTSTRAP.index('sudo -H bash "$stage/repository/install.sh"'))
        self.assertNotIn("curl | sudo", BOOTSTRAP)
        self.assertNotIn("wget | sudo", BOOTSTRAP)

    def test_update_uses_the_downloaded_transactional_manager(self):
        self.assertIn('sudo -H bash "$stage/repository/scripts/mddctl"', BOOTSTRAP)
        self.assertEqual(BOOTSTRAP.count('exec sudo mddctl "${args[@]}"'), 1)
        self.assertLess(BOOTSTRAP.index("clone --single-branch --branch vmware"),
                        BOOTSTRAP.index('sudo -H bash "$stage/repository/scripts/mddctl"'))
        self.assertIn("fsck --full --no-dangling", BOOTSTRAP)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "bootstrap sudo contract runs in Linux")
    def test_existing_nopasswd_policy_does_not_fall_through_to_sudo_v(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fakebin = root / "bin"
            fakebin.mkdir()
            log = root / "sudo.log"
            bootstrap = root / "bootstrap.sh"
            shutil.copy2(ROOT / "bootstrap.sh", bootstrap)
            (fakebin / "mddctl").write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            (fakebin / "sudo").write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >> "$SUDO_LOG"
if [ "${1:-}" = -n ] && [ "${2:-}" = true ]; then exit 0; fi
if [ "${1:-}" = -v ]; then exit 99; fi
exit 0
""",
                encoding="ascii",
            )
            for path in (bootstrap, fakebin / "mddctl", fakebin / "sudo"):
                path.chmod(0o755)
            handoff_test_tree_to_bootstrap_user(root)
            environment = {
                **os.environ,
                "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
                "SUDO_LOG": str(log),
            }

            result = run_bootstrap_as_user(
                ["bash", str(bootstrap), "doctor", "--dry-run"],
                cwd=root, check=False, env=environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertIn("-n true", calls)
            self.assertFalse(any(call == "-v" for call in calls))

    def test_downloaded_manager_uses_its_verified_archive_helper_for_bootstrap(self):
        self.assertIn('MDDCTL_SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")"', MDDCTL)
        self.assertIn('[[ -e "$MDDCTL_SOURCE_ROOT/.git"', MDDCTL)
        self.assertIn('ARCHIVE_TOOL="$MDDCTL_SOURCE_DIR/mdd_archive.py"', MDDCTL)
        self.assertIn('ARCHIVE_TOOL="$INSTALL_DIR/scripts/mdd_archive.py"', MDDCTL)

    def test_bootstrap_checkout_is_complete_for_the_local_git_transport(self):
        self.assertIn("clone --single-branch --branch vmware", BOOTSTRAP)
        self.assertNotIn("--filter=blob:none", BOOTSTRAP)
        checkout = shell_function(INSTALL, "install_source_checkout")
        self.assertIn("remote.origin.promisor", checkout)
        self.assertIn("provide a complete vmware checkout", checkout)

    def test_public_bootstrap_options_are_explicit(self):
        for value in ("install|update|doctor", "--install-dir", "--data-dir", "--ref",
                      "--require-scr-prime", "--require-cellular", "--no-start",
                      "--dry-run", "--yes"):
            self.assertIn(value, BOOTSTRAP)

    def test_ref_accepts_only_vmware_or_an_exact_commit(self):
        self.assertIn('"$ref" == vmware', BOOTSTRAP)
        self.assertIn('^[0-9a-fA-F]{40}$', BOOTSTRAP)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "bootstrap execution contract runs as an ordinary Linux user")
    def test_install_dry_run_parses_all_hardware_and_path_options_without_sudo(self):
        result = run_public_bootstrap(
            ["install", "--dry-run", "--yes",
             "--install-dir", "/opt/mdd-test", "--data-dir", "/var/lib/mdd-test",
             "--ref", "vmware", "--require-scr-prime", "--require-cellular",
             "--configure-firewall", "--no-start"],
            check=True)
        self.assertIn("dry-run: action=install", result.stdout)
        self.assertIn("require_scr_prime=1", result.stdout)
        self.assertNotIn("confirming administrator access", result.stdout)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "bootstrap execution contract runs in Linux")
    def test_invalid_ref_stops_before_any_privileged_action(self):
        result = run_public_bootstrap(
            ["install", "--dry-run", "--ref", "main"],
            check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact 40-character commit", result.stderr)


class InstallerContractTests(unittest.TestCase):
    def test_activation_ignore_rules_match_root_paths_without_directory_only_semantics(self):
        rules = GITIGNORE.splitlines()
        for value in ("/.venv", "/control/.venv", "/webui/dist"):
            self.assertIn(value, rules)
            self.assertNotIn(f"{value}/", rules)

    def test_only_the_four_x86_64_guest_targets_are_accepted(self):
        self.assertIn('ubuntu:24.04|ubuntu:26.04|debian:12|debian:13', INSTALL)
        self.assertIn('[[ $(uname -m) == x86_64 ]]', INSTALL)
        self.assertIn('systemd must be PID 1', INSTALL)

    def test_control_is_native_and_only_engine_is_built_as_an_mdd_image(self):
        self.assertFalse((ROOT / "control/Dockerfile").exists())
        self.assertIn("mdd-sim-gateway-control.service", INSTALL)
        self.assertIn('ExecStart=$source_dir/.venv/bin/python run.py', INSTALL)
        self.assertNotIn("mdd-sim-gateway/control", INSTALL)
        self.assertIn('mdd-sim-gateway/engine:$sha', INSTALL)
        self.assertNotIn("HOST_DATA_DIR", (ROOT / "control/app/engine.py").read_text(encoding="utf-8"))

    def test_backup_control_path_receives_only_installer_managed_directories(self):
        self.assertEqual(INSTALL.count("Environment=MDD_STATE_DIR=$state_dir"), 2)
        self.assertEqual(INSTALL.count("Environment=MDD_BACKUP_DIR=$backup_dir"), 2)
        self.assertIn("--state $state_dir --backup $backup_dir", INSTALL)
        worker = (ROOT / "scripts/mdd_backup_worker.py").read_text(encoding="utf-8")
        self.assertIn('[mddctl, "backup", "--output", str(archive)]', worker)
        self.assertIn('[mddctl, "restore", "--input", str(archive)]', worker)
        self.assertNotIn("shell=True", worker)

    def test_webui_and_venv_are_staged_before_atomic_symlink_switch(self):
        self.assertIn('NODE_BUILD_IMAGE="node:22.14.0-bookworm-slim@sha256:', INSTALL)
        self.assertIn("npm ci; npm run build", INSTALL)
        self.assertIn('python3 -m venv --clear "$temp/venv"', INSTALL)
        self.assertIn('mv -Tf "$source_dir/.venv.new"', INSTALL)
        self.assertIn('mv -Tf "$source_dir/webui/dist.new"', INSTALL)

    def test_active_generation_is_raw_canonical_and_identity_checked(self):
        validator = shell_function(MDDCTL, "validate_active_generation")
        update = shell_function(MDDCTL, "cmd_update")
        checkout = shell_function(INSTALL, "install_source_checkout")
        self.assertIn('raw=$(readlink -- "$link")', validator)
        self.assertIn('"$raw" == /* && "$raw" == "$expected"', validator)
        self.assertIn('canonical=$(realpath -e -- "$link"', validator)
        self.assertIn('bash "$INSTALL_DIR/install.sh" verify', validator)
        self.assertIn('docker image inspect "$ENGINE_STABLE_IMAGE"', validator)
        self.assertLess(update.index("validate_active_generation"), update.index("if ((dry_run))"))
        self.assertLess(update.index("validate_active_generation"), update.index("fetch --prune"))
        self.assertIn("install does not update an existing managed checkout", checkout)
        self.assertNotIn('fetch --no-tags "$source_dir"', checkout)
        self.assertNotIn("merge --ff-only", checkout)
        for forbidden in ("rm -f", "rm -rf", "unlink", "git clean", "reset --hard"):
            self.assertNotIn(forbidden, validator)

    def test_engine_build_is_commit_specific_and_has_tun_and_fingerprint_gates(self):
        prepare = shell_function(INSTALL, "prepare_build")
        for value in ("RUNTIME_FP=$runtime_fp", "BASE_FP=$base_fp",
                      "org.opencontainers.image.revision=$sha", "--cap-add NET_ADMIN",
                      "--device /dev/net/tun", "asterisk_modules"):
            self.assertIn(value, prepare)
        self.assertIn('-f "$source_dir/engine/Dockerfile" "$source_dir/engine"', prepare)
        self.assertTrue((ROOT / "engine/.dockerignore").exists())

    def test_fingerprint_helper_does_not_reassign_the_readonly_version(self):
        helper = shell_function(INSTALL, "engine_fingerprint")
        self.assertIn('env PCSC_VERSION="$PCSC_VERSION"', helper)
        self.assertNotIn('$(PCSC_VERSION="$PCSC_VERSION"', INSTALL)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "readonly fingerprint contract runs in a supported Linux guest")
    def test_fingerprint_helper_accepts_a_readonly_pcsc_version(self):
        helper = shell_function(INSTALL, "engine_fingerprint")
        with tempfile.TemporaryDirectory() as directory:
            script_path = Path(directory) / "tools" / "engine-fingerprint.sh"
            script_path.parent.mkdir()
            script_path.write_text(
                """#!/bin/sh
[ "$PCSC_VERSION" = 2.3.3 ] || exit 66
[ "$1" = runtime ] || exit 67
printf '%s\\n' fingerprint-ok
""", encoding="utf-8")
            script_path.chmod(0o755)
            script = helper + '\n}\nreadonly PCSC_VERSION=2.3.3\nengine_fingerprint "$1" runtime'
            result = subprocess.run(
                ["bash", "-euc", script, "fingerprint-helper", directory],
                check=True, text=True, capture_output=True)
            self.assertEqual(result.stdout.strip(), "fingerprint-ok")

    @unittest.skipIf(
        os.name == "nt" or not shutil.which("git") or not shutil.which("sh"),
        "Engine fingerprint regression requires Git and a POSIX shell",
    )
    def test_engine_fingerprint_uses_only_git_managed_worktree_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "tools").mkdir()
            (repo / "engine/patches").mkdir(parents=True)
            (repo / "engine/templates").mkdir()
            shutil.copy2(ROOT / "tools/engine-fingerprint.sh", repo / "tools")
            (repo / "engine/Dockerfile").write_text("FROM scratch\n", encoding="ascii")
            (repo / "engine/entrypoint.sh").write_text("#!/bin/sh\n", encoding="ascii")
            patch = repo / "engine/patches/managed.py"
            template = repo / "engine/templates/managed.py"
            patch.write_text("PATCH_VALUE = 1\n", encoding="ascii")
            template.write_text("TEMPLATE_VALUE = 1\n", encoding="ascii")
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=vmware", "."],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "add", "engine", "tools"], cwd=repo, check=True)

            def fingerprint(kind: str) -> str:
                result = subprocess.run(
                    ["sh", str(repo / "tools/engine-fingerprint.sh"), kind],
                    cwd=repo,
                    env={**os.environ, "PCSC_VERSION": "2.3.3"},
                    check=True,
                    text=True,
                    capture_output=True,
                )
                return result.stdout.strip()

            base_before = fingerprint("base")
            runtime_before = fingerprint("runtime")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "compileall",
                    "-q",
                    str(repo / "engine/patches"),
                    str(repo / "engine/templates"),
                ],
                check=True,
            )
            (repo / "engine/patches/generated.tmp").write_text("ignored\n", encoding="ascii")
            (repo / "engine/templates/generated.tmp").write_text("ignored\n", encoding="ascii")
            self.assertTrue(list((repo / "engine/patches").glob("__pycache__/*.pyc")))
            self.assertTrue(list((repo / "engine/templates").glob("__pycache__/*.pyc")))
            self.assertEqual(fingerprint("base"), base_before)
            self.assertEqual(fingerprint("runtime"), runtime_before)

            patch.write_text("PATCH_VALUE = 2\n", encoding="ascii")
            self.assertNotEqual(fingerprint("base"), base_before)
            self.assertEqual(fingerprint("runtime"), runtime_before)
            template.write_text("TEMPLATE_VALUE = 2\n", encoding="ascii")
            self.assertNotEqual(fingerprint("runtime"), runtime_before)

    def test_initial_install_uses_cache_unless_no_cache_is_explicit(self):
        install_action = INSTALL[INSTALL.index('case "$action" in'):]
        self.assertNotIn('[[ -f "$build_root/READY" ]] || no_cache=1', install_action)
        prepare = shell_function(INSTALL, "prepare_build")
        self.assertIn("docker build --pull --no-cache", prepare)

    def test_staged_venv_is_relocated_before_the_ready_marker(self):
        prepare = shell_function(INSTALL, "prepare_build")
        moved = prepare.index('mv "$temp" "$build_root"')
        relocated = prepare.index(
            'relocate_venv "$build_root/venv" "$temp/venv" "$build_root/venv"')
        ready = prepare.index('touch "$build_root/READY"')
        verified = prepare.index(
            'verify_prepared_build "$source_dir" "$build_root" "$sha"', ready)
        self.assertLess(moved, relocated)
        self.assertLess(relocated, ready)
        self.assertLess(ready, verified)
        self.assertIn('rm -f "$build_root/READY"', prepare)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "venv relocation contract runs in a supported Linux guest")
    def test_relocated_venv_console_scripts_remain_executable(self):
        helper = shell_function(INSTALL, "relocate_venv")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "build.tmp.123"
            final = root / "build"
            subprocess.run([shutil.which("python3") or "python3", "-m", "venv",
                            str(staging / "venv")], check=True)
            staging.rename(final)
            script = helper + '\n}\nrelocate_venv "$1" "$2" "$3"'
            subprocess.run(
                ["bash", "-euc", script, "relocate-venv", str(final / "venv"),
                 str(staging / "venv"), str(final / "venv")],
                check=True, text=True, capture_output=True)
            subprocess.run([str(final / "venv/bin/pip"), "check"], check=True,
                           text=True, capture_output=True)
            for path in (final / "venv/bin/pip", final / "venv/bin/activate",
                         final / "venv/pyvenv.cfg"):
                self.assertNotIn(str(staging / "venv"), path.read_text(encoding="utf-8"))

    def test_scr_prime_uses_native_probe_then_patch_three_only(self):
        gate = shell_function(INSTALL, "scr_prime_gate")
        patched = shell_function(INSTALL, "install_scr_prime_ccid")
        self.assertIn("lsusb -d 04d9:c001", gate)
        self.assertIn("scr_prime_pcsc_visible", gate)
        self.assertIn("03_scr_prime_reader.patch", patched)
        self.assertNotIn("01_hsic_slot_status.patch", INSTALL)
        self.assertNotIn("02_hsic_malformed_atr.patch", INSTALL)
        self.assertIn("apt-mark hold libccid", patched)
        self.assertIn("scr-prime-driver.json", patched)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "SCR Prime reader validation runs in a supported Linux shell")
    def test_optional_scr_prime_validation_accepts_a_reader_without_a_card(self):
        validator = shell_function(INSTALL, "validate_scr_prime_reader")
        script = validator + r'''
}
warn() { printf 'warning:%s\n' "$*"; }
die() { printf 'error:%s\n' "$*" >&2; exit 9; }
require_scr_prime=$1
validate_scr_prime_reader "$2"
'''
        reader_only = "Reader 0: SCR Prime CCID Reader 00 00"
        optional = subprocess.run(
            ["bash", "-c", script, "scr-reader-check", "0", reader_only],
            check=False, text=True, capture_output=True)
        required = subprocess.run(
            ["bash", "-c", script, "scr-reader-check", "1", reader_only],
            check=False, text=True, capture_output=True)
        with_atr = subprocess.run(
            ["bash", "-c", script, "scr-reader-check", "1",
             f"{reader_only}\nATR: 3B 00"],
            check=False, text=True, capture_output=True)

        self.assertEqual(optional.returncode, 0, optional.stderr)
        self.assertIn("no card ATR is available", optional.stdout)
        self.assertEqual(required.returncode, 9)
        self.assertIn("insert a SIM", required.stderr)
        self.assertEqual(with_atr.returncode, 0, with_atr.stderr)

    def test_driver_install_validates_active_source_before_touching_the_driver(self):
        command = shell_function(MDDCTL, "cmd_driver")
        install_case = command[command.index("install)"):command.index("restore)")]
        checkout = install_case.index("validate_managed_checkout")
        generation = install_case.index("validate_active_generation")
        usb = install_case.index("lsusb -d 04d9:c001")
        handoff = install_case.index('bash "$INSTALL_DIR/install.sh" driver')
        self.assertLess(checkout, generation)
        self.assertLess(generation, usb)
        self.assertLess(usb, handoff)
        self.assertIn("driver --source", install_case)

    def test_internal_driver_action_uses_only_the_active_patch_and_gate(self):
        action = INSTALL[INSTALL.index("  driver)\n"):INSTALL.index("  install)\n")]
        self.assertIn("03_scr_prime_reader.patch", action)
        self.assertIn("detect_distro", action)
        self.assertIn("scr_prime_gate", action)
        self.assertNotIn("install_packages", action)
        self.assertNotIn("install_source_checkout", action)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "driver tree hashing runs in a supported Linux shell")
    def test_driver_tree_hash_records_symlink_shape_without_following_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "ifd-ccid.bundle"
            bundle.mkdir()
            outside_a = root / "distribution-a.plist"
            outside_b = root / "distribution-b.plist"
            outside_a.write_text("first", encoding="utf-8")
            outside_b.write_text("second", encoding="utf-8")
            link = bundle / "Info.plist"
            link.symlink_to(outside_a)

            def digest(source: str) -> str:
                helper = shell_function(source, "tree_hash")
                result = subprocess.run(
                    ["bash", "-c", helper + '\n}\ntree_hash "$1"',
                     "driver-tree-hash", str(bundle)],
                    check=True, text=True, capture_output=True)
                return result.stdout.strip()

            installer_hash = digest(INSTALL)
            self.assertEqual(installer_hash, digest(MDDCTL))
            outside_a.write_text("changed but deliberately not followed", encoding="utf-8")
            self.assertEqual(installer_hash, digest(INSTALL))
            link.unlink()
            link.symlink_to(outside_b)
            self.assertNotEqual(installer_hash, digest(INSTALL))

    def test_lpac_build_gate_does_not_require_a_pcsc_reader(self):
        validator = shell_function(INSTALL, "lpac_binary_valid")
        ensure = shell_function(INSTALL, "ensure_lpac")
        self.assertIn("LPAC_APDU=stdio LPAC_HTTP=stdio", validator)
        self.assertIn('"$binary" driver list', validator)
        self.assertIn("'\"pcsc\"'", validator)
        self.assertIn("'\"curl\"'", validator)
        self.assertIn('lpac_binary_valid "$destination/lpac" && return', ensure)
        self.assertNotIn("driver apdu list", INSTALL)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "lpac validation contract runs in a supported Linux guest")
    def test_lpac_validator_forces_hardware_free_stdio_drivers(self):
        validator = shell_function(INSTALL, "lpac_binary_valid")
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "lpac"
            binary.write_text(
                """#!/bin/sh
[ "$LPAC_APDU" = stdio ] && [ "$LPAC_HTTP" = stdio ] || exit 64
[ "$1" = driver ] && [ "$2" = list ] || exit 65
printf '%s\\n' '{"LPAC_APDU":["pcsc","stdio"],"LPAC_HTTP":["curl","stdio"]}'
""", encoding="utf-8")
            binary.chmod(0o755)
            script = validator + '\n}\nlpac_binary_valid "$1"'
            subprocess.run(
                ["bash", "-c", script,
                 "lpac-validator", str(binary)], check=True)

    def test_networkmanager_policy_preserves_the_management_interface(self):
        self.assertIn("address-before.json", INSTALL)
        self.assertIn("routes-before.json", INSTALL)
        self.assertIn("match-device=except:type:gsm", INSTALL)
        self.assertIn("networkd_was_active", INSTALL)
        self.assertIn("networking_was_active", INSTALL)
        self.assertIn("restore_network_guard", INSTALL)
        self.assertIn("network_guard_cleanup", INSTALL)
        self.assertIn("default route or SSH address changed", INSTALL)
        self.assertIn("SetLogging s INFO", INSTALL)

    def test_optional_cellular_probe_does_not_enter_the_required_wait_loop(self):
        gate = shell_function(INSTALL, "cellular_gate")
        self.assertLess(gate.index("require_cellular == 0"),
                        gate.index("deadline=$((SECONDS + 90))"))
        self.assertIn("no Quectel-class modem is visible", gate)
        self.assertIn("no cellular modem detected", gate)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "cellular timing contract runs in a supported Linux guest")
    def test_optional_cellular_probe_never_sleeps(self):
        gate = shell_function(INSTALL, "cellular_gate")
        script = gate + """
}
require_cellular=0
mmcli() { return 1; }
sleep() { exit 97; }
warn() { :; }
info() { :; }
die() { exit 98; }
cellular_gate
"""
        subprocess.run(["bash", "-euc", script], check=True)

    def test_firewall_cleanup_tracks_only_rules_created_by_mdd(self):
        firewall = shell_function(INSTALL, "configure_firewall_rules")
        self.assertNotIn(': > "$state_dir/firewall-created"', firewall)
        self.assertIn('grep -Fqx "$spec" "$state_dir/firewall-created"', firewall)
        self.assertIn("firewall-nft-created", firewall)
        self.assertIn("is not recorded as MDD-owned", firewall)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "firewall return contract runs in a supported Linux guest")
    def test_print_only_firewall_path_returns_success(self):
        firewall = shell_function(INSTALL, "configure_firewall_rules")
        script = firewall + """
}
configure_firewall=0
firewall_rule_specs() { printf '8443/tcp|MDD Control\\n'; }
firewall_ports() { :; }
info() { :; }
die() { exit 98; }
configure_firewall_rules
"""
        subprocess.run(["bash", "-euc", script], check=True)

    def test_install_reports_and_diagnoses_systemd_start(self):
        install_action = INSTALL[INSTALL.index('case "$action" in'):]
        self.assertIn('info "starting Control and orchestrator services"', install_action)
        self.assertIn("systemctl --no-pager --full status", install_action)
        self.assertIn('die "could not start MDD systemd services"', install_action)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "HTTPS startup retry contract runs in a supported Linux guest")
    def test_install_https_health_retries_connection_refusal(self):
        helper = shell_function(INSTALL, "https_health_ready")
        script = helper + """
}
attempts=0
curl() { attempts=$((attempts + 1)); ((attempts >= 3)); }
sleep() { :; }
https_health_ready
[[ $attempts -eq 3 ]]
"""
        subprocess.run(["bash", "-euc", script], check=True)

    def test_no_cloud_build_or_precompiled_project_asset_is_tracked(self):
        self.assertFalse(any((ROOT / ".github/workflows").glob("*.yml")))
        self.assertFalse((ROOT / "webui/release-dist.SHA256SUMS").exists())
        self.assertFalse((ROOT / "update-policy.json").exists())
        self.assertFalse((ROOT / "engine/Dockerfile.overlay").exists())
        self.assertNotIn("filter=lfs", (ROOT / ".gitattributes").read_text(encoding="utf-8"))
        self.assertNotIn("engine-base:trusted", INSTALL + MDDCTL)


class MddctlContractTests(unittest.TestCase):
    def test_public_management_commands_are_present(self):
        for value in ("status)", "doctor)", "start)", "stop)", "restart)", "logs)",
                      "update)", "backup)", "restore)", "driver)", "uninstall)"):
            self.assertIn(value, MDDCTL)

    def test_update_is_clean_exact_remote_and_fast_forward_only(self):
        update = shell_function(MDDCTL, "cmd_update")
        checkout = shell_function(MDDCTL, "validate_managed_checkout")
        self.assertIn("remote get-url origin", checkout)
        self.assertIn("managed_checkout_status_kind", checkout)
        status = shell_function(MDDCTL, "managed_checkout_status_kind")
        self.assertIn('"--porcelain=v1", "-z"', status)
        self.assertIn('b"?? .venv", b"?? webui/dist"', status)
        self.assertIn("--path-format=absolute", MDDCTL)
        self.assertIn("validate_active_generation", update)
        self.assertIn('merge-base --is-ancestor "$old" "$new"', update)
        self.assertIn('merge --ff-only "origin/$BRANCH"', update)
        self.assertNotIn(" rebase ", update)
        self.assertNotIn("push --force", update)
        self.assertLess(update.index("backup_archive"), update.index("merge --ff-only"))
        self.assertIn('reset --hard "$UPDATE_OLD"', update)
        self.assertIn("UPDATE_TRANSACTION_ARMED=1", update)
        self.assertIn("update_cleanup", update)
        self.assertIn('current_status" == legacy-activation-links', update)
        self.assertIn("remember_run_state", update)
        self.assertIn("restore_run_state", update)

    def test_update_and_archive_transactions_have_exit_and_signal_cleanup(self):
        update = shell_function(MDDCTL, "cmd_update")
        backup = shell_function(MDDCTL, "backup_archive")
        restore = shell_function(MDDCTL, "restore_archive")
        for source, cleanup in ((update, "update_cleanup"), (backup, "backup_cleanup"),
                                (restore, "restore_cleanup")):
            self.assertIn(f"trap '{cleanup} $?' EXIT", source)
            self.assertIn("trap 'exit 130' INT", source)
            self.assertIn("trap 'exit 143' TERM", source)

    def test_mutating_health_checks_wait_but_doctor_stays_a_snapshot(self):
        health = shell_function(MDDCTL, "health_check")
        doctor = shell_function(MDDCTL, "cmd_doctor")
        wait = shell_function(MDDCTL, "https_healthy_wait")
        self.assertIn("https_healthy_wait", health)
        self.assertIn("SECONDS + 30", wait)
        self.assertIn("sleep 1", wait)
        self.assertIn("https_healthy && https=1", doctor)
        self.assertNotIn("https_healthy_wait", doctor)

    def test_driver_restore_and_reprobe_preserve_evidence_on_failure(self):
        restore = shell_function(MDDCTL, "driver_restore_internal")
        reprobe = shell_function(MDDCTL, "driver_reprobe_native")
        for source in (restore, reprobe):
            self.assertIn("metadata.json", source)
            self.assertIn("pcsc-maintenance", source)
            self.assertIn("03_scr_prime_reader.patch", MDDCTL)
        self.assertIn("hold_was_present", restore)
        self.assertIn("driver_restore_cleanup", restore)
        self.assertIn("driver_reprobe_cleanup", reprobe)

    def test_backup_restore_checks_integrity_checksum_and_paths(self):
        backup = shell_function(MDDCTL, "backup_archive")
        extract = shell_function(MDDCTL, "verify_and_extract_archive")
        restore = shell_function(MDDCTL, "restore_archive")
        self.assertIn("PRAGMA integrity_check", ARCHIVE)
        self.assertIn("PRAGMA wal_checkpoint(TRUNCATE)", ARCHIVE)
        self.assertIn("sha256sum", backup)
        self.assertIn('archive_tool create "$DATA_DIR"', backup)
        self.assertIn('archive_tool verify-extract "$input" "$destination"', extract)
        self.assertIn('".." in pure.parts', ARCHIVE)
        self.assertIn("member.issym()", ARCHIVE)
        self.assertNotIn("extractall", extract)
        self.assertIn(".pre-restore-", restore)

    def test_doctor_json_schema_never_collects_subscriber_or_message_fields(self):
        doctor = shell_function(MDDCTL, "cmd_doctor")
        for forbidden in ("imsi", "iccid", "imei", "credential", "message", "sms_text"):
            self.assertNotIn(forbidden, doctor.lower())
        self.assertIn('"scr_prime"', doctor)
        self.assertIn('"cellular"', doctor)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "Linux bash syntax gate runs in the supported guest environment")
    def test_shell_syntax(self):
        subprocess.run(["bash", "-n", str(ROOT / "bootstrap.sh"), str(ROOT / "install.sh"),
                        str(ROOT / "scripts/mddctl")], check=True)


class VersionContractTests(unittest.TestCase):
    def test_vmware_version_suffix(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, "1.7.0-vmware.3")
        for path in (ROOT / "webui/package.json", ROOT / "webui/package-lock.json"):
            self.assertIn(f'"version": "{version}"', path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
