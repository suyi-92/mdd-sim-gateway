"""Capability admission stays closed during real hardware work and remains idempotent."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from control.app import capability_operations, main


class CapabilityBusyApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_real_owner_returns_closed_busy_without_starting_another_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            for owner in ('network', 'rescan', 'hardware', 'other_device', 'other_target'):
                with self.subTest(owner=owner):
                    store = capability_operations.CapabilityOperations(Path(temporary) / (owner + '.json'))
                    lock = asyncio.Lock()
                    if owner == 'hardware':
                        await lock.acquire()
                    if owner in {'other_device', 'other_target'}:
                        store.begin('reader-b' if owner == 'other_device' else 'reader-a',
                                    {'vowifi_enabled': False})
                    with patch.object(main, 'capability_operation_store', store), \
                            patch.object(main, 'capability_lock', lock), \
                            patch.object(main, '_unified_devices', new=AsyncMock(return_value=[
                                {'id': 'reader-a', 'device_type': 'reader'}])), \
                            patch.object(main.network_operations, 'busy', return_value=owner == 'network'), \
                            patch.object(main.operations, 'device_rescan_status', return_value={
                                'state': 'running' if owner == 'rescan' else 'idle'}), \
                            patch.object(main, '_apply_device_capabilities', new=AsyncMock()) as apply:
                        tasks = set(main.capability_tasks)
                        try:
                            with self.assertRaises(main.HTTPException) as caught:
                                await main.api_device_capabilities(
                                    'reader-a', {'vowifi_enabled': True}, background=True)
                            self.assertEqual(caught.exception.status_code, 409)
                            self.assertEqual(caught.exception.detail, {
                                'code': 'busy',
                                'message': 'Another device operation is running. Please wait.'})
                            self.assertEqual(main.capability_tasks, tasks)
                            apply.assert_not_awaited()
                        finally:
                            if lock.locked():
                                lock.release()

    async def test_identical_active_target_returns_its_existing_operation_despite_held_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = capability_operations.CapabilityOperations(Path(temporary) / 'operations.json')
            first = store.begin('reader-a', {'vowifi_enabled': True})
            lock = asyncio.Lock()
            with patch.object(main, 'capability_operation_store', store), \
                    patch.object(main, 'capability_lock', lock), \
                    patch.object(main, '_unified_devices', new=AsyncMock(return_value=[
                        {'id': 'reader-a', 'device_type': 'reader'}])), \
                    patch.object(main.network_operations, 'busy', return_value=False), \
                    patch.object(main.operations, 'device_rescan_status', return_value={'state': 'idle'}):
                async with lock:
                    tasks = set(main.capability_tasks)
                    repeated = await main.api_device_capabilities(
                        'reader-a', {'vowifi_enabled': True}, background=True)
                    self.assertEqual(repeated['operation']['operation_id'], first['operation_id'])
                    self.assertEqual(main.capability_tasks, tasks)
