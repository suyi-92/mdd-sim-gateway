"""Static safety contracts for the VMware-only source-build installer."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
MDDCTL = (ROOT / "scripts/mddctl").read_text(encoding="utf-8")
ARCHIVE = (ROOT / "scripts/mdd_archive.py").read_text(encoding="utf-8")


def shell_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    next_function = source.find("\n}\n\n", start)
    if next_function < 0:
        raise AssertionError(f"could not bound shell function {name}")
    return source[start:next_function]


class BootstrapContractTests(unittest.TestCase):
    def test_stream_entry_downloads_as_user_then_invokes_a_local_root_script(self):
        self.assertIn('[[ ${EUID:-$(id -u)} -ne 0 ]]', BOOTSTRAP)
        self.assertLess(BOOTSTRAP.index("git -c advice.detachedHead=false clone"),
                        BOOTSTRAP.index('sudo -H bash "$stage/repository/install.sh"'))
        self.assertNotIn("curl | sudo", BOOTSTRAP)
        self.assertNotIn("wget | sudo", BOOTSTRAP)

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
        result = subprocess.run(
            ["bash", str(ROOT / "bootstrap.sh"), "install", "--dry-run", "--yes",
             "--install-dir", "/opt/mdd-test", "--data-dir", "/var/lib/mdd-test",
             "--ref", "vmware", "--require-scr-prime", "--require-cellular",
             "--configure-firewall", "--no-start"],
            check=True, text=True, capture_output=True)
        self.assertIn("dry-run: action=install", result.stdout)
        self.assertIn("require_scr_prime=1", result.stdout)
        self.assertNotIn("confirming administrator access", result.stdout)

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"),
                     "bootstrap execution contract runs in Linux")
    def test_invalid_ref_stops_before_any_privileged_action(self):
        result = subprocess.run(
            ["bash", str(ROOT / "bootstrap.sh"), "install", "--dry-run", "--ref", "main"],
            text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact 40-character commit", result.stderr)


class InstallerContractTests(unittest.TestCase):
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

    def test_webui_and_venv_are_staged_before_atomic_symlink_switch(self):
        self.assertIn('NODE_BUILD_IMAGE="node:22.14.0-bookworm-slim@sha256:', INSTALL)
        self.assertIn("npm ci; npm run build", INSTALL)
        self.assertIn('python3 -m venv --clear "$temp/venv"', INSTALL)
        self.assertIn('mv -Tf "$source_dir/.venv.new"', INSTALL)
        self.assertIn('mv -Tf "$source_dir/webui/dist.new"', INSTALL)

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
        self.assertIn("status --porcelain --untracked-files=normal", checkout)
        self.assertIn('merge-base --is-ancestor "$old" "$new"', update)
        self.assertIn('merge --ff-only "origin/$BRANCH"', update)
        self.assertNotIn(" rebase ", update)
        self.assertNotIn("push --force", update)
        self.assertLess(update.index("backup_archive"), update.index("merge --ff-only"))
        self.assertIn('reset --hard "$old"', update)
        self.assertIn("transaction_armed=1", update)
        self.assertIn("update_cleanup", update)
        self.assertIn('install.sh" verify', update)

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
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(),
                         "1.7.0-vmware.1")


if __name__ == "__main__":
    unittest.main()
