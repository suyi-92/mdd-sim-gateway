"""Small, private, closed-schema records for multi-night stability diagnosis."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time

MAX_BYTES = 2 * 1024 * 1024
ARCHIVES = 6
RETENTION_SECONDS = 7 * 86400
EVENTS = {
    "session_started", "ike_request_timeout", "child_rekey_sent", "child_rekey_rejected", "child_rekey_retry",
    "child_rekey_fallback", "child_rekey_complete", "ike_rekey_sent", "ike_rekey_rejected",
    "ike_rekey_complete", "tunnel_down", "health_sample", "health_changed",
    "ims_transport", "ims_registry", "recovery_scheduled", "recovery_started",
    "recovery_succeeded", "recovery_failed", "recovery_cancelled", "client_request",
}
ENUMS = {
    "mode": {"pfs", "inherited"},
    "state": {"OK", "STOPPED", "NO_CARD", "PIN_PROBLEM", "EPDG_UNRESOLVED", "TUNNEL_DOWN",
              "REGISTERING", "ERROR", "CONNECTED", "DOWN", "CONNECTING", "unknown"},
    "registration": {"Registered", "Unregistered", "Rejected", "unknown"},
    "transport_state": {"connected", "disconnected", "shutdown", "destroy", "unknown"},
    "protocol": {"TCP", "TCP6", "TLS", "TLS6", "WS", "WSS", "UDP", "UDP6", "unknown"},
    "scope": {"devices", "cards", "instances", "system", "availability"},
    "outcome": {"timeout", "network", "http", "invalid_response", "recovered"},
    "pin_state": {"PIN_DISABLED", "PIN_OK", "PIN_REQUIRED", "PIN_FAIL", "NO_CARD", "READY",
                  "WRONG_CARD", "WRONG_PIN", "PIN_BLOCKED", "unknown"},
    "auth_state": {"AUTH_OK", "AUTH_FAIL", "STARTING", "NO_CARD", "unknown"},
    "reason_code": {"ok", "stopped", "no_card", "wrong_card", "card_mismatch", "pin_wrong",
                    "pin_blocked", "pin_required", "pin_invalid", "epdg_unresolved", "tunnel_network",
                    "tunnel_setup", "tunnel_sim_auth", "tunnel_not_authorized", "tunnel_no_eap",
                    "tunnel_proposal", "reg_unanswered", "reg_rejected", "reg_reauth_failed",
                    "registering", "rekey_timeout", "ike_rekey_timeout", "rekey_send_error",
                    "ike_rekey_send_error", "maintenance_rebuild", "client_engine_failure",
                    "reader_ambiguous", "esim_profile_switch", "manual", "liveness_timeout",
                    "engine_start_failed", "engine_start_error", "unknown"},
}
NUMBERS = {"message_id", "notify_code", "attempt", "elapsed_ms", "sa_age_seconds", "timeout_ms",
           "dh_group", "encryption_id", "integrity_id", "status_code", "direction_code",
           "active_channels", "retry_count", "delay_seconds", "engine_restart_count", "client_epoch", "sequence"}
BOOLEANS = {"running", "card_present", "exit_ready", "imei_valid", "imei_source_matches", "iccid_matches"}


def payload(component: str, event: str, facts: dict, now: float) -> dict | None:
    if component not in {"ike", "control"} or event not in EVENTS:
        return None
    record = {"schema": 1, "ts": round(now, 3), "mono_ms": int(time.monotonic() * 1000),
              "pid": os.getpid(), "component": component, "event": event}
    version = str(facts.get("version") or os.environ.get("MDD_VERSION", "unknown"))
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-vmware\.[0-9]+)?|unknown", version):
        record["version"] = version
    for key, allowed in ENUMS.items():
        if isinstance(facts.get(key), str) and facts[key] in allowed:
            record[key] = facts[key]
    for key in NUMBERS:
        value = facts.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFFFFFF:
            record[key] = value
    for key in BOOLEANS:
        if isinstance(facts.get(key), bool):
            record[key] = facts[key]
    if re.fullmatch(r"[a-z]{2}", str(facts.get("country") or "")):
        record["country"] = facts["country"]
    if re.fullmatch(r"[0-9]{3}-[0-9]{2,3}", str(facts.get("plmn") or "")):
        record["plmn"] = facts["plmn"]
    # No identifiers, addresses, arbitrary text, crypto material or message payloads.
    return record


def _regular(path: Path) -> bool:
    try:
        item = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or item.st_uid != os.geteuid()
            or item.st_mode & 0o077):
        raise OSError("unsafe stability log")
    return True


def record(directory: Path, component: str, event: str, *, now: float | None = None, **facts) -> bool:
    stamp = time.time() if now is None else now
    data = payload(component, event, facts, stamp)
    if data is None:
        return False
    lock = None
    try:
        directory = Path(directory)
        if directory.is_symlink() or not directory.is_dir():
            return False  # Never resurrect a removed line directory.
        lock = os.open(directory / f".stability-{component}.lock",
                       os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lock_info = os.fstat(lock)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or lock_info.st_uid != os.geteuid() or lock_info.st_mode & 0o077):
            return False
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = directory / f"stability-{component}.jsonl"
        paths = [current, *(directory / f"stability-{component}.{n}.jsonl" for n in range(1, ARCHIVES + 1))]
        for path in paths:
            if _regular(path) and path.stat().st_mtime < stamp - RETENTION_SECONDS:
                path.unlink()
        line = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if _regular(current):
            metadata = current.stat()
            old_day = datetime.fromtimestamp(metadata.st_mtime, timezone.utc).date()
            if metadata.st_size + len(line) > MAX_BYTES or old_day != datetime.fromtimestamp(stamp, timezone.utc).date():
                paths[-1].unlink(missing_ok=True)
                for index in range(ARCHIVES, 0, -1):
                    if paths[index - 1].exists():
                        os.replace(paths[index - 1], paths[index])
        fd = os.open(current, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return False
            stream.write(line)
        return True
    except (OSError, ValueError, TypeError):
        return False  # Logging must not break a tunnel or recovery.
    finally:
        if lock is not None:
            os.close(lock)


def ike_event(event: str, **facts) -> None:
    record(Path("/logs"), "ike", event, **facts)
