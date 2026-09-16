import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from control.app import cellular_network, main


MODEM = "/org/freedesktop/ModemManager1/Modem/7"


class CellularNetworkCommandTests(unittest.TestCase):
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

    def test_registration_accepts_only_automatic_or_exact_plmn(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.assertEqual(cellular_network.register(
            MODEM, mode="automatic", runner=runner, timeout=20),
            {"mode": "automatic", "operator_id": ""})
        self.assertEqual(cellular_network.register(
            MODEM, mode="manual", operator_id="46000", runner=runner, timeout=20),
            {"mode": "manual", "operator_id": "46000"})
        self.assertIn("--3gpp-register-home", calls[0][0])
        self.assertIn("--3gpp-register-in-operator=46000", calls[1][0])
        with self.assertRaises(cellular_network.CellularNetworkError):
            cellular_network.register(
                MODEM, mode="manual", operator_id="46000; reboot", runner=runner)
        self.assertEqual(len(calls), 2)


class CellularNetworkApiTests(unittest.IsolatedAsyncioTestCase):
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
            MODEM, mode="manual", operator_id="46000")
        save.assert_called_once_with({
            "id": "3", "cellular_network_mode": "manual",
            "cellular_operator_id": "46000"})
        broadcast.assert_awaited_once()

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
