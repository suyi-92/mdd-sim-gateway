import sys
import types
import unittest


# Pure decoder tests should remain runnable on development hosts without libpcsclite/pyscard.
try:
    from control.app import sim
except ModuleNotFoundError as exc:
    if not str(exc.name).startswith("smartcard"):
        raise
    smartcard = types.ModuleType("smartcard")
    system = types.ModuleType("smartcard.System"); system.readers = lambda: []
    connection = types.ModuleType("smartcard.CardConnection"); connection.CardConnection = object
    exceptions = types.ModuleType("smartcard.Exceptions")
    exceptions.NoCardException = exceptions.CardConnectionException = RuntimeError
    scard = types.ModuleType("smartcard.scard")
    scard.SCardBeginTransaction = scard.SCardEndTransaction = lambda *args: None
    scard.SCARD_LEAVE_CARD = 0
    sys.modules.update({"smartcard": smartcard, "smartcard.System": system,
                        "smartcard.CardConnection": connection,
                        "smartcard.Exceptions": exceptions, "smartcard.scard": scard})
    from control.app import sim


class SimCarrierIdentityTests(unittest.TestCase):
    def test_gsm_spn_decoding(self):
        self.assertEqual(sim.decode_alpha_identifier(
            [ord(value) for value in "giffgaff"] + [0xFF]), "giffgaff")

    def test_ucs2_spn_decoding(self):
        payload = [0x80] + list("中国移动".encode("utf-16-be")) + [0xFF, 0xFF]
        self.assertEqual(sim.decode_alpha_identifier(payload), "中国移动")

    def test_optional_transparent_ef_uses_the_fcp_file_size(self):
        class Connection:
            def __init__(self):
                self.commands = []

            def transmit(self, command):
                self.commands.append(command)
                if len(self.commands) == 1:  # SELECT
                    return [], 0x61, 6
                if len(self.commands) == 2:  # GET RESPONSE: FCP says three bytes
                    return [0x62, 0x04, 0x80, 0x02, 0x00, 0x03], 0x90, 0x00
                return [0x35, 0x34, 0x4D], 0x90, 0x00

        connection = Connection()
        self.assertEqual(sim._read_transparent(connection, "6f3e"), [0x35, 0x34, 0x4D])
        self.assertEqual(connection.commands[-1][-1], 3)

    def test_apdu_transport_follows_legacy_9f_continuation(self):
        class Connection:
            def __init__(self):
                self.commands = []

            def transmit(self, command):
                self.commands.append(command)
                if len(self.commands) == 1:
                    return [0xAA], 0x9F, 0x02
                return [0xBB, 0xCC], 0x90, 0x00

        connection = Connection()
        data, s1, s2 = sim._transmit(connection, [0x00, 0xA4, 0x00, 0x04, 0x00])
        self.assertEqual((data, s1, s2), ([0xAA, 0xBB, 0xCC], 0x90, 0x00))
        self.assertEqual(connection.commands[1], [0x00, 0xC0, 0x00, 0x00, 0x02])

    def test_apdu_transport_retries_the_card_supplied_length(self):
        class Connection:
            def __init__(self):
                self.commands = []

            def transmit(self, command):
                self.commands.append(command)
                if len(self.commands) == 1:
                    return [], 0x6C, 0x09
                return list(range(9)), 0x90, 0x00

        connection = Connection()
        data, s1, s2 = sim._transmit(connection, [0x00, 0xB0, 0x00, 0x00, 0x00])
        self.assertEqual((len(data), s1, s2), (9, 0x90, 0x00))
        self.assertEqual(connection.commands[1][-1], 9)

    def test_usim_selection_accepts_nested_ef_dir_and_inline_fcp(self):
        aid = sim._hx("A0000000871002FF86FFFF89FFFFFFFF")
        record = [0x61, 7 + len(aid), 0x50, 0x03, 0x55, 0x53, 0x49,
                  0x4F, len(aid), *aid]

        class Connection:
            def __init__(self):
                self.commands = []

            def transmit(self, command):
                self.commands.append(command)
                index = len(self.commands)
                if index == 1:  # MF, no FCP requested
                    return [], 0x90, 0x00
                if index == 2:  # EF_DIR selected; FCP returned inline
                    return [0x62, 0x00], 0x90, 0x00
                if index == 3:
                    return record, 0x90, 0x00
                return [0x62, 0x00], 0x90, 0x00

        connection = Connection()
        self.assertTrue(sim._select_adf_usim(connection))
        self.assertEqual(connection.commands[2], [0x00, 0xB2, 0x01, 0x04, 0x00])
        self.assertEqual(connection.commands[-1][-1], 0x00)

    def test_usim_selection_never_falls_back_to_a_non_usim_application(self):
        csim = sim._hx("A0000003431002")
        record = [0x61, 2 + len(csim), 0x4F, len(csim), *csim]

        class Connection:
            def __init__(self):
                self.calls = 0

            def transmit(self, _command):
                self.calls += 1
                if self.calls <= 2:
                    return [], 0x90, 0x00
                if self.calls == 3:
                    return record, 0x90, 0x00
                return [], 0x6A, 0x83

        self.assertFalse(sim._select_adf_usim(Connection()))


if __name__ == "__main__":
    unittest.main()
