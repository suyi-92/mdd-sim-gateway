"""Durable progress for physical-device capability changes.

The requested booleans are safe to expose to an authenticated browser.  Runtime
exceptions, card identities and generated hardware configuration are deliberately
excluded: callers receive a closed error code instead.  A Control restart cannot
resume an arbitrary Python coroutine safely, so an unfinished record becomes
``interrupted`` while the already-persisted desired device state remains authoritative.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path


ACTIVE_STATES = {"accepted", "running"}
TERMINAL_STATES = {"success", "failed", "interrupted"}
CAPABILITY_FIELDS = {"cellular_enabled", "vowifi_enabled", "flight_mode"}
PHASES = {"queued", "persisting", "stopping", "reconciling", "starting", "complete"}
ERROR_CODES = {
    "busy", "interrupted", "invalid_request", "not_found", "pin_required",
    "pin_invalid", "no_card", "card_mismatch", "card_unreadable",
    "recovery_cancelled", "transition_timeout", "device_unavailable",
    "resume_failed", "failed",
}


class OperationBusy(RuntimeError):
    pass


class CapabilityOperations:
    def __init__(self, path: str | Path, *, limit: int = 64):
        self.path = Path(path)
        self.limit = max(4, int(limit))
        self.lock = threading.RLock()
        self._interrupt_unfinished()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, TypeError, ValueError):
            return {}

    def _write(self, document: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    @staticmethod
    def _records(document: dict) -> list[dict]:
        value = document.get("operations")
        return [dict(item) for item in value if isinstance(item, dict)] \
            if isinstance(value, list) else []

    def _save(self, records: list[dict]) -> None:
        records = sorted(records, key=lambda item: float(item.get("updated_at") or 0))
        records = records[-self.limit:]
        self._write({"version": 1, "updated_at": time.time(), "operations": records})

    def _interrupt_unfinished(self) -> None:
        now = time.time()
        with self.lock:
            records = self._records(self._load())
            changed = False
            for item in records:
                if item.get("state") in ACTIVE_STATES:
                    item.update(state="interrupted", phase="complete",
                                error_code="interrupted", updated_at=now,
                                finished_at=now)
                    changed = True
            if changed:
                self._save(records)

    @staticmethod
    def _target(value: dict) -> dict:
        return {key: bool(value[key]) for key in CAPABILITY_FIELDS if key in value}

    @staticmethod
    def public(value: dict | None) -> dict:
        value = value or {}
        allowed = (
            "operation_id", "device_id", "state", "phase", "target",
            "created_at", "updated_at", "finished_at", "error_code",
        )
        result = {key: value[key] for key in allowed if key in value}
        if "target" in result:
            result["target"] = CapabilityOperations._target(result["target"])
        return result

    def begin(self, device_id: str, target: dict) -> dict:
        now = time.time()
        target = self._target(target)
        if not target:
            raise ValueError("capability target is empty")
        with self.lock:
            records = self._records(self._load())
            active = next((item for item in records if item.get("state") in ACTIVE_STATES), None)
            if active:
                if (str(active.get("device_id") or "") == str(device_id)
                        and self._target(active.get("target") or {}) == target):
                    return self.public(active)
                raise OperationBusy("another device capability operation is running")
            operation = {
                "operation_id": secrets.token_hex(12),
                "device_id": str(device_id),
                "state": "accepted", "phase": "queued", "target": target,
                "created_at": now, "updated_at": now,
            }
            records.append(operation)
            self._save(records)
            return self.public(operation)

    def update(self, operation_id: str, *, state: str | None = None,
               phase: str | None = None, error_code: str = "") -> dict | None:
        now = time.time()
        with self.lock:
            records = self._records(self._load())
            operation = next((item for item in records
                              if item.get("operation_id") == str(operation_id)), None)
            if not operation:
                return None
            if state is not None:
                if state not in ACTIVE_STATES | TERMINAL_STATES:
                    raise ValueError("invalid capability operation state")
                operation["state"] = state
            if phase is not None:
                if phase not in PHASES:
                    raise ValueError("invalid capability operation phase")
                operation["phase"] = phase
            if error_code:
                operation["error_code"] = (error_code if error_code in ERROR_CODES
                                           else "failed")
            elif state == "success":
                operation.pop("error_code", None)
            operation["updated_at"] = now
            if operation.get("state") in TERMINAL_STATES:
                operation["finished_at"] = now
                operation["phase"] = "complete"
            self._save(records)
            return self.public(operation)

    def latest(self, device_id: str) -> dict:
        with self.lock:
            records = [item for item in self._records(self._load())
                       if str(item.get("device_id") or "") == str(device_id)]
            if not records:
                return {}
            return self.public(max(records, key=lambda item: float(item.get("updated_at") or 0)))

    def latest_all(self) -> dict[str, dict]:
        with self.lock:
            latest: dict[str, dict] = {}
            for item in self._records(self._load()):
                device_id = str(item.get("device_id") or "")
                if not device_id:
                    continue
                previous = latest.get(device_id)
                if (not previous or float(item.get("updated_at") or 0)
                        >= float(previous.get("updated_at") or 0)):
                    latest[device_id] = item
            return {device_id: self.public(item) for device_id, item in latest.items()}
