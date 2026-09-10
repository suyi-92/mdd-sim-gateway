import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from host.mdd_orchestrator import Orchestrator


class CountryTunDnsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Orchestrator(Path(self.temp.name), Path.cwd(), dry_run=False)
        self.app.log = Mock()
        self.config = {"inbounds": [{"type": "tun", "interface_name": "mdd-gb",
                                    "tag": "tun-gb", "auto_route": False}]}

    def run_isolation(self, domains="~.", default_route="yes", failure=None):
        def command(args, **kwargs):
            if failure:
                raise failure
            value = domains if args[1] == "domain" else default_route
            return SimpleNamespace(returncode=0, stdout=f"Link 10 (mdd-gb): {value}\n")
        with patch("host.mdd_orchestrator.shutil.which", return_value="/usr/bin/resolvectl"), \
                patch("host.mdd_orchestrator.subprocess.run", side_effect=command) as run:
            self.app.isolate_country_tun_dns(self.config)
        return [call.args[0] for call in run.call_args_list]

    def test_removes_only_default_dns_and_keeps_scoped_domains(self):
        calls = self.run_isolation("~. ~ims.example search.example")
        self.assertEqual(calls, [
            ["resolvectl", "domain", "mdd-gb"],
            ["resolvectl", "default-route", "mdd-gb"],
            ["resolvectl", "domain", "mdd-gb", "~ims.example", "search.example"],
            ["resolvectl", "default-route", "mdd-gb", "no"],
        ])

    def test_empty_domain_is_passed_explicitly(self):
        self.assertIn(["resolvectl", "domain", "mdd-gb", ""], self.run_isolation())

    def test_healthy_scoped_dns_is_read_only(self):
        self.assertEqual(self.run_isolation("~ims.example", "no"), [
            ["resolvectl", "domain", "mdd-gb"], ["resolvectl", "default-route", "mdd-gb"]])

    def test_late_default_route_registration_is_repaired_without_erasing_domains(self):
        self.assertEqual(self.run_isolation("~ims.example")[-1],
                         ["resolvectl", "default-route", "mdd-gb", "no"])
        self.assertEqual(len(self.run_isolation("~ims.example")), 3)

    def test_does_not_rely_on_interface_name_alone(self):
        original = self.config["inbounds"][0]
        for changes in ({"auto_route": True}, {"auto_route": None}, {"type": "socks"},
                        {"interface_name": "vpn0"}, {"tag": "unrelated"}):
            with self.subTest(changes=changes):
                self.config["inbounds"][0] = {**original, **changes}
                self.assertEqual(self.run_isolation(), [])

    def test_dry_run_never_queries_or_writes_host_dns(self):
        self.app.dry_run = True
        self.assertEqual(self.run_isolation(), [])

    def test_failure_is_reported_without_stopping_country_process(self):
        self.run_isolation(failure=subprocess.TimeoutExpired("resolvectl", 3))
        self.assertIn("will retry", self.app.log.call_args.args[0])

    def test_unchanged_generation_retries_late_dns_registration(self):
        self.app.last_proxy_fingerprint = hashlib.sha256(
            json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        self.app.singbox = Mock()
        self.app.singbox.poll.return_value = None
        with patch.object(self.app, "isolate_country_tun_dns") as isolate:
            self.app.apply_singbox(self.config)
        isolate.assert_called_once_with(self.config)
        self.app.singbox.terminate.assert_not_called()
