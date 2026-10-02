import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from host import mdd_orchestrator
from host.mdd_orchestrator import Orchestrator
from host.vpcd_modem_bridge import (ChannelArbiter, ModemCard, ModemError, ModemManagerCard,
                                    allocate_logical_channels,
                                    allocate_logical_channels_with_recovery,
                                    logical_channel_metadata, refresh_metadata, serve_slot,
                                    slot_channel_map)


class ManageChannelTests(unittest.TestCase):
    """An LPA opens a logical channel before it can select the ISD-R. The slot is already
    on one, opened with AT+CCHO, and the modem cannot nest another inside it — so the
    bridge answers MANAGE CHANNEL itself. Refusing it failed every eSIM read on a module
    with a bare `euicc_init`, exactly as lpac's PC/SC driver reports that failure."""

    OPEN = bytes.fromhex("0070000001")      # lpac's APDU_OPENLOGICCHANNEL
    @staticmethod
    def close(channel):
        return bytes((0x00, 0x70, 0x80, channel, 0x00))

    def test_open_reports_the_channel_this_slot_already_holds(self):
        rewritten, response = ModemCard.on_channel(self.OPEN, 2)
        self.assertIsNone(rewritten)                       # never reaches the modem
        self.assertEqual(response, bytes.fromhex("029000"))  # lpac requires 3 bytes, SW 9x

    def test_close_is_acknowledged_without_releasing_the_slot(self):
        rewritten, response = ModemCard.on_channel(self.close(2), 2)
        self.assertIsNone(rewritten)
        self.assertEqual(response, bytes.fromhex("9000"))

    def test_closing_the_channel_replaces_the_isdr_session(self):
        """The LPA leaves the ISD-R selected on a channel pin_keeper shares, and pcscd only
        power-cycles the card once every client is gone — so the next ADF.USIM select would
        fail and the line would report NO_CARD with a perfectly good SIM in the reader."""
        card = ModemCard.__new__(ModemCard)
        card.lock = threading.RLock()
        selected = {1: 'USIM', 2: 'ISD-R', 3: 'USIM'}
        calls = []
        def csim(apdu):
            calls.append(apdu.hex())
            if apdu == self.close(2):
                selected.pop(2)
                return bytes.fromhex('9000')
            if apdu == self.OPEN:
                self.assertNotIn(2, selected)
                selected[2] = 'MF'
                return bytes.fromhex('029000')
            return bytes.fromhex('6E00')  # SELECT MF cannot escape this ISD-R session.
        card.csim = csim

        self.assertEqual(card.transmit(self.close(2), 2), bytes.fromhex("9000"))
        self.assertEqual(selected, {1: 'USIM', 2: 'MF', 3: 'USIM'})
        self.assertEqual(calls, ['0070800200', '0070000001'])

    def test_failed_real_close_is_not_acknowledged_and_does_not_open_another_channel(self):
        card = ModemCard.__new__(ModemCard)
        card.lock = threading.RLock()
        with patch.object(card, 'csim', return_value=bytes.fromhex('6A86')) as csim:
            with self.assertRaises(ModemError):
                card.transmit(self.close(2), 2)
        csim.assert_called_once_with(self.close(2))

    def test_unexpected_replacement_never_closes_a_sibling(self):
        card = ModemCard.__new__(ModemCard)
        card.lock = threading.RLock()
        with patch.object(card, 'csim', side_effect=[bytes.fromhex('9000'), bytes.fromhex('019000')]) as csim:
            with self.assertRaises(ModemError):
                card.transmit(self.close(2), 2)
        self.assertEqual([c.args[0] for c in csim.call_args_list], [self.close(2), self.OPEN])

    def test_opening_a_channel_does_not_disturb_the_selection(self):
        card = ModemCard.__new__(ModemCard)
        card.reset_channel = lambda channel: self.fail("open must not reset a held session")

        self.assertEqual(card.transmit(self.OPEN, 1), bytes.fromhex("019000"))

    def test_a_select_after_the_open_is_forced_onto_the_real_channel(self):
        select = bytes.fromhex("01A40400" + "10" + "A0000005591010FFFFFFFF8900000100")
        rewritten, response = ModemCard.on_channel(select, 3)
        self.assertIsNone(response)
        self.assertEqual(rewritten[0], 0x03)
        self.assertEqual(rewritten[1:], select[1:])

    def test_extended_channel_uses_further_interindustry_cla_coding(self):
        select = bytes.fromhex("00A40400" + "10" + "A0000005591010FFFFFFFF8900000100")
        proprietary = bytes.fromhex("80E2000000")

        rewritten, response = ModemCard.on_channel(select, 4)
        self.assertIsNone(response)
        self.assertEqual(rewritten, bytes.fromhex("40") + select[1:])
        rewritten, response = ModemCard.on_channel(proprietary, 19)
        self.assertIsNone(response)
        self.assertEqual(rewritten, bytes.fromhex("CF") + proprietary[1:])

    def test_an_unknown_manage_channel_variant_is_still_refused(self):
        _rewritten, response = ModemCard.on_channel(bytes.fromhex("0070400001"), 1)
        self.assertEqual(response, bytes.fromhex("6A86"))


