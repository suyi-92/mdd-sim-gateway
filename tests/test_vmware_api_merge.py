"""Regressions at the native Control/upstream API boundary."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from control.app import main


class RetainedSmsRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_reimport_leaves_binary_mms_to_the_normal_poller(self):
        line = {"id": "fixture-line"}
        text = {"instance": line["id"], "body": "retained text", "data": b""}
        binary = {"instance": line["id"], "body": "", "data": b"\x01\x06"}
        empty = {"instance": line["id"], "body": "   ", "data": b""}
        scanner = Mock()
        scanner.discover.return_value = [text, binary, empty]
        with patch.object(main.cfg, "get_instance", return_value=line), \
                patch.object(main.cfg, "list_instances", return_value=[line]), \
                patch.object(main.cellular_sms, "modem_for_instance", return_value=("fixture", None)), \
                patch.object(main.cellular_sms, "Scanner", return_value=scanner) as factory, \
                patch.object(main.store, "reimport_cellular_messages", return_value=[]) as restore, \
                patch.object(main.hub, "cellular_sms_lock", asyncio.Lock()), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.api_messages_reimport_cellular(
                line["id"], {"confirm_id": line["id"]})
        factory.assert_called_once_with(local_sms_tracker=main.store)
        restore.assert_called_once_with(line["id"], [text])
        self.assertEqual(result["retained"], 1)
