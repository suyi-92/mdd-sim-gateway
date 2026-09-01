"""Static safety contracts for the VMware-only source-build installer."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
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

    def test_firewall_cleanup_tracks_only_rules_created_by_mdd(self):
        firewall = shell_function(INSTALL, "configure_firewall_rules")
        self.assertNotIn(': > "$state_dir/firewall-created"', firewall)
        self.assertIn('grep -Fqx "$spec" "$state_dir/firewall-created"', firewall)
        self.assertIn("firewall-nft-created", firewall)
        self.assertIn("is not recorded as MDD-owned", firewall)

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
