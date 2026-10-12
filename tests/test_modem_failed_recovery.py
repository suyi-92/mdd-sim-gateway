import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from control.app import device_state
from host import mdd_orchestrator
from host.mdd_orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[1]
MODEM = {"id": "a", "tty": "/dev/ttyUSB2"}
WANTED = {"a": {"cellular_enabled": True, "flight_mode": False, "vowifi_enabled": True}}


class FailedModemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Orchestrator(Path(self.temp.name) / "data", Path(self.temp.name))
        self.app.root.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def apply(self, snapshot, wanted=WANTED, reboot_error=None):
        calls = []
        stub = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(self.app, "modemmanager_modem_for_tty", return_value="/mm/0"), \
                patch.object(self.app, "modem_snapshot", return_value=dict(snapshot)), \
                patch.object(self.app, "reboot_modem", side_effect=reboot_error, create=True) as reboot, \
                patch("host.mdd_orchestrator.serial", SimpleNamespace()), \
                patch("host.mdd_orchestrator.run",
                      side_effect=lambda args, **k: calls.append(args) or stub):
            self.app.apply_device_radios([MODEM], wanted, through_modemmanager=True)
        return calls, reboot

    def failed(self, reason="unknown-capabilities"):
        return {"available": True, "state": "failed", "failed_reason": reason,
                "radio_enabled": False, "data_active": False}

    def test_a_failed_modem_is_not_asked_to_enable_again(self):
        calls, _reboot = self.apply(self.failed())
        self.assertFalse(any("--enable" in args for args in calls))
        state = self.app.cellular_states["a"]
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["failure"], {"reason": "unknown-capabilities",
                                            "resettable": False, "resets": 0, "rebooted": 0,
                                            "exhausted": False, "recovery": "manual_model_verification"})

    def test_the_module_is_rebooted_after_a_grace_period_spaced_out_and_bounded(self):
        for _ in range(10):
            calls, reboot = self.apply(self.failed())
            reboot.assert_not_called()
            self.assertFalse(any("--enable" in args or any("CFUN" in arg for arg in args) for args in calls))
        self.assertEqual(self.app.cellular_states["a"]["failure"]["resets"], 0)

    def test_flight_mode_records_the_failure_but_never_reboots(self):
        for flight in (True, False, True):
            _, reboot = self.apply(self.failed(), wanted={'a': {**WANTED['a'], 'flight_mode': flight}})
            reboot.assert_not_called()
            self.assertFalse(self.app.modem_failure('a')['resettable'])

    def test_reboots_that_could_not_be_sent_are_told_apart(self):
        for _ in range(10):
            calls, reboot = self.apply(self.failed())
            reboot.assert_not_called()
            self.assertFalse(any("--enable" in args or any("CFUN" in arg for arg in args) for args in calls))
        self.assertEqual(self.app.cellular_states["a"]["failure"]["resets"], 0)

    def test_the_next_reboot_waits_for_the_backoff(self):
        for _ in range(10):
            calls, reboot = self.apply(self.failed())
            reboot.assert_not_called()
            self.assertFalse(any("--enable" in args or any("CFUN" in arg for arg in args) for args in calls))
        self.assertEqual(self.app.cellular_states["a"]["failure"]["resets"], 0)

    def test_a_missing_sim_is_reported_but_never_rebooted(self):
        _, reboot = self.apply(self.failed('sim-missing'))
        reboot.assert_not_called()
        self.assertEqual(self.app.modem_failure('a')['reason'], 'sim-missing')

    def test_recovery_clears_the_record_and_enables_the_radio_again(self):
        self.apply(self.failed())
        calls, _reboot = self.apply({"available": True, "state": "disabled",
                                     "radio_enabled": False, "data_active": False})
        self.assertNotIn("a", self.app._modem_failed)
        self.assertIn(["mmcli", "-m", "/mm/0", "--enable"], calls)

    def test_a_failed_modem_is_a_settled_status_and_leaves_vowifi_alone(self):
        self.apply(self.failed())
        self.app.bridges["a"] = SimpleNamespace(poll=lambda: None, pid=9)
        self.app._bridge_started["a"] = time.time() - 10
        with patch.object(self.app, "service_active", return_value=True), \
                patch.object(self.app, "modemmanager_modem_for_tty", return_value="/mm/0"):
            self.app.publish_device_status(WANTED, {"a": {**MODEM, "name": "A"}})
        device = device_state._read(str(self.app.device_status_path), {})["devices"]["a"]
        self.assertFalse(device["transitioning"])
        self.assertEqual(device["error"], "")
        self.assertTrue(device["actual"]["vowifi_bridge_active"])
        self.assertEqual(device["cellular"]["failure"]["reason"], "unknown-capabilities")

    def test_the_failed_reason_is_read_from_modemmanager(self):
        text = ("modem.generic.state                 : failed\n"
                "modem.generic.state-failed-reason   : unknown-capabilities\n"
                "modem.generic.power-state           : on\n")
        with patch.object(self.app, "modemmanager_modem_for_tty", return_value="/mm/0"), \
                patch("host.mdd_orchestrator.run",
                      return_value=SimpleNamespace(returncode=0, stdout=text, stderr="")):
            snapshot = self.app.modem_snapshot(MODEM)
        self.assertEqual((snapshot["state"], snapshot["failed_reason"], snapshot["radio_enabled"]),
                         ("failed", "unknown-capabilities", False))

    def test_detach_and_generation_change_forget_old_failure(self):
        self.app.recover_failed_modem({**MODEM, 'usb_generation': 'old'}, 'unknown')
        old = self.app._modem_failed['a']
        self.app.recover_failed_modem({**MODEM, 'usb_generation': 'new'}, 'unknown')
        self.assertIsNot(old, self.app._modem_failed['a'])
        self.assertEqual(self.app._modem_failed['a']['generation'], 'new')
        self.app.forget_absent_modem_failures(set())
        self.assertEqual(self.app._modem_failed, {})


class FailedModemWordingTests(unittest.TestCase):
    def test_every_failed_modem_reason_is_translated(self):
        source = (ROOT / "control" / "app" / "main.py").read_text(encoding="utf-8")
        start = source.index('elif host_cell.get("state") == "failed":')
        block = source[start:source.index("elif radio_on and registered", start)]
        sentences = ["".join(re.findall(r'"([^"]*)"', part))
                     for part in re.split(r"\bif\b|\belse\b", block.split("cell_reason = (")[1])]
        sentences = [s for s in sentences if s.startswith("ModemManager")]
        self.assertEqual(len(sentences), 2)
        i18n = (ROOT / "webui" / "src" / "i18n.jsx").read_text(encoding="utf-8")
        zh = i18n[i18n.index("const zh"):i18n.index("const en")]
        for sentence in sentences:
            self.assertIn(f"'{sentence}'", zh)


if __name__ == "__main__":
    unittest.main()
