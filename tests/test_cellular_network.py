import asyncio
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from control.app import cellular_network, main


MODEM = "/org/freedesktop/ModemManager1/Modem/7"


class CellularNetworkCommandTests(unittest.TestCase):
    @staticmethod
    def reply(stdout="", stderr="", returncode=0):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def metadata(self, plugin="quectel", ports=None):
        return self.reply(json.dumps({"modem": {"generic": {
            "plugin": plugin, "ports": ports if ports is not None else [
                "cdc-wdm7 (qmi)", "ttyUSB7 (at)", "wwan7 (net)"],
        }}}))

    def cops_reply(self, value):
        return self.reply(json.dumps({"type": "s", "data": [value]}))

    def transaction(self, scan_reply, selection="+COPS: 0", restore_reply=None):
        return [self.metadata(), self.cops_reply(selection), self.cops_reply(""),
                scan_reply, restore_reply if restore_reply is not None else self.cops_reply("")]

    def test_scan_parser_merges_technologies_and_orders_current_first(self):
        output = """  ---------------------
  3GPP scan | networks: 46001 - CHN-UNICOM (lte, current)
            |           46000 - China Mobile (gsm, available)
            |           46000 - CMCC (lte, available)
            |           46011 - -- (lte, forbidden)
"""
        self.assertEqual(cellular_network.parse_scan_output(output), [
            {"operator_id": "46001", "name": "CHN-UNICOM",
             "access_technology": "lte", "status": "current"},
            {"operator_id": "46000", "name": "China Mobile",
             "access_technology": "gsm/lte", "status": "available"},
            {"operator_id": "46011", "name": "46011",
             "access_technology": "lte", "status": "forbidden"},
        ])

    def test_scan_uses_bounded_c_locale_mmcli_command(self):
        runner = Mock(return_value=SimpleNamespace(
            returncode=0,
            stdout="3GPP scan | networks: 46000 - China Mobile (lte, available)\n",
            stderr=""))
        result = cellular_network.scan(MODEM, runner=runner, timeout=30)
        self.assertEqual(result[0]["operator_id"], "46000")
        args, kwargs = runner.call_args
        self.assertEqual(args[0], [
            "mmcli", "-m", MODEM, "--3gpp-scan", "--timeout=30"])
        self.assertEqual(kwargs["timeout"], 45)
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")
        self.assertFalse(kwargs["check"])

    def test_quectel_qmi_uses_same_mm_object_with_full_at_timeout(self):
        runner = Mock(side_effect=self.transaction(self.cops_reply(
            '+COPS: (1,"Fixture Mobile","Fixture","00101",7),'
            '(2,"Fixture Home","Home","00102",7),,(0-4),(0-2)')))
        sleeper = Mock()
        result = cellular_network.scan(MODEM, runner=runner, sleeper=sleeper)
        self.assertEqual([n["operator_id"] for n in result], ["00102", "00101"])
        args, kwargs = runner.call_args_list[3]
        self.assertEqual(args[0], [
            "busctl", "--system", "--json=short", "--timeout=330", "call",
            "org.freedesktop.ModemManager1", MODEM,
            "org.freedesktop.ModemManager1.Modem", "Command", "su", "AT+COPS=?", "315"])
        self.assertEqual(kwargs["timeout"], 345)
        self.assertEqual([call.args[0][-2] for call in runner.call_args_list[1:]],
                         ["AT+COPS?", "AT+COPS=2", "AT+COPS=?", "AT+COPS=0"])
        sleeper.assert_called_once_with(3)
        self.assertEqual(result[0]["status"], "current")
        self.assertEqual(result[0]["access_technology"], "lte")

    def test_other_backends_and_quectel_without_at_keep_standard_scan(self):
        for metadata in [self.metadata(plugin="generic"), self.metadata(
                ports=["cdc-wdm7 (qmi)", "wwan7 (net)"]), self.metadata(
                    ports=["ttyUSB7 (at)"])]:
            with self.subTest(metadata=metadata):
                runner = Mock(side_effect=[metadata, self.reply(
                    "3GPP scan | networks: 00101 - Fixture Mobile (lte, available)")])
                self.assertEqual(cellular_network.scan(MODEM, runner=runner)[0]["operator_id"], "00101")
                self.assertIn("--3gpp-scan", runner.call_args.args[0])

    def test_completed_empty_mmcli_scan_gets_one_at_fallback(self):
        runner = Mock(side_effect=[self.metadata(plugin="generic"),
            self.reply(stderr=cellular_network.EMPTY_MMCLI_SCAN + "\n", returncode=1),
            self.cops_reply('+COPS: (1,"Fixture","F","00101",7),,(0-4),(0-2)')])
        self.assertEqual(cellular_network.scan(MODEM, runner=runner)[0]["operator_id"], "00101")
        self.assertEqual(len(runner.call_args_list), 3)
        self.assertIn(MODEM, runner.call_args.args[0])

    def test_real_mm_failures_and_timeouts_never_launch_another_scan(self):
        for failure in [self.reply(stderr="Timeout was reached", returncode=1),
                        self.reply(stderr="Unauthorized", returncode=1),
                        self.reply(stderr="SIM not inserted", returncode=1),
                        self.reply(stderr="Operation in progress", returncode=1),
                        subprocess.TimeoutExpired("mmcli", 315)]:
            with self.subTest(failure=failure):
                runner = Mock(side_effect=[self.metadata(plugin="generic"), failure])
                with self.assertRaises(cellular_network.CellularNetworkError):
                    cellular_network.scan(MODEM, runner=runner)
                self.assertEqual(len(runner.call_args_list), 2)

    def test_cops_merges_rats_preserves_mnc_and_forbidden_status(self):
        result = cellular_network.parse_cops_output(
            '+COPS: (1,"Fixture (West), Mobile","F","00101",0),'
            '(2,"Fixture LTE","F","00101",7),'
            '(3,"","Forbidden","001001",2),(0,"","","00102"),,(0-4),(0-2)')
        self.assertEqual(result, [
            {"operator_id": "00101", "name": "Fixture (West), Mobile",
             "access_technology": "gsm/lte", "status": "current"},
            {"operator_id": "00102", "name": "00102", "access_technology": "", "status": "unknown"},
            {"operator_id": "001001", "name": "Forbidden", "access_technology": "umts", "status": "forbidden"},
        ])

    def test_empty_at_list_is_empty_but_malformed_reply_is_an_error(self):
        for value in ["+COPS:", "+COPS: ,,(0-4),(0-2)"]:
            runner = Mock(side_effect=self.transaction(self.cops_reply(value)))
            self.assertEqual(cellular_network.scan(MODEM, runner=runner, sleeper=Mock()), [])
        for value in ["OK", "ERROR", '+COPS: (1,"Bad","B","001",7)',
                      '+COPS: (1,"Good","G","00101",7),(broken)',
                      '+COPS: (1,"Bad","B","00101;reboot",7)']:
            with self.subTest(value=value), self.assertRaises(cellular_network.CellularNetworkError):
                cellular_network.parse_cops_output(value)
        for reply in [self.reply("not json"), self.reply('{"type":"s","data":[]}'),
                      self.reply('{"type":"u","data":[3]}'),
                      self.reply(stderr="AT command rejected", returncode=1),
                      subprocess.TimeoutExpired("busctl", 345)]:
            runner = Mock(side_effect=self.transaction(reply))
            with self.subTest(reply=reply), self.assertRaises(cellular_network.CellularNetworkError):
                cellular_network.scan(MODEM, runner=runner, sleeper=Mock())
            self.assertEqual(runner.call_args.args[0][-2], "AT+COPS=0")

    def test_invalid_path_never_reaches_mm_or_at(self):
        runner = Mock()
        with self.assertRaises(cellular_network.CellularNetworkError):
            cellular_network.scan(MODEM + ";reboot", runner=runner)
        runner.assert_not_called()

    def test_modem_rejection_is_actionable_and_does_not_become_an_empty_success(self):
        runner = Mock(side_effect=self.transaction(
            self.reply(stderr="Call failed: Operation not allowed\n", returncode=1)))
        with self.assertRaisesRegex(cellular_network.CellularNetworkError, "The modem rejected the scan"):
            cellular_network.scan(MODEM, runner=runner, sleeper=Mock())
        self.assertEqual(len(runner.call_args_list), 5)
        self.assertEqual(runner.call_args.args[0][-2], "AT+COPS=0")

    def test_manual_selection_is_restored_exactly_after_success_or_failure(self):
        for mode in [1, 4]:
            for result in [self.cops_reply("+COPS: ,,(0-4),(0-2)"),
                           subprocess.TimeoutExpired("busctl", 345)]:
                runner = Mock(side_effect=self.transaction(
                    result, selection=f'+COPS: {mode},2,"001001",7'))
                with self.subTest(mode=mode, result=result):
                    if isinstance(result, Exception):
                        with self.assertRaises(cellular_network.CellularNetworkError):
                            cellular_network.scan(MODEM, runner=runner, sleeper=Mock())
                    else:
                        self.assertEqual(cellular_network.scan(MODEM, runner=runner, sleeper=Mock()), [])
                    self.assertEqual(runner.call_args.args[0][-2], f'AT+COPS={mode},2,"001001"')

    def test_failed_deregistration_still_restores_without_starting_scan(self):
        for failure in [self.reply(stderr="rejected", returncode=1),
                        subprocess.TimeoutExpired("busctl", 90)]:
            runner = Mock(side_effect=[self.metadata(), self.cops_reply("+COPS: 0"),
                                       failure, self.cops_reply("")])
            with self.subTest(failure=failure), self.assertRaises(cellular_network.CellularNetworkError):
                cellular_network.scan(MODEM, runner=runner, sleeper=Mock())
            self.assertEqual([call.args[0][-2] for call in runner.call_args_list[1:]],
                             ["AT+COPS?", "AT+COPS=2", "AT+COPS=0"])

    def test_restore_failure_cannot_be_reported_as_scan_success(self):
        runner = Mock(side_effect=self.transaction(
            self.cops_reply('+COPS: (1,"Fixture","F","00101",7)'),
            restore_reply=self.reply(stderr="restore failed", returncode=1)))
        with self.assertRaisesRegex(cellular_network.CellularNetworkError, "Restoring cellular registration failed"):
            cellular_network.scan(MODEM, runner=runner, sleeper=Mock())

    def test_unrestorable_selection_and_deregistered_mode_are_never_changed(self):
        for selection in ['+COPS: 1,0,"Fixture Mobile",7', "+COPS: 2", "+COPS: 1",
                          '+COPS: 1,2,"00101;reboot",7', "unknown"]:
            runner = Mock(side_effect=[self.metadata(), self.cops_reply(selection),
                                       self.cops_reply("+COPS: ,,(0-4),(0-2)")])
            self.assertEqual(cellular_network.scan(MODEM, runner=runner, sleeper=Mock()), [])
            self.assertEqual([call.args[0][-2] for call in runner.call_args_list[1:]],
                             ["AT+COPS?", "AT+COPS=?"])

    def test_registration_accepts_only_automatic_or_exact_plmn(self):
        calls = []

        def runner(args, **kwargs):
            if "--output-json" in args and not calls:
                calls.append((args, kwargs))
                return self.reply("{}")
            calls.append((args, kwargs))
            if "--output-json" in args:
                return self.reply(json.dumps({"modem": {"3gpp": {
                    "registration-state": "roaming", "operator-code": "46000"}}}))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.assertEqual(cellular_network.register(
            MODEM, mode="automatic", runner=runner, timeout=20, sleeper=Mock()),
            {"mode": "automatic", "operator_id": "",
             "registration": {"state": "roaming", "operator_id": "46000"}})
        self.assertEqual(cellular_network.register(
            MODEM, mode="manual", operator_id="46000", runner=runner, timeout=20, sleeper=Mock()),
            {"mode": "manual", "operator_id": "46000",
             "registration": {"state": "roaming", "operator_id": "46000"}})
        self.assertTrue(any("--3gpp-register-home" in args for args, _ in calls))
        self.assertTrue(any("--3gpp-register-in-operator=46000" in args for args, _ in calls))
        with self.assertRaises(cellular_network.CellularNetworkError):
            cellular_network.register(
                MODEM, mode="manual", operator_id="46000; reboot", runner=runner)
        self.assertEqual(len(calls), 10)

    def test_selection_recovery_proof_requires_exact_mode_and_numeric_plmn(self):
        for selection, reply, expected in [
                ({"mode": "automatic", "operator_id": ""}, "+COPS: 0", True),
                ({"mode": "automatic", "operator_id": ""}, "+COPS: 1", False),
                ({"mode": "manual", "operator_id": "00101"}, '+COPS: 1,2,"00101",7', True),
                ({"mode": "manual", "operator_id": "00101"}, '+COPS: 1,2,"00102",7', False),
                ({"mode": "manual", "operator_id": "00101"}, '+COPS: 1,0,"Fixture",7', False)]:
            runner = Mock(side_effect=[self.metadata(), self.cops_reply(reply)])
            self.assertEqual(cellular_network._selection_is_applied(MODEM, selection, runner), expected)


