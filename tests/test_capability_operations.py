import json
import tempfile
import unittest
from pathlib import Path

from control.app.capability_operations import CapabilityOperations, OperationBusy


class CapabilityOperationTests(unittest.TestCase):
    def test_operation_survives_a_fresh_store_and_exposes_only_closed_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capabilities.json"
            store = CapabilityOperations(path)
            started = store.begin("modem-a", {"flight_mode": False})
            store.update(started["operation_id"], state="running", phase="reconciling")

            restored = CapabilityOperations(path).latest("modem-a")

            self.assertEqual(restored["state"], "interrupted")
            self.assertEqual(restored["error_code"], "interrupted")
            self.assertEqual(restored["target"], {"flight_mode": False})
            self.assertNotIn("exception", json.dumps(restored))

    def test_one_active_operation_is_idempotent_but_conflicts_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CapabilityOperations(Path(temporary) / "capabilities.json")
            first = store.begin("modem-a", {"vowifi_enabled": True})
            repeated = store.begin("modem-a", {"vowifi_enabled": True})
            self.assertEqual(repeated["operation_id"], first["operation_id"])
            with self.assertRaises(OperationBusy):
                store.begin("modem-b", {"flight_mode": True})

    def test_terminal_result_allows_the_next_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CapabilityOperations(Path(temporary) / "capabilities.json")
            first = store.begin("modem-a", {"cellular_enabled": True})
            finished = store.update(first["operation_id"], state="failed",
                                    error_code="private runtime detail")
            second = store.begin("modem-b", {"flight_mode": False})

            self.assertEqual(finished["error_code"], "failed")
            self.assertNotEqual(second["operation_id"], first["operation_id"])


if __name__ == "__main__":
    unittest.main()
