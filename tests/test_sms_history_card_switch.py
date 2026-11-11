"""Saved SMS follows a SIM when its reader or modem changes."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from control.app import config, main, sim, store


CARDS = {
    "a": ("8900000000000000001", "001010000000001"),
    "b": ("8900000000000000002", "001010000000002"),
}
NATIVE = "SCR Prime CCID Reader (fixture) 00 00"
MODEM = [f"VoWiFi Modem fixture-modem 00 {slot:02d}" for slot in range(3)]
PEER = "FIXTURE"


class SmsHistoryCardSwitchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for patcher in (
            patch.multiple(config, DATA_DIR=str(root), CONFIG_PATH=str(root / "config.yaml"),
                           _loaded=None),
            patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(root / "history.sqlite"),
                           PREVIOUS_DB_PATH=str(root / "old.sqlite"),
                           _subscriber_resolver=main._line_subscriber),
            patch.object(main.hub, "cards", {}),
            patch.object(main.hub, "lpa_busy", {}),
            patch.object(main, "_find_running_by_reader", return_value=None),
            patch.object(main, "_auto_start_hotplugged_line", new=AsyncMock()),
            patch.object(main.usbreader, "port_for_index", return_value="fixture-usb"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for iid, (iccid, imsi) in CARDS.items():
            config.upsert_instance({"id": iid, "name": f"fixture-{iid}",
                                    "iccid": iccid, "imsi": imsi, "mcc": "001", "mnc": "01",
                                    "enabled": False, "reader_index": 0})
        store.init()
        self.history = {}
        for iid in CARDS:
            store.ingest_message(iid, "in", PEER, f"fixture {iid} cellular",
                                 transport="cellular", sent_ts=1000)
            store.ingest_message(iid, "in", PEER, f"fixture {iid} vowifi",
                                 transport="vowifi", sent_ts=1100)
            self.history[iid] = store.list_messages(iid, PEER)

    async def insert(self, iid, reader=NATIVE):
        iccid, imsi = CARDS[iid]
        card = sim.CardInfo(reader=reader, reader_index=0, present=True,
                            iccid=iccid, imsi=imsi, mcc="001", mnc="01",
                            pin_enabled=False, pin_tries=3, smsc="")
        identity = {"hardware_id": "fixture-modem", "slots": 3} if reader in MODEM else None
        with patch.object(main, "_probe_inserted_card", new=AsyncMock(return_value=card)), \
                patch.object(main, "_modem_identity_for_reader", return_value=identity), \
                patch.object(main.sim, "list_readers", return_value=MODEM):
            await main._on_card_insert_locked(reader, 0)
            await asyncio.sleep(0)  # Finish the mocked hotplug task before its patch exits.
        matched = main.hub.cards[reader]["matched"]
        self.assertEqual(matched, iid)
        self.assertEqual(main.api_messages(matched, PEER)["messages"], self.history[iid])
        self.assertEqual(len(main.api_threads(matched)["threads"]), 1)
        self.assertEqual({item["id"] for item in config.list_instances()}, set(CARDS))
        return config.get_instance(matched)

    async def test_switching_a_reader_between_cards_restores_each_cards_history(self):
        for iid in ("a", "b", "a"):
            with self.subTest(card=iid):
                await self.insert(iid)
        for iid in CARDS:
            self.assertEqual(store.list_messages(iid, PEER), self.history[iid])

    async def test_same_card_moves_between_modem_and_native_reader_without_new_history(self):
        original = await self.insert("a", MODEM[0])
        self.assertEqual(original["ami_reader"], MODEM[2])
        # The previous reader has gone away before the same card is inserted elsewhere.
        main.hub.cards.clear()
        native = await self.insert("a")
        self.assertFalse(any(key in native for key in ("pin_reader", "swu_reader", "ami_reader")))
        main.hub.cards.clear()
        modem = await self.insert("a", MODEM[0])
        self.assertEqual(modem["ami_reader"], MODEM[2])
        # The on-disk records survive another Control/store initialization too.
        store.init()
        for iid in CARDS:
            self.assertEqual(store.list_messages(iid, PEER), self.history[iid])


if __name__ == "__main__":
    unittest.main()
