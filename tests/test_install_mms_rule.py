"""install.sh: the udev rule that releases a Quectel module's secondary AT port for MMS.

The shell functions are cut out of install.sh and run against a temporary rules directory,
with udevadm and systemctl replaced by stubs that record how they were called.
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = (ROOT / "scripts/mdd_mms_ports.sh").read_text(encoding="utf-8")


def shell_function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", INSTALL, re.M | re.S)
    assert match, name
    return match.group(0)


class InstallMmsRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.rules = root / "rules.d"
        self.rules.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        for tool in ("udevadm", "systemctl"):
            stub = self.bin / tool
            stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{self.log}"\nexit 0\n')
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.temp.cleanup()

    def run_installer(self, *calls: str, rules_dir: Path | None = None) -> list[str]:
        self.log.write_text("")
        script = "\n".join([
            "set -e",
            'have() { command -v "$1" >/dev/null 2>&1; }',
            "info() { :; }",
            "warn() { :; }",
            re.search(r"^MMS_AT_PORT_RULE=.*$", INSTALL, re.M).group(0),
            shell_function("mms_at_port_rule_owned"),
            shell_function("mark_mms_at_port_rule_pending"),
            shell_function("reapply_modem_port_rules"),
            shell_function("ensure_mms_at_port_rule"),
            shell_function("remove_mms_at_port_rule"),
            shell_function("snapshot_mms_at_port_rule"),
            shell_function("restore_mms_at_port_rule"),
            *calls,
        ])
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
               "MDD_UDEV_RULES_DIR": str(rules_dir or self.rules)}
        subprocess.run(["bash", "-c", script], check=True, env=env)
        return [line for line in self.log.read_text().splitlines() if line]

    @property
    def rule(self) -> Path:
        return self.rules / "78-mdd-mms-at-port.rules"

    def test_install_writes_the_rule_and_reapplies_it(self):
        calls = self.run_installer("ensure_mms_at_port_rule")
        text = self.rule.read_text()
        self.assertIn('ATTRS{idVendor}=="2c7c"', text)
        self.assertIn('ENV{ID_MM_PORT_TYPE_AT_SECONDARY}=="1"', text)
        self.assertIn('ENV{ID_MM_PORT_IGNORE}="1"', text)
        self.assertNotIn("bInterfaceNumber", text, "never keyed on an interface number")
        self.assertNotIn("ID_MM_PORT_TYPE_AT_PRIMARY", text)
        self.assertEqual(oct(self.rule.stat().st_mode & 0o777), "0o644")
        self.assertIn("udevadm trigger --action=change --subsystem-match=tty", calls)
        self.assertIn("systemctl restart ModemManager.service", calls)

    def test_reinstall_with_an_unchanged_rule_touches_nothing(self):
        self.run_installer("ensure_mms_at_port_rule")
        self.assertEqual(self.run_installer("ensure_mms_at_port_rule"), [])

    def test_a_changed_rule_is_rewritten_and_reapplied(self):
        self.rule.write_text("# MDD Sim Gateway: an earlier, interface-number based rule\n")
        calls = self.run_installer("ensure_mms_at_port_rule")
        self.assertIn("ID_MM_PORT_TYPE_AT_SECONDARY", self.rule.read_text())
        self.assertIn("systemctl restart ModemManager.service", calls)

    def test_uninstall_removes_the_rule_and_returns_the_port(self):
        self.run_installer("ensure_mms_at_port_rule")
        calls = self.run_installer("remove_mms_at_port_rule")
        self.assertFalse(self.rule.exists())
        # Without a change event the udev database would keep ID_MM_PORT_IGNORE until reboot.
        self.assertIn("udevadm trigger --action=change --subsystem-match=tty", calls)
        self.assertIn("systemctl restart ModemManager.service", calls)
        self.assertEqual(self.run_installer("remove_mms_at_port_rule"), [],
                         "uninstalling twice is a no-op")

    def test_symlink_cannot_redirect_rule_installation(self):
        victim = Path(self.temp.name) / "unrelated-rule"
        victim.write_text("do-not-change")
        self.rule.symlink_to(victim)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_installer("ensure_mms_at_port_rule")
        self.assertEqual(victim.read_text(), "do-not-change")

    def test_an_unowned_existing_rule_is_neither_overwritten_nor_removed(self):
        self.rule.write_text("# administrator rule\n")
        for operation in ("ensure_mms_at_port_rule", "remove_mms_at_port_rule"):
            with self.subTest(operation=operation), self.assertRaises(subprocess.CalledProcessError):
                self.run_installer(operation)
            self.assertEqual(self.rule.read_text(), "# administrator rule\n")

    def test_restart_failure_propagates_to_the_update_transaction(self):
        systemctl = self.bin / "systemctl"
        systemctl.write_text('#!/bin/sh\n[ "$1" != restart ]\n')
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_installer("ensure_mms_at_port_rule")

    def test_first_install_udev_failure_retries_same_rule_then_becomes_a_noop(self):
        udev = self.bin / "udevadm"
        udev.write_text('#!/bin/sh\n[ "$1" != trigger ]\n')
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_installer("ensure_mms_at_port_rule")
        pending = Path(str(self.rule) + ".pending")
        self.assertTrue(self.rule.is_file())
        self.assertTrue(pending.is_file())
        udev.write_text(f'#!/bin/sh\necho "udevadm $*" >> "{self.log}"\nexit 0\n')
        calls = self.run_installer("ensure_mms_at_port_rule")
        self.assertIn("udevadm trigger --action=change --subsystem-match=tty", calls)
        self.assertFalse(pending.exists())
        self.assertEqual(self.run_installer("ensure_mms_at_port_rule"), [])

    def test_pending_marker_ownership_is_checked_before_install_remove_or_restore(self):
        pending = Path(str(self.rule) + ".pending")
        victim = Path(self.temp.name) / "unrelated"
        victim.write_text("unchanged")
        for kind in ("unknown", "symlink"):
            if kind == "unknown": pending.write_text("not-managed")
            else: pending.symlink_to(victim)
            for action in ("ensure_mms_at_port_rule", "remove_mms_at_port_rule",
                           "restore_mms_at_port_rule /not-used"):
                with self.subTest(kind=kind, action=action), self.assertRaises(subprocess.CalledProcessError):
                    self.run_installer(action)
            pending.unlink()
        self.assertEqual(victim.read_text(), "unchanged")

    def test_restore_identical_rule_with_pending_reapplies_and_clears_marker(self):
        self.run_installer("ensure_mms_at_port_rule")
        backup = Path(self.temp.name) / "backup"
        calls = self.run_installer(f'snapshot_mms_at_port_rule "{backup}"',
                                   "mark_mms_at_port_rule_pending",
                                   f'restore_mms_at_port_rule "{backup}"')
        self.assertIn("udevadm trigger --action=change --subsystem-match=tty", calls)
        self.assertFalse(Path(str(self.rule) + ".pending").exists())

    def test_remove_retries_pending_properties_even_if_rule_is_already_absent(self):
        self.run_installer("mark_mms_at_port_rule_pending")
        calls = self.run_installer("remove_mms_at_port_rule")
        self.assertIn("udevadm trigger --action=change --subsystem-match=tty", calls)
        self.assertFalse(Path(str(self.rule) + ".pending").exists())

    def test_host_without_udev_rules_directory_is_left_alone(self):
        missing = self.rules / "absent"
        self.assertEqual(self.run_installer("ensure_mms_at_port_rule", rules_dir=missing), [])
        self.assertFalse(missing.exists())

    def test_uninstall_command_uses_the_removal(self):
        body = (ROOT / "scripts/mddctl").read_text(encoding="utf-8")
        self.assertIn("remove_mms_at_port_rule", body)


class NativeMmsPortTransactionTests(unittest.TestCase):
    def run_change(self, mode, *, existing=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts/mdd_mms_ports.sh").write_text(INSTALL)
            (root / "rules").mkdir()
            (root / "bin").mkdir()
            rule = root / "rules/78-mdd-mms-at-port.rules"
            previous = "# MDD Sim Gateway: previous verified rule\n"
            if existing:
                rule.write_text(previous)
            stubs = {
                "cat": '\n'.join([
                    'if [ "$TEST_MODE" = cat-fail ]; then printf "partial rule"; exit 7; fi',
                    'exec /usr/bin/cat "$@"']),
                "chmod": '\n'.join([
                    'if [ "$TEST_MODE" = fail ] && [ "$1" = 0644 ]; then exit 7; fi',
                    'exec /usr/bin/chmod "$@"']),
                "mv": '\n'.join([
                    'if [ "$TEST_MODE" = publish-fail ]; then exit 7; fi',
                    '/usr/bin/mv "$@" || exit $?',
                    'if [ "$TEST_MODE" = signal ] && [ "$4" = "$MDD_UDEV_RULES_DIR/78-mdd-mms-at-port.rules" ]; then kill -TERM "$PPID"; fi']),
                "udevadm": '\n'.join([
                    'printf "udevadm %s\\n" "$*" >> "$TEST_CALLS"',
                    'if [ "$TEST_MODE" = "udev-$1" ] && [ ! -f "$TEST_FAILED" ]; then',
                    '  touch "$TEST_FAILED"; exit 7',
                    'fi',
                    'if [ "$TEST_MODE" = udev-persistent ] && [ "$1" = trigger ]; then exit 7; fi']),
                "systemctl": 'printf "systemctl %s\\n" "$*" >> "$TEST_CALLS"\nexit 0',
            }
            for name, body in stubs.items():
                stub = root / "bin" / name
                stub.write_text("#!/bin/sh\n" + body + "\nexit 0\n")
                stub.chmod(0o755)
            state = root / "service-state"
            state.write_text("running")
            calls = root / "calls"
            calls.touch()
            manager = (ROOT / "scripts/mddctl").read_text()
            function = re.search(r"^cmd_mms_port\(\) \{\n.*?^\}\n", manager,
                                 re.M | re.S).group(0)
            script = '''set -Eeuo pipefail
info() { :; }
warn() { echo "$*" >&2; }
die() { echo "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
acquire_lock() { :; }
validate_managed_checkout() { :; }
validate_active_generation() { :; }
remember_run_state() { :; }
stop_runtime() { printf stopped > "$TEST_STATE"; }
restore_run_state() { printf running > "$TEST_STATE"; }
''' + function + '\ncmd_mms_port install\n'
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                  env={**os.environ, "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
                                       "INSTALL_DIR": str(root), "CACHE_DIR": str(root / "cache"),
                                       "MDD_UDEV_RULES_DIR": str(root / "rules"),
                                       "TEST_STATE": str(state), "TEST_MODE": mode,
                                       "TEST_CALLS": str(calls), "TEST_FAILED": str(root / "failed-once")})
            return (done, state.read_text(), rule.read_text() if rule.exists() else None,
                    calls.read_text().splitlines(), list((root / "rules").glob("*.tmp.*")))

    def test_success_restores_the_previous_service_state(self):
        done, state, rule, _calls, leftovers = self.run_change("success")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(state, "running")
        self.assertIn("ID_MM_PORT_IGNORE", rule)
        self.assertEqual(leftovers, [])

    def test_failed_rule_generation_or_publication_preserves_the_previous_rule(self):
        for mode in ("cat-fail", "fail", "publish-fail"):
            with self.subTest(mode=mode):
                done, state, rule, calls, leftovers = self.run_change(mode, existing=True)
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(state, "running")
                self.assertEqual(rule, "# MDD Sim Gateway: previous verified rule\n")
                self.assertEqual(calls, [], "incomplete rules must never reach udev or ModemManager")
                self.assertEqual(leftovers, [])

    def test_failed_write_restores_the_previous_service_state_and_absent_rule(self):
        done, state, rule, _calls, _leftovers = self.run_change("fail")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(state, "running")
        self.assertIsNone(rule)

    def test_udev_reload_trigger_or_settle_failure_rolls_back_and_reapplies_old_rule(self):
        for mode in ("udev-control", "udev-trigger", "udev-settle"):
            with self.subTest(mode=mode):
                done, state, rule, calls, leftovers = self.run_change(mode, existing=True)
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(state, "running")
                self.assertEqual(rule, "# MDD Sim Gateway: previous verified rule\n")
                self.assertEqual(calls.count("systemctl restart ModemManager.service"), 1,
                                 "only successful rollback may restart ModemManager")
                self.assertEqual(calls[-4:], ["udevadm trigger --action=change --subsystem-match=tty",
                                              "udevadm settle --timeout=10",
                                              "systemctl is-active ModemManager.service",
                                              "systemctl restart ModemManager.service"])
                self.assertEqual(leftovers, [])

    def test_persistently_failed_udev_rollback_keeps_services_stopped(self):
        done, state, rule, calls, _leftovers = self.run_change("udev-persistent")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(state, "stopped")
        self.assertIsNone(rule)
        self.assertIn("MMS rule rollback failed", done.stderr)
        self.assertNotIn("systemctl restart ModemManager.service", calls)

    def test_termination_after_rule_write_rolls_back_the_rule_and_services(self):
        done, state, rule, _calls, _leftovers = self.run_change("signal")
        self.assertEqual(done.returncode, 143, done.stderr)
        self.assertEqual(state, "running")
        self.assertIsNone(rule)


if __name__ == "__main__":
    unittest.main()
