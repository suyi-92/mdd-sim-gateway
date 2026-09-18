"""Durable, bounded eSIM-to-cellular recovery intents.

The file is private runtime state, not a log.  It may contain the target ICCID so a Control
restart can prove which card an unfinished task belongs to; client views never expose it.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path


TERMINAL_STATES = {"success", "network_rejected", "failed", "cancelled"}
ACTIVE_STATES = {
    "switching", "profile_enabled", "bridge_recovery", "waiting_flight_mode",
    "waiting_baseband", "automatic_selection", "registering",
}


class RecoveryStore:
    def __init__(self, path: str, *, timeout: float = 900.0, limit: int = 32):
        self.path = Path(path)
        self.timeout = max(60.0, float(timeout))
        self.limit = max(4, int(limit))
        self.lock = threading.RLock()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def schedule(self, device_id: str, iccid: str, reader: str,
                 hardware_generation: str = "") -> dict:
        now = time.time()
        with self.lock:
            document = self._load()
            tasks = document.get("tasks") if isinstance(document.get("tasks"), dict) else {}
            tasks = {str(key): value for key, value in tasks.items()
                     if isinstance(value, dict) and value.get("id") == key}
            for value in tasks.values():
                if (value.get("device_id") == device_id
                        and value.get("state") not in TERMINAL_STATES):
                    value.update(state="cancelled", phase="superseded",
                                 error_code="new_profile_switch", updated_at=now,
                                 finished_at=now)
            task = {
                "id": secrets.token_hex(12), "device_id": str(device_id),
                "iccid": str(iccid), "reader": str(reader),
                "hardware_generation": str(hardware_generation or ""),
                "state": "switching", "phase": "profile_enable",
                "created_at": now, "updated_at": now,
                "deadline_at": now + self.timeout,
            }
            tasks[task["id"]] = task
            ordered = sorted(tasks.values(), key=lambda item: float(item.get("updated_at") or 0))
            tasks = {item["id"]: item for item in ordered[-self.limit:]}
            self._write({"version": 1, "updated_at": now, "tasks": tasks})
            return dict(task)

    def update(self, task_id: str, state: str | None = None, **fields) -> dict | None:
        now = time.time()
        with self.lock:
            document = self._load()
            tasks = document.get("tasks") if isinstance(document.get("tasks"), dict) else {}
            task = tasks.get(str(task_id))
            if not isinstance(task, dict):
                return None
            task.update(fields)
            if state:
                task["state"] = state
            task["updated_at"] = now
            if task.get("state") in TERMINAL_STATES:
                task["finished_at"] = now
            self._write({"version": 1, "updated_at": now, "tasks": tasks})
            return dict(task)

    def active(self) -> list[dict]:
        now = time.time()
        changed = False
        with self.lock:
            document = self._load()
            tasks = document.get("tasks") if isinstance(document.get("tasks"), dict) else {}
            result = []
            for task in tasks.values():
                if not isinstance(task, dict) or task.get("state") in TERMINAL_STATES:
                    continue
                if now >= float(task.get("deadline_at") or 0):
                    task.update(state="failed", phase="expired",
                                error_code="recovery_timeout", updated_at=now,
                                finished_at=now)
                    changed = True
                    continue
                result.append(dict(task))
            if changed:
                self._write({"version": 1, "updated_at": now, "tasks": tasks})
            return result

    def latest(self, device_id: str) -> dict | None:
        with self.lock:
            raw = self._load().get("tasks")
            tasks = raw if isinstance(raw, dict) else {}
            matches = [dict(task) for task in tasks.values()
                       if isinstance(task, dict)
                       and task.get("device_id") == str(device_id)]
        return max(matches, key=lambda item: float(item.get("updated_at") or 0),
                   default=None)

    def latest_all(self) -> list[dict]:
        with self.lock:
            raw = self._load().get("tasks")
            tasks = raw.values() if isinstance(raw, dict) else []
            latest: dict[str, dict] = {}
            for value in tasks:
                if not isinstance(value, dict) or not value.get("device_id"):
                    continue
                device_id = str(value["device_id"])
                if float(value.get("updated_at") or 0) >= float(
                        (latest.get(device_id) or {}).get("updated_at") or 0):
                    latest[device_id] = dict(value)
            return list(latest.values())

    def cancel_device(self, device_id: str, reason: str) -> bool:
        now = time.time()
        changed = False
        with self.lock:
            document = self._load()
            tasks = document.get("tasks") if isinstance(document.get("tasks"), dict) else {}
            for task in tasks.values():
                if (task.get("device_id") == str(device_id)
                        and task.get("state") not in TERMINAL_STATES):
                    task.update(state="cancelled", phase="cancelled", error_code=str(reason),
                                updated_at=now, finished_at=now)
                    changed = True
            if changed:
                self._write({"version": 1, "updated_at": now, "tasks": tasks})
        return changed

    @staticmethod
    def public(task: dict | None) -> dict:
        task = task or {}
        allowed = ("id", "device_id", "state", "phase", "error_code", "created_at",
                   "updated_at", "finished_at", "deadline_at", "network_reject")
        return {key: task[key] for key in allowed if key in task}
