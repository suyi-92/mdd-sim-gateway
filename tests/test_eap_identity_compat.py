"""Exercise carrier-compatible generic EAP Identity handling without the Engine card stack."""
import ast
import struct
import types
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parent.parent / "engine" / "swu_ike.py"
WANTED = {"permanent_eap_identity", "build_eap_identity_response",
          "encode_eap_at_identity", "state_2", "state_3"}


def _load_functions(logs):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    picked = [node for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef) and node.name in WANTED]
    module = ast.Module(body=picked, type_ignores=[])
    namespace = {
        "struct": struct,
        "EAP_RESPONSE": 2,
        "EAP_IDENTITY": 1,
        "EAP_REQUEST": 1,
        "EAP_AKA": 23,
        "AKA_Challenge": 1,
        "AKA_Identity": 5,
        "AKA_Reauthentication": 13,
        "AT_PERMANENT_ID_REQ": 10,
        "AT_ANY_ID_REQ": 13,
        "AT_IDENTITY": 14,
        "AT_FULLAUTH_ID_REQ": 17,
        "IKE_AUTH": 35,
        "SK": 46,
        "N": 41,
        "EAP": 48,
        "DEVICE_IDENTITY": 41101,
        "AUTHENTICATION_FAILED": 24,
        "REPEAT_STATE": 2,
        "MANDATORY_INFORMATION_MISSING": 4,
        "OTHER_ERROR": 5,
        "fromHex": bytes.fromhex,
        "swu_log": logs.append,
    }
    exec(compile(module, str(SOURCE), "exec"), namespace)  # noqa: S102
    return namespace


class FakeTunnel:
    def __init__(self, functions, inner_payloads):
        self.message_id_request = 0
        self.ike_decoded_header = {"exchange_type": 35}
        self.decoded_payload = [[46, inner_payloads]]
        self.imsi = "001010000000001"
        self.mcc = "001"
        self.mnc = "01"
        self.sent = []
        self.state_2 = types.MethodType(functions["state_2"], self)
        self.state_3 = types.MethodType(functions["state_3"], self)
        self.encode_eap_at_identity = types.MethodType(
            functions["encode_eap_at_identity"], self)

    def create_IKE_AUTH(self):
        return b"initial-ike-auth"

    def create_IKE_AUTH_EAP_IDENTITY(self):
        return b"identity-ike-auth"

    def create_IKE_AUTH_2(self):
        return b"followup-ike-auth"

    def _send_request_await_response(self, packet):
        self.sent.append(packet)
        return True


class GenericEapIdentityTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.functions = _load_functions(self.logs)

    def test_generic_identity_request_is_answered_with_the_permanent_nai(self):
        request_id = 37
        tunnel = FakeTunnel(
            self.functions, [[48, [1, request_id, 1, b"carrier prompt"]]])

        result, detail = tunnel.state_2()

        self.assertEqual((result, detail), (2, "EAP IDENTITY REQUESTED"))
        response = tunnel.eap_payload_response
        self.assertEqual(response[0], 2)
        self.assertEqual(response[1], request_id)
        self.assertEqual(struct.unpack("!H", response[2:4])[0], len(response))
        self.assertEqual(response[4], 1)
        expected = b"0001010000000001@nai.epc.mnc001.mcc001.3gppnetwork.org"
        self.assertEqual(response[5:], expected)
        self.assertTrue(any("value redacted" in line for line in self.logs))
        self.assertFalse(any("001010000000001" in line for line in self.logs))

    def test_unsupported_eap_type_is_not_misreported_as_no_payload(self):
        tunnel = FakeTunnel(self.functions, [[48, [1, 9, 99, b""]]])

        result, detail = tunnel.state_2()

        self.assertEqual(result, 4)
        self.assertEqual(detail, "UNSUPPORTED EAP REQUEST RECEIVED")
        self.assertEqual(tunnel.reject_reason_code, "no_eap_challenge")
        self.assertEqual(tunnel.reject_reason_policy, "backoff")
        self.assertTrue(any("code=1 type=99" in line for line in self.logs))

    def test_generic_identity_request_is_also_tolerated_later_in_the_exchange(self):
        request_id = 41
        tunnel = FakeTunnel(self.functions, [[48, [1, request_id, 1, b""]]])

        result, detail = tunnel.state_3()

        self.assertEqual((result, detail), (2, "EAP IDENTITY REQUESTED"))
        self.assertEqual(tunnel.sent, [b"followup-ike-auth"])
        self.assertEqual(tunnel.eap_payload_response[1], request_id)

    def test_all_standard_eap_aka_identity_selectors_receive_a_permanent_identity(self):
        for selector in (10, 13, 17):
            with self.subTest(selector=selector):
                tunnel = FakeTunnel(
                    self.functions, [[48, [1, 17, 23, 5, [(99, 0), (selector, 0)]]]])

                result, detail = tunnel.state_2()

                self.assertEqual((result, detail), (2, "EAP IDENTITY REQUESTED"))
                self.assertEqual(tunnel.eap_payload_response[0:2], bytes([2, 17]))
                self.assertEqual(tunnel.eap_payload_response[4:6], bytes([23, 5]))
                self.assertIn(b"001010000000001", tunnel.eap_payload_response)

    def test_empty_ike_auth_response_has_a_machine_readable_backoff_reason(self):
        tunnel = FakeTunnel(self.functions, [])

        result, detail = tunnel.state_2()

        self.assertEqual(result, 4)
        self.assertEqual(detail, "NO EAP PAYLOAD RECEIVED")
        self.assertEqual(tunnel.reject_reason_code, "no_eap_challenge")
        self.assertEqual(tunnel.reject_reason_policy, "backoff")


if __name__ == "__main__":
    unittest.main()