class ModemBackendTests(unittest.TestCase):
    def test_confirmed_hot_swap_retires_the_bridge_generation(self):
        stopping = threading.Event()
        old = {"iccid": "8900000000000000001", "iccid_verified": True,
               "iccid_source": "card", "imei": "490154203237518"}
        new = {**old, "iccid": "8900000000000000002"}
        card = SimpleNamespace(refresh_identity=Mock(return_value=new))
        published = []
        static = {**logical_channel_metadata([1, 2, 3]), "bridge_pid": 7}
        with patch("host.vpcd_modem_bridge.write_metadata",
                   side_effect=lambda _path, value: published.append(value)), \
                patch("builtins.print"):
            refresh_metadata(card, "/fixture", static, 0, old, stopping)
        self.assertTrue(stopping.is_set())
        self.assertEqual(card.refresh_identity.call_count, 1)
        self.assertEqual(published[-1]["channel_status"], "error")
        self.assertEqual(published[-1]["channel_error"], "card_identity_changed")

    def test_repeated_identity_loss_retires_but_one_missed_sample_recovers(self):
        old = {"iccid": "8900000000000000001", "iccid_verified": True,
               "iccid_source": "card", "imei": "490154203237518"}
        missing = {**old, "iccid": "", "iccid_verified": False,
                   "iccid_source": "unknown"}
        static = {**logical_channel_metadata([1, 2, 3]), "bridge_pid": 7}

        stopping = threading.Event()
        card = SimpleNamespace(refresh_identity=Mock(return_value=missing))
        published = []
        with patch("host.vpcd_modem_bridge.IDENTITY_LOSS_LIMIT", 2), \
                patch("host.vpcd_modem_bridge.write_metadata",
                      side_effect=lambda _path, value: published.append(value)), \
                patch("builtins.print"):
            refresh_metadata(card, "/fixture", static, 0, old, stopping)
        self.assertTrue(stopping.is_set())
        self.assertEqual(card.refresh_identity.call_count, 2)
        self.assertEqual(published[-1]["channel_error"], "card_identity_unavailable")

        stopping = threading.Event()
        samples = iter((missing, old))
        def recover(_previous):
            value = next(samples)
            if value is old:
                stopping.set()
            return value
        card = SimpleNamespace(refresh_identity=Mock(side_effect=recover))
        published = []
        with patch("host.vpcd_modem_bridge.write_metadata",
                   side_effect=lambda _path, value: published.append(value)):
            refresh_metadata(card, "/fixture", static, 0, old, stopping)
        self.assertEqual(card.refresh_identity.call_count, 2)
        self.assertTrue(all(value["channel_status"] == "ready" for value in published))

    def test_periodic_identity_refresh_avoids_unchanged_basic_channel_read(self):
        card = ModemCard.__new__(ModemCard)
        previous = {"imei": "fixture", "iccid": "8900000000000000001",
                    "iccid_verified": True, "iccid_source": "card"}
        with patch.object(card, "cached_iccid", return_value=previous["iccid"]), \
                patch.object(card, "_iccid_from_card") as direct:
            self.assertEqual(card.refresh_identity(previous), previous)
        direct.assert_not_called()

    def test_changed_baseband_cache_requires_new_direct_card_proof(self):
        card = ModemCard.__new__(ModemCard)
        previous = {"iccid": "8900000000000000001", "iccid_verified": True}
        replacement = "8900000000000000002"
        with patch.object(card, "cached_iccid", return_value=replacement), \
                patch.object(card, "_iccid_from_card", return_value=replacement) as direct:
            value = card.refresh_identity(previous)
        direct.assert_called_once_with()
        self.assertTrue(value["iccid_verified"])
        self.assertEqual(value["iccid"], replacement)

    def test_preallocated_slot_emulates_manage_channel_open_and_its_exact_close(self):
        card = ModemCard.__new__(ModemCard)
        with patch.object(card, "csim") as csim, \
                patch.object(card, "reset_channel") as reset_channel:
            self.assertEqual(
                card.transmit(bytes.fromhex("0070000001"), 2),
                bytes.fromhex("029000"))
            self.assertEqual(
                card.transmit(bytes.fromhex("0070800200"), 2),
                bytes.fromhex("9000"))
        csim.assert_not_called()
        reset_channel.assert_called_once_with(2)

    def test_preallocated_slot_rejects_wrong_or_malformed_manage_channel_commands(self):
        card = ModemCard.__new__(ModemCard)
        with patch.object(card, "csim") as csim:
            for apdu in ("0070010001", "0070800100", "0070000000", "0070"):
                self.assertEqual(card.transmit(bytes.fromhex(apdu), 2),
                                 bytes.fromhex("6A86"))
        csim.assert_not_called()

    def test_logical_channel_metadata_exposes_capacity_roles_and_ids(self):
        value = logical_channel_metadata([1, 2, 3])
        self.assertEqual(value["channel_capacity"], 3)
        self.assertEqual(value["channel_allocated"], 3)
        self.assertEqual(value["channel_status"], "ready")
        self.assertEqual(value["logical_channels"], [
            {"slot": 0, "channel": 1, "role": "pin"},
            {"slot": 1, "channel": 2, "role": "swu"},
            {"slot": 2, "channel": 3, "role": "ims"},
        ])

    def test_a_card_that_runs_out_of_channels_is_served_with_what_it_granted(self):
        """A China Unicom USIM grants channel 1 and answers the second OPEN with 6A81.
        Refusing the whole card left a registered modem showing "no SIM" and no VoWiFi."""
        card = self.FakeCard((1, ModemError("MANAGE CHANNEL OPEN failed: 006a81")))
        self.assertEqual(allocate_logical_channels(card, 3), [1])
        self.assertEqual(card.closed, [])

    def test_a_channel_that_stays_duplicated_ends_allocation_without_failing(self):
        card = self.FakeCard((1, 1, 1, 1))
        self.assertEqual(allocate_logical_channels(card, 3), [1])
        # Only a channel that stays duplicated across settle+retry stops allocation.
        self.assertEqual(card.settled, 2)
        self.assertEqual(card.closed, [])

    def test_a_card_that_grants_no_channel_is_still_refused(self):
        card = self.FakeCard((ModemError("MANAGE CHANNEL OPEN failed: 006a81"),))
        with self.assertRaisesRegex(ModemError,
                                    "SIM logical channel allocation failed \\(0/3 allocated\\)"):
            allocate_logical_channels(card, 3)
        self.assertEqual(card.closed, [])

    def test_slots_share_channels_when_the_card_grants_fewer(self):
        self.assertEqual(slot_channel_map([1, 2, 3], 3), [1, 2, 3])
        self.assertEqual(slot_channel_map([1], 3), [1, 1, 1])
        # IMS keeps its own channel: Asterisk gives up on a slow AKA answer.
        self.assertEqual(slot_channel_map([1, 2], 3), [1, 1, 2])

    def test_shared_channel_metadata_reports_every_served_slot(self):
        value = logical_channel_metadata([1], 3, slot_channels=[1, 1, 1])
        self.assertEqual(value["channel_allocated"], 1)
        self.assertEqual(value["slots_served"], 3)
        self.assertTrue(value["channel_shared"])
        self.assertEqual([item["channel"] for item in value["logical_channels"]], [1, 1, 1])
        self.assertFalse(logical_channel_metadata([1, 2, 3])["channel_shared"])

    def test_a_repeated_channel_number_is_retried_before_the_bridge_gives_up(self):
        """A late AT reply read as the answer to the next command repeats the previous
        channel. Failing on the first sighting took both lines down over a transport
        artefact; settling the port and asking again recovers the same SIM."""
        card = self.FakeCard((1, 1, 2, 3))
        self.assertEqual(allocate_logical_channels(card, 3), [1, 2, 3])
        self.assertEqual(card.settled, 1)
        self.assertEqual(card.closed, [])

    def test_clean_bridge_start_does_not_repeat_stale_channel_cleanup(self):
        card = self.FakeCard((1, 2, 3))

        self.assertEqual(allocate_logical_channels_with_recovery(card, 3), [1, 2, 3])
        self.assertEqual(card.closed, [])

    def test_bridge_accepts_three_owned_slots_with_an_extended_channel_number(self):
        card = self.FakeCard((1, 2, 4))

        self.assertEqual(allocate_logical_channels_with_recovery(card, 3), [1, 2, 4])
        self.assertEqual(card.closed, [])

    def test_failed_bridge_start_never_closes_unowned_stale_channels(self):
        card = self.FakeCard((ModemError("no channel available"), 1, 2, 3))

        with self.assertRaisesRegex(ModemError, "without closing unowned channels"):
            allocate_logical_channels_with_recovery(card, 3)
        self.assertEqual(card.closed, [])

    def test_extended_channel_opened_by_this_process_is_retained(self):
        card = ModemCard.__new__(ModemCard)
        card.lock = threading.RLock()
        opened = bytes.fromhex("049000")
        with patch.object(card, "csim", return_value=opened) as csim:
            self.assertEqual(card.open_channel(), 4)
        csim.assert_called_once_with(bytes.fromhex("0070000001"))

    def test_out_of_range_channel_opened_by_this_process_is_closed(self):
        card = ModemCard.__new__(ModemCard)
        card.lock = threading.RLock()
        opened = bytes.fromhex("149000")
        with patch.object(card, "csim", side_effect=[opened, bytes.fromhex("9000")]) as csim:
            with self.assertRaisesRegex(ModemError, "unsupported logical channel allocated: 20"):
                card.open_channel()
        self.assertEqual(
            [call.args[0] for call in csim.call_args_list],
            [bytes.fromhex("0070000001"), bytes.fromhex("0070801400")],
        )

    def test_extended_channel_can_be_recycled_after_lpa_close(self):
        card = ModemCard.__new__(ModemCard)
        card.lock = threading.RLock()
        close = bytes.fromhex("0070800400")
        with patch.object(card, "csim", side_effect=[bytes.fromhex("9000"),
                                                      bytes.fromhex("049000")]) as csim:
            self.assertEqual(card.transmit(close, 4), bytes.fromhex("9000"))
        self.assertEqual([call.args[0] for call in csim.call_args_list],
                         [close, bytes.fromhex("0070000001")])

    class FakeCard:
        def __init__(self, values):
            self.values = iter(values)
            self.closed = []
            self.settled = 0

        def open_channel(self):
            value = next(self.values)
            if isinstance(value, Exception):
                raise value
            return value

        def close_channel(self, channel):
            self.closed.append(channel)

        def settle(self):
            self.settled += 1

    def test_modemmanager_command_backend(self):
        card = ModemManagerCard.__new__(ModemManagerCard)
        card.lock = threading.RLock()
        card.timeout = 10
        card.modem = "0"
        card.debug = False
        result = SimpleNamespace(returncode=0, stdout=b'response: \'+CSIM: 4,"9000"\'\n', stderr=b"")
        with patch("host.vpcd_modem_bridge.subprocess.run", return_value=result) as invoke:
            self.assertEqual(card.csim(bytes.fromhex("00A40000023F00")), bytes.fromhex("9000"))
        self.assertTrue(any(value.startswith("--command=AT+CSIM=14,")
                            for value in invoke.call_args.args[0]))

    def test_modemmanager_tty_mapping(self):
        def fake_run(args, **_kwargs):
            if args == ["mmcli", "-L"]:
                return SimpleNamespace(returncode=0,
                    stdout="/org/freedesktop/ModemManager1/Modem/2\n")
            return SimpleNamespace(returncode=0,
                stdout="modem.generic.ports.value[1] : ttyUSB2 (at)\n")
        with patch.object(mdd_orchestrator, "run", side_effect=fake_run):
            self.assertEqual(Orchestrator.modemmanager_modem_for_tty("/dev/ttyUSB2"),
                             "/org/freedesktop/ModemManager1/Modem/2")

    def test_bridge_stop_waits_for_owned_channel_cleanup_before_force_kill(self):
        app = Orchestrator.__new__(Orchestrator)
        proc = Mock()
        proc.poll.return_value = None
        app.bridges = {"modem-a": proc}
        app.bridge_ports = {"modem-a": 36000}

        app.stop_bridge("modem-a")

        proc.terminate.assert_called_once_with()
        proc.wait.assert_called_once_with(mdd_orchestrator.BRIDGE_STOP_GRACE_SECONDS)
        proc.kill.assert_not_called()
        self.assertNotIn("modem-a", app.bridges)
        self.assertNotIn("modem-a", app.bridge_ports)

    def test_bridge_stop_force_kills_only_after_cleanup_budget_expires(self):
        proc = Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = [
            subprocess.TimeoutExpired("bridge", mdd_orchestrator.BRIDGE_STOP_GRACE_SECONDS),
            0,
        ]

        Orchestrator._stop_bridge_process(proc)

        proc.terminate.assert_called_once_with()
        proc.kill.assert_called_once_with()
        self.assertEqual(proc.wait.call_args_list[0].args,
                         (mdd_orchestrator.BRIDGE_STOP_GRACE_SECONDS,))
        self.assertEqual(proc.wait.call_args_list[1].args, ())

    def test_a_slot_pcscd_never_opens_stops_logging_and_backs_off(self):
        """A reader can expose fewer slots than the modem offers. Retrying that every
        second and logging each attempt writes to the journal forever, which matters on
        hosts whose storage is an SD card."""
        attempts, sleeps, lines = [], [], []

        def refuse(address, timeout=None):
            attempts.append(address)
            if len(attempts) >= 6:
                raise KeyboardInterrupt
            raise ConnectionRefusedError("[Errno 111] Connection refused")

        with patch("host.vpcd_modem_bridge.socket.create_connection", side_effect=refuse), \
                patch("host.vpcd_modem_bridge.time.sleep", side_effect=sleeps.append), \
                patch("builtins.print", side_effect=lambda *a, **k: lines.append(a[0])):
            with self.assertRaises(KeyboardInterrupt):
                serve_slot(SimpleNamespace(channel=3), "127.0.0.1", 36221, 2, b"", False)

        self.assertEqual(len(lines), 1, "an unchanged reason must be reported once")
        self.assertIn("Connection refused", lines[0])
        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 8.0, 16.0])

    def test_a_new_failure_reason_is_always_reported(self):
        reasons = ["[Errno 111] Connection refused", "[Errno 111] Connection refused",
                   "timed out"]
        lines = []

        def fail(address, timeout=None):
            if not reasons:
                raise KeyboardInterrupt
            raise OSError(reasons.pop(0))

        with patch("host.vpcd_modem_bridge.socket.create_connection", side_effect=fail), \
                patch("host.vpcd_modem_bridge.time.sleep"), \
                patch("builtins.print", side_effect=lambda *a, **k: lines.append(a[0])):
            with self.assertRaises(KeyboardInterrupt):
                serve_slot(SimpleNamespace(channel=3), "127.0.0.1", 36221, 2, b"", False)

        self.assertEqual(len(lines), 2)
        self.assertIn("timed out", lines[1])

    def test_pcsc_power_reset_recycles_the_slot_and_retires_failed_generation(self):
        card = SimpleNamespace(reset_channel=Mock(side_effect=ModemError('failed reset')))
        sock = Mock()
        sock.recv.side_effect = [b'\x00\x01', b'\x02']
        with patch('host.vpcd_modem_bridge.socket.create_connection', return_value=sock), \
                patch('builtins.print'):
            serve_slot(ChannelArbiter(card, 3), '127.0.0.1', 36221, 2, b'', False)
        card.reset_channel.assert_called_once_with(3)
        sock.close.assert_called_once()



