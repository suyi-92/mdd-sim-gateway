"""Process-local, bounded network jobs independent of HTTP/browser lifetimes."""
from __future__ import annotations

import asyncio
import copy
import secrets
import time
import math
from collections import OrderedDict
from .cellular_network import REGISTER_TOTAL_TIMEOUT_SECONDS


class OperationBusy(RuntimeError):
    pass


class NetworkOperations:
    def __init__(self, limit=64):
        self.limit = limit
        self.records = OrderedDict()
        self.active = None
        self.active_deadline = None
        self.tasks = set()

    def busy(self):
        return self.active is not None

    def _record(self, key):
        if key not in self.records:
            self.records[key] = {"context": secrets.token_hex(12), "networks": [], "operation": None}
        self.records.move_to_end(key)
        for old in list(self.records):
            if len(self.records) <= self.limit:
                break
            if old != self.active and old != key:
                del self.records[old]
        return self.records[key]

    def view(self, key):
        result = {**copy.deepcopy(self._record(key)),
                  "blocked": self.busy() and self.active != key}
        operation = result.get("operation")
        if operation and operation.get("deadline_at") and operation["state"] == "running":
            operation["remaining_seconds"] = max(0, math.ceil((self.active_deadline or time.monotonic()) - time.monotonic()))
        return result

    def reset(self, key):
        if self.busy():
            raise OperationBusy("A cellular network operation is already running.")
        self.records.pop(key, None)
        self._record(key)["selection_reset"] = True
        return self.view(key)

    def progress(self, key, operation_id, phase):
        operation = self._record(key).get("operation")
        if (self.active == key and operation and operation["id"] == operation_id
                and phase in {"registering", "confirming", "restoring"}):
            operation["phase"] = phase

    def deadline(self, key, operation_id):
        operation = self._record(key).get("operation")
        return (self.active_deadline if self.active == key and operation
                and operation["id"] == operation_id else None)

    def start(self, key, action, selection, worker):
        # No await between admission and ownership. A second tab cannot enqueue a
        # scan behind registration and silently disconnect the newly selected PLMN.
        if self.busy():
            raise OperationBusy("A cellular network operation is already running.")
        record = self._record(key)
        record.pop("selection_reset", None)
        operation = {"id": secrets.token_hex(12), "action": action, "state": "running",
                     "selection": copy.deepcopy(selection), "started_at": int(time.time()),
                     "phase": "queued"}
        if action == "apply":
            operation["deadline_at"] = operation["started_at"] + REGISTER_TOTAL_TIMEOUT_SECONDS
        record["operation"] = operation
        self.active = key
        self.active_deadline = time.monotonic() + REGISTER_TOTAL_TIMEOUT_SECONDS if action == "apply" else None

        async def run():
            try:
                result = await worker()
                if isinstance(result.get("networks"), list):
                    record["networks"] = copy.deepcopy(result["networks"][:128])
                operation.update(state="success", result=copy.deepcopy(result))
            except asyncio.CancelledError:
                operation.update(state="failed", error={"code": "interrupted"})
                raise
            except Exception as exc:
                # HTTP helpers supply closed error details; never return a traceback
                # or an arbitrary modem/journal string through the polling endpoint.
                detail = getattr(exc, "detail", None)
                if not isinstance(detail, dict):
                    detail = {"code": "scan_failed" if action == "scan" else "failed"}
                networks = detail.get("networks")
                if isinstance(networks, list):
                    record["networks"] = copy.deepcopy(networks[:128])
                operation.update(state="partial" if isinstance(networks, list) else "failed",
                                 error=copy.deepcopy(detail))
            finally:
                operation["finished_at"] = int(time.time())
                self.active = None
                self.active_deadline = None

        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.view(key)