class CellularRegistrationTests(unittest.TestCase):
    def setUp(self):
        backend = patch.object(cellular_network, "_prefer_at_scan", return_value=False)
        self.backend = backend.start()
        self.addCleanup(backend.stop)
        patcher = patch.object(cellular_network, "_selection_is_applied", return_value=False)
        self.selection_applied = patcher.start()
        self.addCleanup(patcher.stop)
    @staticmethod
    def reply(value="", error=""):
        return SimpleNamespace(returncode=int(bool(error)), stdout=value, stderr=error)

    def state(self, state="roaming", operator="00101"):
        return self.reply(json.dumps({"modem": {"3gpp": {
            "registration-state": state, "operator-code": operator}}}))

    def test_late_registration_after_mm_timeout_is_confirmed_without_retry(self):
        runner = Mock(side_effect=[self.reply(error="Network timeout"),
            self.state("searching", ""), self.state("roaming", "00102"),
            self.state("roaming", "00102"), self.state("roaming", "00102")])
        sleep = Mock()
        result = cellular_network.register(MODEM, mode="manual", operator_id="00102",
                                           runner=runner, sleeper=sleep, settle_attempts=4)
        self.assertEqual(result["registration"], {"state": "roaming", "operator_id": "00102"})
        self.assertEqual(sum("--output-json" not in c.args[0] for c in runner.call_args_list), 1)
        self.assertEqual(sleep.call_count, 3)

    def test_wrong_plmn_is_not_success_even_when_command_and_state_say_registered(self):
        runner = Mock(side_effect=[self.reply(), self.state(operator="00101"),
            self.reply(), self.state(operator="00101")])
        with self.assertRaises(cellular_network.CellularRegistrationError) as caught:
            cellular_network.register(MODEM, mode="manual", operator_id="00102",
                                      runner=runner, sleeper=Mock(), settle_attempts=1)
        self.assertEqual(caught.exception.detail["code"], "not_registered")
        self.assertEqual(caught.exception.detail["recovery"]["state"], "restored")
        self.assertIn("--3gpp-register-home", runner.call_args_list[2].args[0])

    def test_manual_failure_restores_saved_manual_selection(self):
        runner = Mock(side_effect=[self.reply(error="Network not allowed"), self.state("denied", ""),
            self.reply(), self.state(operator="001001")])
        with self.assertRaises(cellular_network.CellularRegistrationError) as caught:
            cellular_network.register(MODEM, mode="manual", operator_id="00102",
                previous={"mode": "manual", "operator_id": "001001"}, runner=runner,
                sleeper=Mock(), settle_attempts=1)
        self.assertEqual(caught.exception.detail["code"], "denied")
        self.assertIn("--3gpp-register-in-operator=001001", runner.call_args_list[2].args[0])
        self.assertEqual(caught.exception.detail["recovery"]["state"], "restored")

    def test_recovery_distinguishes_searching_and_failure_without_raw_errors(self):
        for recovery_reply, state, expected in [
                (self.reply(), self.state("searching", ""), "pending"),
                (self.reply(error="secret transport error"), self.state("unknown", ""), "failed")]:
            runner = Mock(side_effect=[self.reply(error="Network timeout private-data"),
                self.state("searching", ""), recovery_reply, state])
            with self.subTest(expected=expected), self.assertRaises(cellular_network.CellularRegistrationError) as caught:
                cellular_network.register(MODEM, mode="manual", operator_id="00102",
                                          runner=runner, sleeper=Mock(), settle_attempts=1)
            self.assertEqual(caught.exception.detail["recovery"]["state"], expected)
            self.assertNotIn("private-data", json.dumps(caught.exception.detail))
            self.assertNotIn("secret", json.dumps(caught.exception.detail))

    def test_invalid_saved_selection_cannot_reach_hardware(self):
        runner = Mock()
        with self.assertRaises(cellular_network.CellularNetworkError):
            cellular_network.register(MODEM, mode="manual", operator_id="00101",
                previous={"mode": "manual", "operator_id": "00102;reboot"}, runner=runner)
        runner.assert_not_called()

    def test_transport_timeout_restores_and_unreadable_status_is_not_success(self):
        runner = Mock(side_effect=[subprocess.TimeoutExpired("mmcli", 135),
            self.reply("not json"), self.reply(), self.state()])
        with self.assertRaises(cellular_network.CellularRegistrationError) as caught:
            cellular_network.register(MODEM, mode="manual", operator_id="00102",
                                      runner=runner, sleeper=Mock(), settle_attempts=1)
        self.assertEqual(caught.exception.detail["code"], "network_timeout")
        self.assertEqual(caught.exception.detail["recovery"]["state"], "restored")

    def test_recovery_timeout_with_proven_automatic_mode_is_pending_not_registered(self):
        self.selection_applied.return_value = True
        runner = Mock(side_effect=[self.reply(error="Network timeout"), self.state("searching", ""),
            self.reply(error="Network timeout"), self.state("idle", "")])
        with self.assertRaises(cellular_network.CellularRegistrationError) as caught:
            cellular_network.register(MODEM, mode="manual", operator_id="00102",
                                      runner=runner, sleeper=Mock(), settle_attempts=1)
        self.assertEqual(caught.exception.detail["recovery"]["state"], "pending")
        self.selection_applied.assert_called_once_with(MODEM, {"mode": "automatic", "operator_id": ""}, runner)

    def test_quectel_registration_synchronizes_mm_before_managed_at(self):
        self.backend.return_value = True
        self.selection_applied.return_value = True
        order = []
        def run(args, **kwargs):
            if "--output-json" in args:
                return self.state(operator="00102")
            order.append("mm-register")
            return self.reply()
        runner = Mock(side_effect=run)
        def at(*args):
            order.append(args[1])
            return ""
        with patch.object(cellular_network, "_at_command", side_effect=at) as command:
            result = cellular_network.register(MODEM, mode="manual", operator_id="00102",
                                              runner=runner, sleeper=Mock(), settle_attempts=1)
        self.assertEqual([c.args[1] for c in command.call_args_list], ["AT+COPS=2", 'AT+COPS=1,2,"00102"'])
        self.assertTrue(all(c.args[0] == MODEM for c in command.call_args_list))
        self.assertIn("--3gpp-register-in-operator=00102", runner.call_args_list[0].args[0])
        self.assertEqual(result["registration"]["operator_id"], "00102")
        self.assertEqual(order, ["mm-register", "AT+COPS=2", 'AT+COPS=1,2,"00102"'])

    def test_registration_permission_error_never_touches_at(self):
        runner = Mock(return_value=self.reply(error="Unauthorized"))
        with patch.object(cellular_network, "_at_command") as command:
            self.assertEqual(cellular_network._request_registration(MODEM,
                {"mode": "automatic", "operator_id": ""}, runner, 120, use_at=True), "failed")
        command.assert_not_called()

    def test_quectel_rejection_restores_saved_selection_via_at_and_mm(self):
        self.backend.return_value = True
        self.selection_applied.return_value = True
        runner = Mock(side_effect=[self.reply(), self.state("searching", ""), self.state("searching", ""), self.reply(), self.state()])
        with patch.object(cellular_network, "_at_command", side_effect=["",
                cellular_network.CellularNetworkError("Call failed: No network service"), "", ""]) as command:
            with self.assertRaises(cellular_network.CellularRegistrationError) as caught:
                cellular_network.register(MODEM, mode="manual", operator_id="00102",
                                          runner=runner, sleeper=Mock(), settle_attempts=1)
        self.assertEqual([c.args[1] for c in command.call_args_list],
                         ["AT+COPS=2", 'AT+COPS=1,2,"00102"', "AT+COPS=2", "AT+COPS=0"])
        self.assertEqual(caught.exception.detail["code"], "no_service")
        self.assertEqual(caught.exception.detail["recovery"]["state"], "restored")
        self.assertIn("--3gpp-register-home", runner.call_args_list[3].args[0])

    def test_automatic_selection_preserves_a_working_quectel_registration(self):
        self.backend.return_value = True
        self.selection_applied.return_value = True
        runner = Mock(side_effect=[self.state(), self.reply(), self.state()])
        with patch.object(cellular_network, "_at_command", return_value="") as command:
            cellular_network.register(MODEM, mode="automatic", runner=runner,
                                      sleeper=Mock(), settle_attempts=1)
        self.assertEqual([c.args[1] for c in command.call_args_list], ["AT+COPS=0"])

    def test_transient_old_registration_is_not_confirmed(self):
        runner = Mock(side_effect=[self.reply(), self.state(), self.state("searching", ""),
            self.state("idle", ""), self.reply(), self.state(), self.state(), self.state()])
        with self.assertRaises(cellular_network.CellularRegistrationError) as caught:
            cellular_network.register(MODEM, mode="automatic", runner=runner,
                                      sleeper=Mock(), settle_attempts=3)
        self.assertEqual(caught.exception.detail["code"], "not_registered")
        self.assertEqual(caught.exception.detail["recovery"]["state"], "restored")

    def test_one_registered_sample_at_deadline_does_not_bypass_stability_check(self):
        runner = Mock(side_effect=[self.state("searching", ""), self.state("searching", ""), self.state()])
        result = cellular_network._wait_registration(MODEM, {"mode": "automatic", "operator_id": ""},
                                                     runner, Mock(), 3)
        self.assertEqual(result["state"], "registering")

    def test_quectel_cannot_report_automatic_success_while_hardware_remains_manual(self):
        self.backend.return_value = True
        runner = Mock(side_effect=[self.state(), self.reply(), self.state(), self.state(), self.reply(), self.state()])
        with patch.object(cellular_network, "_at_command", return_value=""):
            with self.assertRaises(cellular_network.CellularRegistrationError) as caught:
                cellular_network.register(MODEM, mode="automatic", runner=runner,
                                          sleeper=Mock(), settle_attempts=1)
        self.assertEqual(caught.exception.detail["code"], "not_registered")
        self.assertEqual(caught.exception.detail["recovery"]["state"], "failed")


class CellularNetworkApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_hardware_rediscovery_cannot_interrupt_a_network_transaction(self):
        with patch.object(main, "capability_lock", asyncio.Lock()), \
                patch.object(main.operations, "request_device_rescan") as rescan:
            async with main.capability_lock:
                for call in [lambda: main.api_devices_rescan(), lambda: main.api_device_rescan("modem-a")]:
                    with self.assertRaises(main.HTTPException) as raised:
                        await call()
                    self.assertEqual(raised.exception.status_code, 409)
            rescan.assert_not_called()

    async def test_scan_waits_for_an_existing_hardware_rediscovery(self):
        for state in ["requested", "running"]:
            with patch.object(main.operations, "device_rescan_status", return_value={"state": state}), \
                    patch.object(main.cellular_network, "scan") as scan:
                with self.assertRaises(main.HTTPException) as raised:
                    await main.api_device_cellular_network_scan("modem-a")
                self.assertEqual(raised.exception.status_code, 409)
                scan.assert_not_called()

    def observed(self, **cellular):
        return {"devices": {"modem-a": {
            "present": True, "mm_object": MODEM,
            "cellular": {"available": True, "sim_present": True,
                         "sim_iccid": "card-a", "data_active": False,
                         **cellular},
        }}}

    async def test_scan_is_scoped_to_current_device_modem(self):
        line = {"id": "3", "iccid": "card-a"}
        networks = [{"operator_id": "46000", "name": "China Mobile",
                     "access_technology": "lte", "status": "available"}]
        with patch.object(main.device_state, "status", return_value=self.observed()), \
                patch.object(main, "_match_instance_by_iccid", return_value=line), \
                patch.object(main.cellular_network, "scan", return_value=networks) as scan:
            result = await main.api_device_cellular_network_scan("modem-a")
        self.assertEqual(result, {"device_id": "modem-a", "networks": networks})
        scan.assert_called_once_with(MODEM)

    async def test_manual_selection_persists_only_after_registration(self):
        line = {"id": "3", "iccid": "card-a"}
        selection = {"mode": "manual", "operator_id": "46000"}
        with patch.object(main.device_state, "status", return_value=self.observed()), \
                patch.object(main, "_match_instance_by_iccid", return_value=line), \
                patch.object(main.cellular_network, "register",
                             return_value=selection) as register, \
                patch.object(main.cfg, "upsert_instance", return_value={
                    **line, "cellular_network_mode": "manual",
                    "cellular_operator_id": "46000"}) as save, \
                patch.object(main.hub, "broadcast", new=AsyncMock()) as broadcast:
            result = await main.api_device_cellular_network_select(
                "modem-a", {"mode": "manual", "operator_id": "46000"})
        self.assertTrue(result["ok"])
        register.assert_called_once_with(
            MODEM, mode="manual", operator_id="46000",
            previous={"mode": "automatic", "operator_id": ""})
        save.assert_called_once_with({
            "id": "3", "cellular_network_mode": "manual",
            "cellular_operator_id": "46000"})
        broadcast.assert_awaited_once()

    async def test_failed_registration_never_saves_the_requested_network(self):
        failure = cellular_network.CellularRegistrationError("network_timeout", {"state": "restored"})
        with patch.object(main.device_state, "status", return_value=self.observed()), \
                patch.object(main, "_match_instance_by_iccid", return_value={"id": "3"}), \
                patch.object(main.cellular_network, "register", side_effect=failure), \
                patch.object(main.cfg, "upsert_instance") as save:
            with self.assertRaises(main.HTTPException) as caught:
                await main.api_device_cellular_network_select("modem-a", {"mode": "manual", "operator_id": "00102"})
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(caught.exception.detail, failure.detail)
            save.assert_not_called()

    async def test_scan_guards_fail_before_any_modem_command(self):
        for observed in [self.observed(sim_present=False), self.observed(data_active=True),
                         self.observed(available=False)]:
            with patch.object(main.device_state, "status", return_value=observed), \
                    patch.object(main.cellular_network, "scan") as scan:
                with self.assertRaises(main.HTTPException) as raised:
                    await main.api_device_cellular_network_scan("modem-a")
                self.assertEqual(raised.exception.status_code, 409)
                scan.assert_not_called()
        observed = self.observed()
        observed["devices"]["modem-a"]["desired"] = {"flight_mode": True}
        with patch.object(main.device_state, "status", return_value=observed), \
                patch.object(main.cellular_network, "scan") as scan:
            with self.assertRaises(main.HTTPException):
                await main.api_device_cellular_network_scan("modem-a")
            scan.assert_not_called()

    async def test_missing_sim_and_active_bearer_fail_before_mmcli(self):
        register = Mock()
        with patch.object(main.cellular_network, "register", register), \
                patch.object(main.device_state, "status", return_value=self.observed(
                    sim_present=False, sim_iccid="")):
            with self.assertRaises(main.HTTPException) as missing:
                await main.api_device_cellular_network_select(
                    "modem-a", {"mode": "manual", "operator_id": "46000"})
        self.assertEqual(missing.exception.status_code, 409)
        with patch.object(main.cellular_network, "register", register), \
                patch.object(main.device_state, "status", return_value=self.observed(
                    data_active=True)), \
                patch.object(main, "_match_instance_by_iccid", return_value={"id": "3"}):
            with self.assertRaises(main.HTTPException) as active:
                await main.api_device_cellular_network_select(
                    "modem-a", {"mode": "automatic"})
        self.assertEqual(active.exception.status_code, 409)
        register.assert_not_called()

    async def test_invalid_manual_operator_is_rejected_before_device_lookup(self):
        with patch.object(main.device_state, "status") as status:
            with self.assertRaises(main.HTTPException) as raised:
                await main.api_device_cellular_network_select(
                    "modem-a", {"mode": "manual", "operator_id": "46000;reboot"})
        self.assertEqual(raised.exception.status_code, 400)
        status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