class SharedChannelTests(unittest.TestCase):
    """Slots sharing one UICC channel must each find the card where they left it."""

    USIM = bytes.fromhex("00A4040410A0000000871002FFFFFFFF8900000100")
    EF_IMSI = bytes.fromhex("00A40004026F07")
    ISIM = bytes.fromhex("00A4040410A0000000871004FFFFFFFF8900000100")
    READ = bytes.fromhex("00B0000009")
    AUTH = bytes.fromhex("00880081" + "22" + "10" + "00" * 16 + "10" + "00" * 16)
    GET_RESPONSE = bytes.fromhex("00C0000035")

    class Card:
        def __init__(self, replies=None):
            self.sent = []
            self.replies = dict(replies or {})

        def transmit(self, apdu, channel):
            self.sent.append((bytes(apdu), channel))
            return self.replies.get(bytes(apdu), bytes.fromhex("9000"))

        @staticmethod
        def closes_logical_channel(apdu, channel):
            return ModemCard.closes_logical_channel(apdu, channel)

        def reset_channel(self, channel):
            self.sent.append((bytes.fromhex("00A40004023F00"), channel))

    def test_a_slot_alone_on_its_channel_never_replays(self):
        card = self.Card()
        arbiter = ChannelArbiter(card, 2)
        for apdu in (self.USIM, self.EF_IMSI, self.READ, self.AUTH):
            arbiter.transmit(1, apdu)
        self.assertEqual([apdu for apdu, _ in card.sent],
                         [self.USIM, self.EF_IMSI, self.READ, self.AUTH])

    def test_the_next_owner_gets_its_selection_back_first(self):
        card = self.Card()
        arbiter = ChannelArbiter(card, 1)
        arbiter.transmit(0, self.USIM)
        arbiter.transmit(0, self.EF_IMSI)
        arbiter.transmit(2, self.ISIM)          # IMS moves the channel to ADF.ISIM
        card.sent.clear()

        arbiter.transmit(0, self.READ)          # PIN keeper resumes its EF_IMSI read
        self.assertEqual([apdu for apdu, _ in card.sent],
                         [self.USIM, self.EF_IMSI, self.READ])
        self.assertTrue(all(channel == 1 for _, channel in card.sent))

    def test_a_slot_that_selected_nothing_is_put_back_on_mf(self):
        card = self.Card()
        arbiter = ChannelArbiter(card, 1)
        arbiter.transmit(2, self.ISIM)
        card.sent.clear()
        arbiter.transmit(1, self.READ)
        self.assertEqual(card.sent[0], (bytes.fromhex("00A40004023F00"), 1))

    def test_a_failed_select_does_not_change_what_is_replayed(self):
        missing = bytes.fromhex("00A40004026F99")
        card = self.Card({missing: bytes.fromhex("6A82")})
        arbiter = ChannelArbiter(card, 1)
        arbiter.transmit(0, self.USIM)
        arbiter.transmit(0, missing)
        arbiter.transmit(2, self.ISIM)
        card.sent.clear()
        arbiter.transmit(0, self.READ)
        self.assertEqual([apdu for apdu, _ in card.sent], [self.USIM, self.READ])

    def test_an_owed_get_response_keeps_the_channel(self):
        """AUTHENTICATE answered 61xx must be followed by GET RESPONSE on the same channel;
        another slot slipping in between would take the other slot's authentication result."""
        card = self.Card({self.AUTH: bytes.fromhex("6135")})
        arbiter = ChannelArbiter(card, 1)
        arbiter.transmit(2, self.ISIM)
        arbiter.transmit(2, self.AUTH)
        order = []
        other = threading.Thread(target=lambda: (arbiter.transmit(0, self.READ),
                                                 order.append("pin")))
        other.start()
        other.join(0.2)
        self.assertTrue(other.is_alive(), "the PIN slot must wait for the owed response")
        arbiter.transmit(2, self.GET_RESPONSE)
        order.append("ims")
        other.join(2)
        self.assertFalse(other.is_alive())
        sent = [apdu for apdu, _ in card.sent]
        self.assertLess(sent.index(self.GET_RESPONSE), sent.index(self.READ))

    def test_an_abandoned_get_response_does_not_block_forever(self):
        card = self.Card({self.AUTH: bytes.fromhex("6135")})
        arbiter = ChannelArbiter(card, 1)
        arbiter.transmit(2, self.AUTH)
        with patch("host.vpcd_modem_bridge.PENDING_RESPONSE_HOLD_SECONDS", 0.05):
            arbiter.pending_deadline = __import__("time").monotonic() + 0.05
            self.assertEqual(arbiter.transmit(0, self.READ), bytes.fromhex("9000"))

    def test_legacy_get_response_holds_the_shared_channel(self):
        card = self.Card({self.AUTH: bytes.fromhex("9F35")})
        arbiter = ChannelArbiter(card, 1)
        arbiter.transmit(2, self.AUTH)
        self.assertEqual(arbiter.pending, 2)
        arbiter.transmit(2, self.GET_RESPONSE)
        self.assertIsNone(arbiter.pending)

    def test_failed_context_restore_does_not_execute_the_requested_read(self):
        card = self.Card()
        arbiter = ChannelArbiter(card, 1)
        arbiter.transmit(0, self.USIM)
        arbiter.transmit(2, self.ISIM)
        card.replies[self.USIM] = bytes.fromhex("6A82")
        card.sent.clear()
        with self.assertRaisesRegex(ModemError, "context"):
            arbiter.transmit(0, self.READ)
        self.assertNotIn((self.READ, 1), card.sent)

    def test_expired_response_is_not_replaced_by_another_transaction(self):
        card = self.Card({self.AUTH: bytes.fromhex("6135")})
        arbiter = ChannelArbiter(card, 1)
        arbiter.transmit(2, self.AUTH)
        arbiter.pending_deadline = 0.0
        arbiter.transmit(0, self.READ)
        card.sent.clear()
        self.assertEqual(arbiter.transmit(2, self.GET_RESPONSE), bytes.fromhex("6985"))
        self.assertNotIn((self.GET_RESPONSE, 1), card.sent)

    def test_incomplete_context_history_fails_closed(self):
        card = self.Card()
        arbiter = ChannelArbiter(card, 1)
        with patch("host.vpcd_modem_bridge.MAX_SELECT_HISTORY", 2):
            arbiter.transmit(0, self.USIM)
            arbiter.transmit(0, self.EF_IMSI)
            arbiter.transmit(0, self.EF_IMSI)
        arbiter.transmit(2, self.ISIM)
        with self.assertRaisesRegex(ModemError, "history"):
            arbiter.transmit(0, self.READ)

    def test_a_reset_puts_only_that_slot_back_on_mf(self):
        card = self.Card()
        arbiter = ChannelArbiter(card, 1)
        arbiter.transmit(0, self.USIM)
        arbiter.transmit(2, self.ISIM)
        arbiter.reset(2)
        card.sent.clear()
        arbiter.transmit(0, self.READ)
        self.assertEqual([apdu for apdu, _ in card.sent], [self.USIM, self.READ])

