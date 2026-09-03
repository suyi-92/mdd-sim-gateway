"""CMLink UK's numeric customer-service short code is a home-local voice number.

It must keep its audio-call semantics while its outbound IMS URI gains the home-domain
phone-context required for a non-E.164 number. The carrier match is intentionally strict:
the underlying PLMN is shared with EE and other hosted brands.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from control.app import config
from engine import render as engine_render

try:
    from control.app import main
except ImportError:  # source-only hosts need not have the Control venv dependencies
    main = None


ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
SHORT_CODE = "10086"
REALM = "ims.mnc033.mcc234.3gppnetwork.org"


def instance(*, spn: str = "CMLink", mnc: str = "33") -> dict:
    return {
        "id": "1",
        "index": 0,
        "imsi": "234330123456789",
        "mcc": "234",
        "mnc": mnc,
        "iccid": "8944110000000000000",
        "imei": "490154203237518",
        "ami_secret": "test-secret",
        "carrier_identity": {"spn": spn},
        "sip": {"webrtc": {"enable": True, "password": "test-password"}},
    }


def render_dialplan(codes=()) -> str:
    context = {
        "webrtc_enable": True,
        "webrtc_user": "webrtc",
        "ring_timeout": 35,
        "msisdn": "+15550000000",
        "realm": REALM,
        "vm_enabled": False,
        "vm_ring_seconds": 25,
        "vm_max_seconds": 120,
        "home_local_voice_codes": tuple(codes),
    }
    env = Environment(loader=FileSystemLoader(str(ROOT / "engine" / "templates")),
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    return env.get_template("extensions.conf.j2").render(**context)


class CarrierRuleTests(unittest.TestCase):
    def test_exact_cmlink_identity_gets_only_the_official_voice_code(self):
        for mnc in ("33", "033"):
            self.assertEqual(
                config.carrier_home_local_voice_codes(
                    "234", mnc, {"spn": "CMLink UK"}),
                (SHORT_CODE,),
            )

    def test_shared_plmn_without_cmlink_spn_gets_no_local_code(self):
        for identity in ({}, {"spn": "EE"}, {"spn": "another MVNO"}):
            self.assertEqual(
                config.carrier_home_local_voice_codes("234", "33", identity), ())

    def test_matching_spn_on_another_plmn_gets_no_local_code(self):
        self.assertEqual(
            config.carrier_home_local_voice_codes(
                "234", "30", {"spn": "CMLink"}),
            (),
        )


class EngineContractTests(unittest.TestCase):
    def test_control_emits_the_derived_code_for_cmlink_only(self):
        rendered = config.render_instance_json(instance(), {})
        self.assertEqual(rendered["sip"]["home_local_voice_codes"], [SHORT_CODE])

        generic = config.render_instance_json(instance(spn="EE"), {})
        self.assertEqual(generic["sip"]["home_local_voice_codes"], [])

    def test_engine_rejects_non_numeric_or_unbounded_hand_authored_values(self):
        cfg = config.render_instance_json(instance(), {})
        cfg["local_addr"] = "192.0.2.20"
        cfg["sip"]["home_local_voice_codes"] = [
            SHORT_CODE, SHORT_CODE, "1", "1234567", "12;bad", {"not": "a code"},
        ]

        context = engine_render.build_context(cfg)

        self.assertEqual(context["home_local_voice_codes"], (SHORT_CODE,))

    def test_rendered_short_code_uri_has_context_and_user_phone(self):
        dialplan = render_dialplan((SHORT_CODE,))
        expected = (
            "Set(DIALDEST=PJSIP/volte_ims/sip:${EXTEN}\\;phone-context="
            f"{REALM}@{REALM}\\;user=phone)"
        )
        self.assertIn(expected, dialplan)
        self.assertIn('ExecIf($["${EXTEN}"="10086"]?', dialplan)
        self.assertIn("Dial(${DIALDEST},35,b(ims-outbound-headers^s^1))", dialplan)

    def test_other_numbers_keep_the_existing_endpoint_dial_form(self):
        dialplan = render_dialplan()
        self.assertIn("Set(DIALDEST=PJSIP/${DIALTARGET}@volte_ims)", dialplan)
        self.assertNotIn("phone-context=", dialplan)

    @unittest.skipIf(main is None, "control plane dependencies are not installed")
    def test_numeric_voice_short_code_is_not_misclassified_as_ussd(self):
        self.assertFalse(main._is_service_code(SHORT_CODE))


@unittest.skipUnless(NODE, "Node.js is required for the call-status behavior test")
class BrowserCallStatusTests(unittest.TestCase):
    def test_authoritative_backend_result_overrides_the_synthetic_decline(self):
        script = r"""
import { ordinaryCallEndLabel } from './webui/src/call-status.js'
const result = {
  pending: ordinaryCallEndLabel('dialing', 'Rejected', true),
  failed: ordinaryCallEndLabel('failed', 'Rejected', true),
  rejected: ordinaryCallEndLabel('rejected', 'Rejected', true),
  direct: ordinaryCallEndLabel('', 'Rejected', false),
}
process.stdout.write(JSON.stringify(result))
"""
        completed = subprocess.run(
            [NODE, "--input-type=module", "--eval", script],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=15,
        )

        self.assertEqual(json.loads(completed.stdout), {
            "pending": "Call ended",
            "failed": "Failed",
            "rejected": "Declined",
            "direct": "Call declined",
        })


if __name__ == "__main__":
    unittest.main()
