"""Generation and durable-copy guards retained while merging the new SMS scanner."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import cellular_sms, store
from tests.test_cellular_sms import FakeModemManager, LINE, MODEM, scanner, sms_object


class ScannerGenerationMergeTests(unittest.TestCase):
    def test_restart_refreshes_sim_binding_before_importing_reused_modem_path(self):
        mm = FakeModemManager()
        epoch, clock = ["first"], [0.0]
        worker = scanner(mm, epoch_getter=lambda: epoch[0], clock=lambda: clock[0])
        lines = [*LINE, {"id": "4", "iccid": "card-b"}]
        worker.poll(lines, lambda record: record, policy="keep")
        # A new daemon reuses Modem/0 before the old topology's 60-second TTL expires.
        mm.iccid = "card-b"
        path = mm.add(7, sms_object(text="synthetic generation test"))
        epoch[0], clock[0] = "second", 1.0
        imported = worker.poll(lines, lambda record: record, policy="delete")
        self.assertEqual([record["instance"] for record in imported], ["4"])
        self.assertEqual(mm.deletes(), [path])

    def test_unavailable_daemon_identity_never_authorizes_object_deletion(self):
        mm = FakeModemManager()
        path = mm.add(7, sms_object(text="synthetic epoch test"))
        worker = scanner(mm, epoch_getter=lambda: "")
        worker.poll(LINE, lambda record: record, policy="delete")
        self.assertEqual(mm.deletes(), [])
        self.assertIn(path, mm.objects)

    def _local_message(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(store, DATA_DIR=str(root),
                                        DB_PATH=str(root / "history.sqlite"),
                                        PREVIOUS_DB_PATH=str(root / "absent.sqlite")))
        store.init()
        mm = FakeModemManager()
        peer, body = "6700", "synthetic local copy"
        path = mm.add(7, sms_object(number=peer, text=body, pdu_type="submit", state="sent"))
        reservation = store.reserve_local_modem_sms(
            "3", "card-a", cellular_sms._content_hash(peer, body), "epoch-1", peer, body)
        store.bind_local_modem_sms(reservation, "epoch-1", MODEM, path)
        message = store.local_modem_sms_message(reservation)
        store.set_message_status(message["id"], "sent")
        return mm, path, message["id"]

    def test_keep_policy_preserves_even_a_final_durable_local_send(self):
        mm, path, _message_id = self._local_message()
        imported = scanner(mm, local_sms_tracker=store).poll(
            LINE, lambda record: record, policy="keep")
        self.assertEqual(imported, [])
        self.assertEqual(mm.deletes(), [])
        self.assertIn(path, mm.objects)

    def test_deleted_local_history_does_not_authorize_deleting_its_modem_copy(self):
        mm, path, message_id = self._local_message()
        store.delete_messages("3", [message_id])
        imported = scanner(mm, local_sms_tracker=store).poll(
            LINE, lambda record: record, policy="delete")
        self.assertEqual(imported, [])
        self.assertEqual(mm.deletes(), [])
        self.assertIn(path, mm.objects)


if __name__ == "__main__":
    unittest.main()
