"""Replayable eSIM download progress, without download credentials or card identities.

Only closed progress/error codes are stored. Reader names are reduced to an opaque lookup
digest; raw lpac data, activation codes and profile metadata must never enter this file.
An interrupted download is reported, never automatically replayed against the eUICC.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path


ACTIVE_STATES = {"running", "cancelling"}
STATES = ACTIVE_STATES | {"success", "failed", "cancelled"}
STEPS = {
    "queued", "started", "completed", "cancelling",
    "es10a_get_euicc_configured_addresses", "es10b_get_euicc_challenge_and_info",
    "es9p_initiate_authentication", "es10b_authenticate_server",
    "es9p_authenticate_client", "es8p_metadata_parse", "es10b_prepare_download",
    "es9p_get_bound_profile_package", "es10b_load_bound_profile_package",
}
ERROR_CODES = {
    "interrupted", "reader_busy", "card_unavailable", "download_timeout",
    "remote_rejected", "network_transport", "process_error", "unknown_error",
    "profile_already_installed", "not_euicc", "cancelled",
}
def error_code(error: BaseException, category: str = "") -> str:
    """Classify only; no exception text is returned or persisted."""
    detail = f"{getattr(error, 'message', '')} {getattr(error, 'detail', '')}".casefold()
    if "install_failed_due_to_iccid_already_exists_on_euicc" in detail:
        return "profile_already_installed"
    if "euicc_init" in detail:
        return "not_euicc"
    if category == "notification_timeout":
        return "download_timeout"
    return category if category in ERROR_CODES else "unknown_error"


class DownloadBusy(RuntimeError):
    pass


class DownloadOperations:
    def __init__(self, path: str, limit: int = 64):
        self.path = Path(path)
        self.limit = max(4, int(limit))
        self.lock = threading.RLock()

    @staticmethod
    def _key(reader: str) -> str:
        return hashlib.sha256(str(reader).encode("utf-8")).hexdigest()

    @staticmethod
    def _card_key(identity: str) -> str:
        return hashlib.sha256(f"esim-card:{identity}".encode("utf-8")).hexdigest()

    @staticmethod
    def _clean(value: dict) -> dict | None:
        if not isinstance(value, dict):
            return None
        if (not re.fullmatch(r"[0-9a-f]{24}", str(value.get("operation_id") or ""))
                or not isinstance(value.get("state"), str) or value["state"] not in STATES):
            return None
        result = {"operation_id": value["operation_id"], "state": value["state"]}
        if isinstance(value.get("step"), str) and value["step"] in STEPS:
            result["step"] = value["step"]
        if isinstance(value.get("error_code"), str) and value["error_code"] in ERROR_CODES:
            result["error_code"] = value["error_code"]
        if isinstance(value.get("card_key"), str) \
                and re.fullmatch(r"[0-9a-f]{64}", value["card_key"]):
            result["card_key"] = value["card_key"]
        for field in ("generation", "created_at", "updated_at", "finished_at"):
            number = value.get(field)
            if type(number) is int and number >= 0:
                result[field] = number
        return result

    def _load(self) -> dict:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        records = document.get("records") if isinstance(document, dict) else None
        if not isinstance(records, dict):
            return {}
        return {key: cleaned for key, value in records.items()
                if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)
                and (cleaned := self._clean(value)) is not None}

    def _write(self, records: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "records": records}, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _public(record: dict | None) -> dict | None:
        if record is None:
            return None
        return {key: value for key, value in record.items() if key != "card_key"}

    def latest(self, reader: str, card_identity: str = "",
               require_card_proof: bool = False) -> dict | None:
        with self.lock:
            record = self._load().get(self._key(reader))
            if record:
                if require_card_proof and (
                        not card_identity or not record.get("card_key")):
                    return None
                if (card_identity and record.get("card_key")
                        and record["card_key"] != self._card_key(card_identity)):
                    return None
            return self._public(record)

    def start(self, reader: str, generation=None, card_identity: str = "") -> dict:
        with self.lock:
            records = self._load()
            key = self._key(reader)
            if (records.get(key) or {}).get("state") in ACTIVE_STATES:
                raise DownloadBusy("an eSIM download is already running on this reader")
            now = int(time.time())
            record = {"operation_id": secrets.token_hex(12), "state": "running",
                      "step": "queued", "created_at": now, "updated_at": now}
            if type(generation) is int and generation >= 0:
                record["generation"] = generation
            if card_identity:
                record["card_key"] = self._card_key(card_identity)
            records[key] = record
            for old in sorted(records, key=lambda item: records[item].get("updated_at", 0)):
                if len(records) <= self.limit:
                    break
                if old != key and records[old]["state"] not in ACTIVE_STATES:
                    records.pop(old)
            self._write(records)
            return self._public(record)

    def update(self, operation_id: str, event: str, *, step: str = "",
               error_code: str = "", generation=None) -> dict | None:
        with self.lock:
            records = self._load()
            record = next((value for value in records.values()
                           if value["operation_id"] == operation_id), None)
            if not record or record["state"] not in ACTIVE_STATES:
                return self._public(record)
            step = str(step).split(":", 1)[0]
            if event in {"started", "progress"} and record["state"] == "cancelling":
                return dict(record)
            if step in STEPS:
                record["step"] = step
            if type(generation) is int and generation >= 0:
                record["generation"] = generation
            if event == "completed":
                record.update(state="success", step="completed")
            elif event == "cancelling":
                record.update(state="cancelling", step="cancelling")
            elif event == "error":
                code = error_code if error_code in ERROR_CODES else "unknown_error"
                cancelled = record["state"] == "cancelling"
                record.update(state="cancelled" if cancelled else "failed",
                              error_code="cancelled" if cancelled else code)
            elif event not in {"started", "progress"}:
                raise ValueError("unknown eSIM download event")
            record["updated_at"] = int(time.time())
            if record["state"] not in ACTIVE_STATES:
                record["finished_at"] = record["updated_at"]
            self._write(records)
            return self._public(record)

    def interrupt_running(self) -> None:
        """Called on Control startup; old APDU writes are never replayed."""
        with self.lock:
            records = self._load()
            changed = False
            for record in records.values():
                if record["state"] in ACTIVE_STATES:
                    record.update(state="failed", error_code="interrupted",
                                  updated_at=int(time.time()), finished_at=int(time.time()))
                    changed = True
            if changed:
                self._write(records)
