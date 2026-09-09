"""Privacy-bounded health/AMI evidence, sharing the Engine's log schema and rotation."""
import importlib.util
from pathlib import Path
import re
import threading
import time

from . import config as cfg
from .version import VERSION

_spec = importlib.util.spec_from_file_location(
    "mdd_stability_log", Path(__file__).resolve().parents[2] / "engine" / "stability_log.py")
_writer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_writer)
_samples = {}
_lock = threading.Lock()
SAMPLE_INTERVAL = 300


def event(iid: str, name: str, *, inst: dict | None = None, **facts) -> None:
    iid = str(iid)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", iid):
        return
    if inst:
        from . import egress
        facts.update(country=egress.line_country(inst),
                     plmn=f'{inst.get("mcc", "")}-{inst.get("mnc", "")}')
    _writer.record(Path(cfg.DATA_DIR) / "instances" / iid / "logs", "control", name,
                   version=VERSION, **facts)


def sample(inst: dict, status: dict, runtime: dict) -> None:
    iid = str(inst["id"])
    detail = status.get("detail") or {}
    facts = {"state": status.get("state"), "reason_code": status.get("reason_code"),
             "registration": detail.get("registration", "unknown"),
             "pin_state": (detail.get("pin") or {}).get("state", "unknown"),
             "running": bool(runtime.get("running")), "active_channels": detail.get("active_channels"),
             "retry_count": (status.get("retry") or {}).get("count")}
    signature = tuple(sorted(facts.items()))
    now = time.monotonic()
    with _lock:
        previous, last = _samples.get(iid, (None, 0))
        if previous == signature and now - last < SAMPLE_INTERVAL:
            return
        _samples[iid] = (signature, now)
    try:
        event(iid, "health_changed" if previous != signature else "health_sample", inst=inst, **facts)
    except (OSError, ValueError, TypeError):
        pass  # Diagnostics must not interrupt health publication or recovery.


def ami_event(iid: str, message) -> None:
    """AMI dictionaries can contain identities: extract only a fixed public schema."""
    if str(message.get("Event", "")) != "MDDTransportState":
        return
    facts = {"transport_state": str(message.get("State", "unknown")),
             "protocol": str(message.get("Protocol", "unknown"))}
    for source, target in (("StatusCode", "status_code"), ("DirectionCode", "direction_code")):
        try:
            facts[target] = int(message.get(source))
        except (TypeError, ValueError):
            pass
    event(iid, "ims_transport", **facts)


def client_events(value: dict) -> int:
    if not isinstance(value, dict) or set(value) != {"events"}:
        raise ValueError("invalid client diagnostics")
    events = value["events"]
    fields = {"scope", "outcome", "status_code", "elapsed_ms", "client_epoch", "sequence"}
    if not isinstance(events, list) or not 1 <= len(events) <= 20:
        raise ValueError("invalid client diagnostics")
    for item in events:
        if (not isinstance(item, dict) or set(item) != fields
                or not isinstance(item["scope"], str) or not isinstance(item["outcome"], str)
                or item["scope"] not in _writer.ENUMS["scope"]
                or item["outcome"] not in _writer.ENUMS["outcome"]
                or any(not isinstance(item[key], int) or isinstance(item[key], bool)
                       or not 0 <= item[key] <= 0xFFFFFFFF
                       for key in fields - {"scope", "outcome"})
                or item["status_code"] > 599):
            raise ValueError("invalid client diagnostics")
    directory = Path(cfg.DATA_DIR) / "logs"
    directory.mkdir(mode=0o700, exist_ok=True)
    for item in events:
        if not _writer.record(directory, "control", "client_request", version=VERSION, **item):
            raise OSError("client diagnostics unavailable")
    return len(events)