class ControlLineToleranceTests(unittest.TestCase):
    """pyserial asserts DTR/RTS inside open() with no way to opt out (pyserial#729).
    Virtualised USB passthrough can fail that control transfer with EPROTO, which used to
    kill the whole bridge for two lines an AT channel never uses."""

    def test_missing_control_lines_do_not_cost_the_port(self):
        import errno
        from host.vpcd_modem_bridge import ATSerial, serial as pyserial
        if pyserial is None:
            self.skipTest("pyserial unavailable")
        probe = ATSerial.__new__(ATSerial)
        for errnum in (errno.EPROTO, errno.ENOTTY):
            with patch.object(pyserial.Serial, "_update_dtr_state",
                              side_effect=OSError(errnum, "x")):
                probe._update_dtr_state()
            with patch.object(pyserial.Serial, "_update_rts_state",
                              side_effect=OSError(errnum, "x")):
                probe._update_rts_state()
        # Ensure the destructor of the half-built probe cannot fail the test run.
        probe.is_open = False

    def test_unrelated_failures_still_raise(self):
        import errno
        from host.vpcd_modem_bridge import ATSerial, serial as pyserial
        if pyserial is None:
            self.skipTest("pyserial unavailable")
        probe = ATSerial.__new__(ATSerial)
        with patch.object(pyserial.Serial, "_update_dtr_state",
                          side_effect=OSError(errno.EACCES, "denied")):
            with self.assertRaises(OSError):
                probe._update_dtr_state()
        probe.is_open = False



if __name__ == "__main__":
    unittest.main()
