"""Send and import SMS objects owned by ModemManager.

VoWiFi SMS arrives through Asterisk. When a modem is also registered on the cellular network,
ModemManager independently receives ordinary 3GPP SMS and keeps them as D-Bus SMS objects.
This module uses ModemManager without taking over the modem's serial port and maps each operation
to the saved SIM line by ICCID.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import os
import math
import re
import subprocess
import threading
import time
from datetime import datetime

SMS_PATH_RE = re.compile(r"^/org/freedesktop/ModemManager1/SMS/\d+$")
MODEM_PATH_RE = re.compile(r"/org/freedesktop/ModemManager1/Modem/\d+")
SIM_PATH_RE = re.compile(r"^/org/freedesktop/ModemManager1/SIM/\d+$")
RECIPIENT_RE = re.compile(r"^\+?\d{1,32}$")
BOOT_ID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
DBUS_OWNER_RE = re.compile(r'^s\s+"(:\d+\.\d+)"$')

# A locally-created ModemManager SMS is also visible to the receive poller. Remember it long
# enough for every live Scanner to claim the path, then let each Scanner suppress that object
# until ModemManager removes it. This avoids a second copy alongside the record written by the
# send API while keeping the process-wide registry bounded.
_LOCAL_SMS_TTL = 3600.0
_LOCAL_SMS_LIMIT = 512
_local_sms_paths: OrderedDict[tuple[str, str, str], float] = OrderedDict()
_local_sms_lock = threading.RLock()


# What to do with an SMS object once its message is safely in the database.
#   delete     remove it at once: modem/SIM storage is small (tens of slots) and a full store
#              stops all further delivery, while the database already holds the message.
#   when_full  leave objects in place, deleting the oldest imported ones only when the storage
#              is nearly full.
#   keep       never delete; the operator manages modem storage.
STORAGE_POLICIES = ("delete", "when_full", "keep")
STORAGE_POLICY_ENV = "MDD_CELLULAR_SMS_STORAGE"
STORAGE_LIMIT_ENV = "MDD_CELLULAR_SMS_STORAGE_LIMIT"
# Slots kept free under when_full, so the parts of a long text still have room to arrive.
_STORAGE_HEADROOM = 3
# Assumed capacity when the modem does not answer AT+CPMS? (ModemManager without --debug).
_DEFAULT_STORAGE_LIMIT = 20
_CAPACITY_TTL = 600.0
# The gateway's own object is removed once ModemManager reports it sent. One that never gets
# there -- its send timed out, or the process exited mid-send -- is removed after this long,
# which is far beyond any submit still in progress.
_OWN_OBJECT_GRACE = 600.0
# Give up after a few failures so an object that can never be deleted (a read-only storage, a
# revoked permission) does not put an mmcli call into every five-second poll forever.
_DELETE_ATTEMPTS = 3


def storage_policy(settings: dict | None = None, environ=os.environ) -> str:
    """The configured policy: settings.cellular_sms_storage, else the environment, else delete."""
    for value in ((settings or {}).get("cellular_sms_storage"), environ.get(STORAGE_POLICY_ENV)):
        text = str(value or "").strip().lower().replace("-", "_")
        if text in STORAGE_POLICIES:
            return text
    return "delete"


def storage_limit(environ=os.environ) -> int:
    try:
        value = int(str(environ.get(STORAGE_LIMIT_ENV) or "").strip())
    except ValueError:
        return _DEFAULT_STORAGE_LIMIT
    return value if value > _STORAGE_HEADROOM else _DEFAULT_STORAGE_LIMIT


def _run_json(args: list[str], runner=subprocess.run) -> dict:
    result = runner(["mmcli", *args, "--output-json"], capture_output=True, text=True,
                    timeout=10, check=False)
    if result.returncode:
        return {}
    try:
        value = json.loads(result.stdout or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _modem_paths(runner=subprocess.run) -> list[str]:
    result = runner(["mmcli", "-L"], capture_output=True, text=True, timeout=10, check=False)
    return sorted(set(MODEM_PATH_RE.findall(result.stdout or ""))) if not result.returncode else []


def _invoke(args: list[str], runner, timeout: float):
    """Run one bounded mmcli command without a shell and classify launch failures."""
    try:
        result = runner(["mmcli", *args], capture_output=True, text=True,
                        timeout=timeout, check=False)
        return result, None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "unavailable"
    except Exception:  # A custom runner must not make the HTTP path raise unexpectedly.
        return None, "error"


def _invoke_busctl(args: list[str], runner, timeout: float):
    """Run one bounded system-bus call with the same failure contract as ``_invoke``."""
    try:
        result = runner(["busctl", "--system", "call", *args], capture_output=True, text=True,
                        timeout=timeout, check=False)
        return result, None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "unavailable"
    except Exception:
        return None, "error"


def _decode_json(result) -> dict:
    try:
        value = json.loads(getattr(result, "stdout", "") or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _command_error(result, fallback: str) -> str:
    detail = " ".join(str(getattr(result, "stderr", "") or "").split())
    # mmcli errors are useful to the operator, but cap them before returning them to the UI.
    return detail[:300] if detail else fallback


def _response(instance_id, *, ok: bool, status: str, error: str | None,
              stage: str, modem_path: str | None = None, sms_path: str | None = None,
              uncertain: bool = False, reservation_id: int | None = None) -> dict:
    response = {
        "ok": ok,
        "status": status,
        "error": error,
        "stage": stage,
        "transport": "cellular",
        "instance": str(instance_id),
        "modem_path": modem_path,
        "sms_path": sms_path,
        "unavailable": status == "unavailable",
        "uncertain": uncertain,
    }
    if reservation_id is not None:
        response["_reservation_id"] = int(reservation_id)
    return response


def _content_hash(recipient: str, text: str) -> str:
    """Stable, non-plaintext identity shared by the sender and receive scanner."""
    return hashlib.sha256(f"{recipient}\0{text}".encode("utf-8")).hexdigest()


def _normalize_iccid(value) -> str:
    """Return the canonical comparison/storage form for a modem or configured ICCID."""
    text = str(value or "").strip().casefold()
    # mmcli renders an unreadable property as the literal placeholder "--".
    return "" if text == "--" else text


def _sms_text(value) -> str:
    """Return the readable body of an SMS, or empty when ModemManager has none yet.

    mmcli renders an unreadable property as the literal placeholder "--", and the text of a
    multi-part SMS stays unreadable until every part has arrived. Storing that placeholder
    would both show "--" as a message and, once assembly completes, import the real text as a
    second message, because the import fingerprint covers the body. A genuine one-character
    body of "--" is indistinguishable here and is dropped with it; that is far rarer than an
    incomplete multi-part SMS, which is a routine event on every long message.
    """
    text = str(value or "")
    return "" if text.strip() == "--" else text


# A carrier MMS notification is delivered over the SMS channel as a WAP Push with no readable
# text and a binary WSP payload carrying this MIME type. The marker is the standard one from
# WAP-209-MMSEncapsulation, so it identifies the notification for any carrier without keying
# on a sender number, an SMSC or an MMSC host.
_WAP_PUSH_MMS_MARKER = b"application/vnd.wap.mms-message"
# Give up after a few failures so an object that can never be deleted (a read-only storage, a
# revoked permission) does not put an mmcli call into every five-second poll forever.
_WAP_PUSH_DELETE_ATTEMPTS = 3
# A multipart SMS normally completes in seconds and has been observed taking about ten minutes
# across two networks. After a full day, a still-``receiving`` object is a stranded fragment;
# keeping it forever can exhaust the very small modem store and block every later SMS.
STALE_RECEIVING_SECONDS = 24 * 3600
_SMS_DELETE_RETRY_MAX_SECONDS = 5 * 60


def _sms_data(value) -> bytes:
    """Decode the mmcli rendering of an SMS binary payload; empty when absent or unparsable."""
    if isinstance(value, (list, tuple)):
        try:
            return bytes(int(item) & 0xFF for item in value)
        except (TypeError, ValueError):
            return b""
    text = str(value or "").strip()
    if not text or text == "--":
        return b""
    # mmcli prints the payload as space-separated hex bytes; tolerate other byte separators.
    compact = re.sub(r"[^0-9A-Fa-f]", "", text)
    if not compact or len(compact) % 2:
        return b""
    try:
        return bytes.fromhex(compact)
    except ValueError:
        return b""


def _is_mms_wap_push(content: dict) -> bool:
    """True for a binary MMS notification: no readable text plus the WAP Push MIME marker."""
    if _sms_text(content.get("text")).strip():
        return False
    return _WAP_PUSH_MMS_MARKER in _sms_data(content.get("data"))


def _normalize_imsi(value) -> str:
    """Return the digits-only comparison form of an IMSI, or empty when absent."""
    return re.sub(r"\D", "", str(value or ""))


def _card_key(iccid: str, imsi: str) -> str:
    """Opaque identity for the card currently attached to a physical modem."""
    value = _normalize_iccid(iccid)
    kind = "iccid"
    if not value:
        value = _normalize_imsi(imsi)
        kind = "imsi"
    return (hashlib.sha256(f"{kind}:{value}".encode("ascii", "ignore")).hexdigest()
            if value else "")


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            value = handle.read().strip()
        return value.lower() if BOOT_ID_RE.fullmatch(value) else ""
    except OSError:
        return ""


def _modemmanager_epoch(runner=subprocess.run, boot_id_reader=_boot_id) -> str:
    """Return a non-secret identity for this host boot and ModemManager process.

    ModemManager reuses numeric SMS object paths after its D-Bus service restarts.  Combining
    the kernel boot id with the service's unique D-Bus owner makes a persisted local-send marker
    valid only for the daemon generation that created the object.
    """
    boot_id = boot_id_reader()
    if not boot_id:
        return ""
    try:
        result = runner([
            "busctl", "--system", "call", "org.freedesktop.DBus",
            "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
            "s", "org.freedesktop.ModemManager1",
        ], capture_output=True, text=True, timeout=3, check=False)
    except Exception:
        return ""
    if getattr(result, "returncode", 1):
        return ""
    match = DBUS_OWNER_RE.fullmatch(str(getattr(result, "stdout", "") or "").strip())
    if not match:
        return ""
    return hashlib.sha256(f"{boot_id}\0{match.group(1)}".encode("ascii")).hexdigest()


def _remember_local_sms(modem_path: str, iccid: str, sms_path: str,
                        now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    key = (modem_path, iccid, sms_path)
    with _local_sms_lock:
        for old_key, expiry in list(_local_sms_paths.items()):
            if expiry <= now:
                _local_sms_paths.pop(old_key, None)
        _local_sms_paths.pop(key, None)
        _local_sms_paths[key] = now + _LOCAL_SMS_TTL
        while len(_local_sms_paths) > _LOCAL_SMS_LIMIT:
            _local_sms_paths.popitem(last=False)


def _is_local_sms(modem_path: str, iccid: str, sms_path: str, now: float) -> bool:
    key = (modem_path, iccid, sms_path)
    with _local_sms_lock:
        expiry = _local_sms_paths.get(key)
        if expiry is None:
            return False
        if expiry <= now:
            _local_sms_paths.pop(key, None)
            return False
        return True


def _instance_iccid(instances: list[dict], instance_id) -> str:
    iid = str(instance_id)
    for item in instances or []:
        if isinstance(item, dict) and str(item.get("id")) == iid:
            return _normalize_iccid(item.get("iccid"))
    return ""


def _instance_imsi(instances: list[dict], instance_id) -> str:
    iid = str(instance_id)
    for item in instances or []:
        if isinstance(item, dict) and str(item.get("id")) == iid:
            return _normalize_imsi(item.get("imsi"))
    return ""


def _find_modem(iccid: str, runner, timeout: float, *,
                imsi: str = "") -> tuple[str | None, str | None]:
    """Return (modem_path, problem); a problem is a stable, user-safe description."""
    listing, problem = _invoke(["-L"], runner, timeout)
    if problem == "timeout":
        return None, "Timed out while listing cellular modems."
    if problem or getattr(listing, "returncode", 1):
        return None, "ModemManager is unavailable."

    modem_paths = sorted(set(MODEM_PATH_RE.findall(getattr(listing, "stdout", "") or "")))
    if not modem_paths:
        return None, "No cellular modem is available."

    inspection_failed = False
    for modem_path in modem_paths:
        detail, problem = _invoke(["-m", modem_path, "--output-json"], runner, timeout)
        if problem or getattr(detail, "returncode", 1):
            inspection_failed = True
            continue
        modem_doc = _decode_json(detail)
        modem = modem_doc.get("modem") or {}
        sim_path = str((modem.get("generic") or {}).get("sim")
                       or modem_doc.get("modem.generic.sim") or "")
        if not SIM_PATH_RE.fullmatch(sim_path):
            inspection_failed = True
            continue

        sim_detail, problem = _invoke(["-i", sim_path, "--output-json"], runner, timeout)
        if problem or getattr(sim_detail, "returncode", 1):
            inspection_failed = True
            continue
        sim_doc = _decode_json(sim_detail)
        sim = sim_doc.get("sim") or {}
        properties = sim.get("properties") or {}
        modem_iccid = _normalize_iccid(properties.get("iccid")
                                       or sim_doc.get("sim.properties.iccid"))
        if modem_iccid:
            if modem_iccid == _normalize_iccid(iccid):
                return modem_path, None
            continue
        # Some modules (observed: CMIOT ML307X) reject the EF_ICCID read ModemManager
        # relies on, so the SIM exposes no ICCID at all. Those modules do report the
        # IMSI, which the line also stores; matching on it keeps the SIM's identity
        # authoritative instead of guessing by modem model or port.
        modem_imsi = _normalize_imsi(properties.get("imsi")
                                     or sim_doc.get("sim.properties.imsi"))
        if imsi and modem_imsi and modem_imsi == imsi:
            return modem_path, None

    if inspection_failed:
        return None, "Could not find the line's SIM among the readable cellular modems."
    return None, "No cellular modem matches this line's ICCID or IMSI."


def modem_for_instance(instances: list[dict], instance_id, runner=subprocess.run,
                       timeout: float = 10.0) -> tuple[str | None, str | None]:
    """Resolve one configured line to its live modem without exposing subscriber identity."""
    iccid = _instance_iccid(instances, instance_id)
    if not iccid:
        return None, "The line has no configured ICCID."
    return _find_modem(iccid, runner, timeout,
                       imsi=_instance_imsi(instances, instance_id))


def _created_sms_path(result) -> str:
    doc = _decode_json(result)
    modem = doc.get("modem") or {}
    path = str((modem.get("messaging") or {}).get("created-sms")
               or doc.get("modem.messaging.created-sms") or "")
    if not path:
        # The D-Bus Create reply is printed by busctl as: o "/org/.../SMS/N".
        # Keep accepting the old mmcli JSON shape for compatibility with focused callers/tests.
        match = re.search(r"/org/freedesktop/ModemManager1/SMS/\d+",
                          getattr(result, "stdout", "") or "")
        path = match.group(0) if match else ""
    return path if SMS_PATH_RE.fullmatch(path) else ""


def send(instances: list[dict], instance_id, recipient: str, text: str,
         runner=subprocess.run, *, timeout: float = 30.0, local_sms_tracker=None,
         epoch_getter=_modemmanager_epoch) -> dict:
    """Send an SMS through the modem containing ``instance_id``'s ICCID.

    This synchronous function is intended to be called with ``asyncio.to_thread``. It never
    raises for an ordinary ModemManager failure and always returns ``ok``, ``status``, ``error``,
    ``modem_path`` and ``sms_path``. A send timeout is deliberately reported as ``unknown``:
    retrying automatically could charge for and deliver a duplicate SMS.
    """
    recipient = str(recipient or "").strip()
    if not RECIPIENT_RE.fullmatch(recipient):
        return _response(instance_id, ok=False, status="failed",
                         error="Recipient must contain only digits with an optional leading +.",
                         stage="validate")
    if not isinstance(text, str) or not text or "\0" in text:
        return _response(instance_id, ok=False, status="failed",
                         error="Message text must be non-empty UTF-8 text.", stage="validate")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return _response(instance_id, ok=False, status="failed",
                         error="Message text must be non-empty UTF-8 text.", stage="validate")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        return _response(instance_id, ok=False, status="failed",
                         error="The ModemManager timeout must be positive.", stage="validate")

    iccid = _instance_iccid(instances, instance_id)
    if not iccid:
        return _response(instance_id, ok=False, status="unavailable",
                         error="The line has no configured ICCID.", stage="lookup")
    modem_path, problem = _find_modem(iccid, runner, timeout,
                                      imsi=_instance_imsi(instances, instance_id))
    if not modem_path:
        return _response(instance_id, ok=False, status="unavailable",
                         error=problem or "No cellular modem is available.", stage="lookup")

    status_result, problem = _invoke(
        ["-m", modem_path, "--messaging-status", "--output-json"], runner, timeout)
    if problem == "timeout":
        return _response(instance_id, ok=False, status="unavailable",
                         error="Timed out while checking cellular SMS support.", stage="check",
                         modem_path=modem_path)
    if problem or getattr(status_result, "returncode", 1):
        error = ("Cellular SMS is unavailable on this modem." if problem else
                 _command_error(status_result, "Cellular SMS is unavailable on this modem."))
        return _response(instance_id, ok=False, status="unavailable", error=error,
                         stage="check", modem_path=modem_path)

    # A locally-created submit object is also returned by ModemManager's receive listing. Make a
    # durable intent before creating it so a control-plane restart cannot turn our own outgoing
    # message into a second, apparently successful history row. Sending without this protection
    # is unsafe and therefore refused.
    if local_sms_tracker is None:
        return _response(instance_id, ok=False, status="failed",
                         error="Durable cellular SMS tracking is unavailable.", stage="track",
                         modem_path=modem_path)
    daemon_epoch = epoch_getter()
    if not daemon_epoch:
        return _response(instance_id, ok=False, status="failed",
                         error="Could not identify the active ModemManager service; SMS was not sent.",
                         stage="track", modem_path=modem_path)
    content_hash = _content_hash(recipient, text)
    try:
        reservation_id = local_sms_tracker.reserve_local_modem_sms(
            str(instance_id), iccid, content_hash, daemon_epoch, recipient, text)
    except Exception:
        return _response(instance_id, ok=False, status="failed",
                         error="Could not durably track the cellular SMS; it was not sent.",
                         stage="track", modem_path=modem_path)
    if not reservation_id:
        return _response(instance_id, ok=False, status="failed",
                         error="Could not durably track the cellular SMS; it was not sent.",
                         stage="track", modem_path=modem_path)

    # mmcli 1.20 has no --messaging-create-sms-with-text option, while the underlying D-Bus
    # Create(a{sv}) method has supported Number and Text since ModemManager 1.0. Passing an argv
    # list (never a shell command) preserves punctuation and quotes as one typed string value.
    create_args = [
        "org.freedesktop.ModemManager1", modem_path,
        "org.freedesktop.ModemManager1.Modem.Messaging", "Create",
        "a{sv}", "2", "number", "s", recipient, "text", "s", text,
    ]
    # Scanner takes this same lock while obtaining the modem's SMS path snapshot, so it cannot
    # observe the new object in the tiny gap before its path is registered.
    with _local_sms_lock:
        create_result, problem = _invoke_busctl(create_args, runner, timeout)
        if not problem and not getattr(create_result, "returncode", 1):
            sms_path = _created_sms_path(create_result)
            if sms_path:
                # The D-Bus owner must still be the one that accepted Create. A daemon restart
                # here makes the returned numeric path ambiguous, so never send it through the
                # new owner.
                if epoch_getter() != daemon_epoch:
                    return _response(
                        instance_id, ok=False, status="failed",
                        error="ModemManager restarted while creating the SMS; it was not sent.",
                        stage="track", modem_path=modem_path, sms_path=sms_path,
                        reservation_id=reservation_id)
                try:
                    tracked = bool(local_sms_tracker.bind_local_modem_sms(
                        reservation_id, daemon_epoch, modem_path, sms_path))
                except Exception:
                    tracked = False
                if not tracked:
                    return _response(
                        instance_id, ok=False, status="failed",
                        error="Could not durably bind the cellular SMS object; it was not sent.",
                        stage="track", modem_path=modem_path, sms_path=sms_path,
                        reservation_id=reservation_id)
                _remember_local_sms(modem_path, iccid, sms_path)
            else:
                try:
                    local_sms_tracker.cancel_local_modem_sms(reservation_id)
                except Exception:
                    pass
                return _response(
                    instance_id, ok=False, status="failed",
                    error="ModemManager returned an invalid SMS object path.",
                    stage="create", modem_path=modem_path, reservation_id=reservation_id)
        else:
            sms_path = None

    if problem == "timeout":
        # Create may have succeeded even though its reply timed out. Keep the reservation so a
        # restarted Scanner can claim and suppress the draft object if it appears later.
        return _response(instance_id, ok=False, status="failed",
                         error="Timed out while creating the cellular SMS.", stage="create",
                         modem_path=modem_path, reservation_id=reservation_id)
    if problem or getattr(create_result, "returncode", 1):
        try:
            local_sms_tracker.cancel_local_modem_sms(reservation_id)
        except Exception:
            pass
        error = ("Could not run ModemManager while creating the SMS." if problem else
                 _command_error(create_result, "ModemManager could not create the SMS."))
        return _response(instance_id, ok=False, status="failed", error=error,
                         stage="create", modem_path=modem_path,
                         reservation_id=reservation_id)

    send_result, problem = _invoke(
        ["-s", sms_path, "--send", "--output-json"], runner, timeout)
    if problem == "timeout":
        return _response(
            instance_id, ok=False, status="unknown",
            error="Cellular SMS send timed out; delivery is unknown and was not retried.",
            stage="send", modem_path=modem_path, sms_path=sms_path, uncertain=True,
            reservation_id=reservation_id)
    if problem or getattr(send_result, "returncode", 1):
        error = ("Could not run ModemManager while sending the SMS." if problem else
                 _command_error(send_result, "ModemManager rejected the SMS."))
        return _response(instance_id, ok=False, status="failed", error=error,
                         stage="send", modem_path=modem_path, sms_path=sms_path,
                         reservation_id=reservation_id)
    return _response(instance_id, ok=True, status="sent", error=None, stage="send",
                     modem_path=modem_path, sms_path=sms_path,
                     reservation_id=reservation_id)


_ISO_ZONE_RE = re.compile(r"(?:Z|([+-])(\d{2})(?::?(\d{2}))?)$")


def _timestamp(value) -> int:
    """Epoch seconds of a ModemManager timestamp, independent of the host's time zone.

    ModemManager writes the SMSC's zone as "+10" (hours only), which datetime.fromisoformat
    rejects before Python 3.11, and a value without any zone would silently be read in the
    host's local zone. The zone is normalised to "+HH:MM" first, and a value that still has
    none is refused (0, so the receipt time is used) rather than guessed.
    """
    raw = str(value or "").strip()
    match = _ISO_ZONE_RE.search(raw)
    if not raw or not match:
        return 0
    if match.group(0) == "Z":
        normalised = raw[:match.start()] + "+00:00"
    else:
        normalised = (raw[:match.start()] + f"{match.group(1)}{match.group(2)}:"
                      f"{match.group(3) or '00'}")
    try:
        parsed = datetime.fromisoformat(normalised)
    except (ValueError, OverflowError):
        return 0
    return int(parsed.timestamp()) if parsed.tzinfo is not None else 0


_CPMS_RE = re.compile(r'"(\w+)"\s*,\s*(\d+)\s*,\s*(\d+)')


def _storage_capacity(modem_path: str, runner) -> tuple[str, int, int] | None:
    """(storage, used, total) of the modem's read/delete SMS storage, or None if unreadable.

    ModemManager exposes no capacity, so this asks the modem (AT+CPMS?). The command channel
    exists only while ModemManager runs with --debug; without it the caller falls back to a
    configured object count.
    """
    result, problem = _invoke(["-m", modem_path, "--command=AT+CPMS?"], runner, 10)
    if problem or getattr(result, "returncode", 1):
        return None
    match = _CPMS_RE.search(getattr(result, "stdout", "") or "")
    if not match:
        return None
    total = int(match.group(3))
    return (match.group(1).lower(), int(match.group(2)), total) if total > 0 else None


class Scanner:
    """Incrementally inspect ModemManager without rereading stable objects every five seconds.

    The SMS path listing remains live on every poll, so a newly arrived message is discovered
    without extra delay. Modem/SIM identity and already-seen SMS details are much more stable and
    are refreshed periodically to tolerate ModemManager restarts and object-path reuse.
    """

    def __init__(self, runner=subprocess.run, *, topology_ttl: float = 60.0,
                 detail_ttl: float = 60.0, clock=time.monotonic, wall_clock=time.time,
                 local_sms_tracker=None, epoch_getter=_modemmanager_epoch,
                 drop_mms_wap_push: bool = False, environ=os.environ):
        self.runner = runner
        self.topology_ttl = topology_ttl
        self.detail_ttl = detail_ttl
        self.clock = clock
        self.wall_clock = wall_clock
        self.local_sms_tracker = local_sms_tracker
        self.epoch_getter = epoch_getter
        self.drop_mms_wap_push = False  # binary notifications now enter the MMS store
        self.environ = environ
        self._live_counts = {}
        self._capacity = {}
        self._daemon_epoch = ""
        self._topology_expires = 0.0
        self._topology: list[tuple[str, str]] = []
        self._details: dict[tuple[str, str], tuple[float, dict]] = {}
        self._local_sms_keys: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._wap_push_attempts: dict[tuple[str, str], int] = {}
        self._cleanup_retries: dict[tuple[str, str], tuple[int, float]] = {}
        # Used only for the one-time post-upgrade baseline. Existing objects are retained for
        # explicit cleanup, never silently attributed to a newly selected SIM.
        self._started_wall = float(self.wall_clock())

    def _read(self, sms_path: str) -> dict | None:
        """One SMS object's detail in the scanner's own terms; None when it cannot be read."""
        doc = _run_json(["-s", sms_path], self.runner)
        sms = doc.get("sms")
        if not isinstance(sms, dict):
            return None
        content, props = sms.get("content") or {}, sms.get("properties") or {}
        timestamp = str(props.get("timestamp") or "")
        return {
            "peer": str(content.get("number") or ""),
            "body": _sms_text(content.get("text")),
            "content": content,
            "state": str(props.get("state") or "").lower(),
            "pdu_type": str(props.get("pdu-type") or "").lower(),
            "storage": str(props.get("storage") or "").lower(),
            "ts": _timestamp(timestamp),
            "timestamp_raw": timestamp,
            "signature": hashlib.sha256("\0".join((
                str(content.get("number") or ""), str(content.get("text") or ""),
                str(content.get("data") or ""), str(props.get("pdu-type") or ""),
                timestamp)).encode("utf-8", "surrogatepass")).hexdigest(),
        }

    def _delete_preserved_sms(self, modem_path: str, sms_path: str,
                              daemon_epoch: str) -> bool:
        """Delete one exact object only while it still belongs to this MM generation."""
        if (not MODEM_PATH_RE.fullmatch(str(modem_path))
                or not SMS_PATH_RE.fullmatch(str(sms_path)) or not daemon_epoch):
            return False
        key = (str(modem_path), str(sms_path))
        now = self.clock()
        attempts, retry_at = self._cleanup_retries.get(key, (0, 0.0))
        if now < retry_at:
            return False
        try:
            if self.epoch_getter() != daemon_epoch:
                return False
        except Exception:
            return False
        result, problem = _invoke(
            ["-m", modem_path, f"--messaging-delete-sms={sms_path}"],
            self.runner, 10)
        if problem is None and result is not None and not result.returncode:
            self._cleanup_retries.pop(key, None)
            self._details.pop(key, None)
            self._wap_push_attempts.pop(key, None)
            return True
        attempts += 1
        delay = min(_SMS_DELETE_RETRY_MAX_SECONDS, 5 * (2 ** min(attempts - 1, 6)))
        self._cleanup_retries[key] = (attempts, now + delay)
        return False

    def _preserved(self, record: dict) -> bool:
        tracker = self.local_sms_tracker
        if tracker is None:
            return False
        if record.get("_stale_only"):
            return True  # explicitly bounded, unassembled inbound fragment retention
        if record.get("_local_only"):
            return tracker.local_modem_sms_is_preserved(
                record["instance"], record.get("_modem_iccid") or "",
                record.get("_daemon_epoch") or "", record.get("_modem_path") or "",
                record.get("_sms_path") or "", _content_hash(record["peer"], record["body"]))
        if record.get("data"):
            return tracker.cellular_binary_is_preserved(record["instance"], record["peer"], record["data"])
        return tracker.imported_cellular_message_is_preserved(
            record["fingerprint"], record["instance"], record["direction"], record["peer"],
            record["body"], record["ts"], "cellular")

    def delete_preserved(self, record: dict) -> bool:
        """Recheck generation, actual SIM, object content and durable row before deletion."""
        if not isinstance(record, dict) or not record.get("_signature"):
            return False
        modem_path, sms_path = record.get("_modem_path"), record.get("_sms_path")
        epoch = record.get("_daemon_epoch")
        if not epoch or not modem_path or not sms_path:
            return False
        with _local_sms_lock:
            try:
                if self.epoch_getter() != epoch:
                    return False
                modem = _run_json(["-m", modem_path], self.runner).get("modem") or {}
                sim_path = str((modem.get("generic") or {}).get("sim") or "")
                if not SIM_PATH_RE.fullmatch(sim_path):
                    return False
                props = ((_run_json(["-i", sim_path], self.runner).get("sim") or {}).get("properties") or {})
                iccid, imsi = _normalize_iccid(props.get("iccid")), _normalize_imsi(props.get("imsi"))
                if iccid:
                    if iccid != record.get("_modem_iccid"):
                        return False
                elif not imsi or imsi != record.get("_modem_imsi"):
                    return False
                current = self._read(sms_path)
                if not current or current["signature"] != record["_signature"]:
                    return False
                if record.get("_stale_only") and current["state"] != "receiving":
                    return False
                if not self._preserved(record):
                    return False
                return self._delete_preserved_sms(modem_path, sms_path, epoch)
            except Exception:
                return False

    def _refresh_topology(self, now: float) -> None:
        topology = []
        for modem_path in _modem_paths(self.runner):
            modem = _run_json(["-m", modem_path], self.runner).get("modem") or {}
            sim_path = str((modem.get("generic") or {}).get("sim") or "")
            if not sim_path:
                continue
            sim_doc = _run_json(["-i", sim_path], self.runner).get("sim") or {}
            properties = sim_doc.get("properties") or {}
            iccid = _normalize_iccid(properties.get("iccid"))
            imsi = _normalize_imsi(properties.get("imsi"))
            if iccid or imsi:
                topology.append((modem_path, iccid, imsi))
        if topology != self._topology:
            self._details.clear()
            self._local_sms_keys.clear()
            self._wap_push_attempts.clear()
            self._cleanup_retries.clear()
        self._topology = topology
        # Empty topology is retried quickly so modem hot-plug discovery stays responsive.
        self._topology_expires = now + (self.topology_ttl if topology else min(5.0, self.topology_ttl))

    def discover(self, instances: list[dict], *,
                 include_local_cleanup: bool = False) -> list[dict]:
        """Return displayable cellular SMS records. No message body or identity is logged."""
        now = self.clock()
        daemon_epoch = self.epoch_getter() if self.local_sms_tracker is not None else ""
        if daemon_epoch != self._daemon_epoch:
            # Object paths and their cached details belong to one ModemManager generation.
            self._details.clear()
            self._local_sms_keys.clear()
            self._wap_push_attempts.clear()
            self._cleanup_retries.clear()
            self._daemon_epoch = daemon_epoch
            self._topology_expires = 0.0
        if now >= self._topology_expires:
            self._refresh_topology(now)
        by_iccid = {_normalize_iccid(item.get("iccid")): str(item.get("id")) for item in instances
                    if item.get("iccid") and item.get("id") is not None}
        # Modules that expose no ICCID through ModemManager are matched on IMSI instead.
        # The instance's configured ICCID stays the durable tracking key either way, so a
        # locally sent SMS is classified identically by the sender and this scanner.
        by_imsi = {_normalize_imsi(item.get("imsi")): str(item.get("id")) for item in instances
                   if item.get("imsi") and item.get("iccid") and item.get("id") is not None}
        instance_iccids = {str(item.get("id")): _normalize_iccid(item.get("iccid"))
                           for item in instances if item.get("id") is not None}
        found = []
        live_keys = set()
        live_local_keys = set()
        fences = {}
        begin_fence = getattr(self.local_sms_tracker, "begin_cellular_sms_fence", None)
        for modem_path, modem_iccid, modem_imsi in self._topology:
            if not callable(begin_fence):
                continue
            if modem_iccid:
                fence_iid = by_iccid.get(modem_iccid)
            else:
                fence_iid = by_imsi.get(modem_imsi) if modem_imsi else None
            card_key = _card_key(modem_iccid, modem_imsi)
            if not fence_iid or not card_key:
                continue
            try:
                fence = begin_fence(modem_path, card_key, fence_iid,
                                    now=int(self.wall_clock()))
            except Exception:
                fence = None
            if isinstance(fence, dict):
                fences[modem_path] = {**fence, "card_key": card_key, "instance": fence_iid}
                if fence.get("baseline"):
                    fences[modem_path]["baseline_cutoff"] = self._started_wall - 120
        claim_object = getattr(self.local_sms_tracker, "claim_cellular_sms_object", None)
        quarantine_object = getattr(self.local_sms_tracker, "quarantine_cellular_sms_object", None)
        repair_stale = getattr(self.local_sms_tracker, "repair_stale_cellular_message", None)
        for modem_path, modem_iccid, modem_imsi in self._topology:
            if modem_iccid:
                iid = by_iccid.get(modem_iccid)
            else:
                iid = by_imsi.get(modem_imsi) if modem_imsi else None
            if not iid:
                continue
            iccid = instance_iccids.get(iid) or modem_iccid
            # Serialize the path snapshot with local object creation; otherwise the poller could
            # import a newly-created submit object before send() has learned its D-Bus path.
            with _local_sms_lock:
                listing = _run_json(["-m", modem_path, "--messaging-list-sms"], self.runner)
                raw_paths = listing.get("modem.messaging.sms")
                paths = raw_paths if isinstance(raw_paths, list) else []
                # Only an explicit, fully valid list is authoritative enough for pruning. A
                # command/JSON failure also produces {}, which must never look like an empty
                # modem and erase durable local-send markers.
                listing_complete = (isinstance(raw_paths, list)
                                     and all(SMS_PATH_RE.fullmatch(str(path))
                                             for path in raw_paths))
            self._live_counts[modem_path] = len(paths) if listing_complete else None
            for sms_path in paths:
                sms_path = str(sms_path)
                if not SMS_PATH_RE.match(sms_path):
                    continue
                key = (modem_path, sms_path)
                live_keys.add(key)
                local_key = (modem_path, iccid, sms_path)
                live_local_keys.add(local_key)
                # Durable classification below also checks a content hash, so do not let the
                # process-local path-only cache hide a different object when ModemManager reuses
                # a numeric path. The path-only fallback is retained for compatibility scanners
                # that have no persistence adapter.
                if self.local_sms_tracker is None and (
                        local_key in self._local_sms_keys
                        or _is_local_sms(modem_path, iccid, sms_path, now)):
                    self._local_sms_keys.pop(local_key, None)
                    self._local_sms_keys[local_key] = None
                    while len(self._local_sms_keys) > _LOCAL_SMS_LIMIT:
                        self._local_sms_keys.popitem(last=False)
                    self._details.pop(key, None)
                    continue
                cached = self._details.get(key)
                if cached and now < cached[0]:
                    record = cached[1]
                else:
                    sms = _run_json(["-s", sms_path], self.runner).get("sms") or {}
                    content, props = sms.get("content") or {}, sms.get("properties") or {}
                    text, peer = _sms_text(content.get("text")), str(content.get("number") or "")
                    state = str(props.get("state") or "").lower()
                    pdu_type = str(props.get("pdu-type") or "").lower()
                    timestamp = str(props.get("timestamp") or "")
                    data = _sms_data(content.get("data")) if not text.strip() else b""
                    if state in ("receiving", "sending"):
                        self._details.pop(key, None)
                        received_at = _timestamp(timestamp)
                        if (include_local_cleanup and state == "receiving" and pdu_type == "deliver"
                                and daemon_epoch and received_at
                                and self.wall_clock() - received_at >= STALE_RECEIVING_SECONDS):
                            detail = self._read(sms_path)
                            if detail:
                                found.append({"instance": iid, "_stale_only": True,
                                              "_modem_path": modem_path, "_sms_path": sms_path,
                                              "_daemon_epoch": daemon_epoch, "_modem_iccid": iccid,
                                              "_modem_imsi": modem_imsi,
                                              "_signature": detail["signature"], "ts": received_at})
                        continue
                    if not text.strip() and (not data or pdu_type == "submit"):
                        self._details.pop(key, None)
                        continue
                    direction = "out" if pdu_type == "submit" else "in"
                    signature_parts = [iccid, sms_path, direction, peer, text, timestamp]
                    if direction == "out":
                        # Numeric object paths restart with ModemManager. Outgoing objects often
                        # have no network timestamp, so the daemon generation is required to keep
                        # a new external send from colliding with an old import. Inbound identity
                        # stays backward-compatible and relies on its network timestamp.
                        signature_parts.append(daemon_epoch)
                    signature = "\0".join(signature_parts)
                    record = {
                        "fingerprint": hashlib.sha256(signature.encode()).hexdigest(),
                        "legacy_fingerprint": hashlib.sha256("\0".join(signature_parts[:6]).encode()).hexdigest(),
                        "direction": direction, "peer": peer, "body": text,
                        "ts": _timestamp(timestamp), "transport": "cellular", "data": data,
                        "storage": str(props.get("storage") or "").lower(),
                        "_modem_iccid": iccid, "_modem_imsi": modem_imsi,
                        "_signature": hashlib.sha256("\0".join((peer,
                            str(content.get("text") or ""), str(content.get("data") or ""),
                            str(props.get("pdu-type") or ""), timestamp)).encode(
                                "utf-8", "surrogatepass")).hexdigest(),
                    }
                    if self.local_sms_tracker is not None and daemon_epoch:
                        record.update({
                            "_modem_path": modem_path, "_sms_path": sms_path,
                            "_daemon_epoch": daemon_epoch,
                        })
                    self._details[key] = (now + self.detail_ttl, record)
                fence = fences.get(modem_path)
                if fence and callable(claim_object):
                    try:
                        ownership = claim_object(
                            modem_path, sms_path, fence["card_key"], iid,
                            record.get("_signature") or "", now=int(self.wall_clock()))
                    except Exception:
                        ownership = None
                    if isinstance(ownership, dict):
                        status = ownership.get("status")
                        stale = status == "stale"
                        baseline_old = bool(
                            fence.get("baseline") and status == "new" and
                            (not int(record.get("ts") or 0) or
                             int(record.get("ts") or 0) < fence.get("baseline_cutoff", 0)))
                        if stale or baseline_old:
                            if baseline_old and callable(quarantine_object):
                                try:
                                    quarantine_object(modem_path, sms_path)
                                except Exception:
                                    pass
                            if callable(repair_stale) and record.get("body", "").strip():
                                try:
                                    repaired = repair_stale(
                                        iid, ownership.get("previous_instance", ""),
                                        record.get("peer", ""), record.get("body", ""),
                                        int(record.get("ts") or 0))
                                    if repaired:
                                        # Keep the journal free of senders and message text.
                                        log.info("removed %d stale cellular SMS row(s) on line %s",
                                                 repaired, iid)
                                except Exception:
                                    pass
                            continue
                if record["direction"] == "out" and self.local_sms_tracker is not None:
                    if not daemon_epoch:
                        # Without a daemon generation we cannot distinguish a locally-created
                        # object from a reused path. Delay outgoing import; inbound SMS remains
                        # available and classification is retried on the next poll.
                        continue
                    try:
                        is_local = self.local_sms_tracker.is_local_modem_sms(
                            daemon_epoch, iccid, modem_path, sms_path,
                            _content_hash(record["peer"], record["body"]), record["ts"])
                    except Exception:
                        # A temporary database problem must not turn an already-sent local object
                        # into a new success row. Retry classification on the next polling cycle.
                        continue
                    if is_local:
                        if include_local_cleanup:
                            found.append({
                                **record, "instance": iid, "_local_only": True,
                                "_modem_iccid": iccid,
                            })
                        continue
                found.append({**record, "instance": iid})
            if listing_complete and daemon_epoch and self.local_sms_tracker is not None:
                pruner = getattr(self.local_sms_tracker, "prune_local_modem_sms", None)
                if callable(pruner):
                    try:
                        pruner(daemon_epoch, iccid, modem_path, set(map(str, paths)))
                    except Exception:
                        # Retaining an obsolete marker is safer than losing local-send identity.
                        pass
        # Bound memory when ModemManager deletes SMS objects or a SIM is no longer configured.
        self._details = {key: value for key, value in self._details.items() if key in live_keys}
        self._wap_push_attempts = {key: value for key, value in self._wap_push_attempts.items()
                                   if key in live_keys}
        self._cleanup_retries = {key: value for key, value in self._cleanup_retries.items()
                                 if key in live_keys}
        for key in list(self._local_sms_keys):
            if key not in live_local_keys:
                self._local_sms_keys.pop(key, None)
        return found

    def poll(self, instances: list[dict], ingest=None, *, policy: str = "delete") -> list:
        """Import first; storage policy never substitutes for durable ownership evidence."""
        records = self.discover(instances, include_local_cleanup=ingest is not None)
        if ingest is None:
            return [r for r in records if not r.get("_local_only") and not r.get("_stale_only")]
        policy = policy if policy in STORAGE_POLICIES else "keep"
        found, candidates = [], {}
        for record in records:
            try:
                preserved = self._preserved(record)
                if not preserved and not record.get("_local_only") and not record.get("_stale_only"):
                    stored = ingest(record)
                    if stored:
                        found.append(stored)
                if policy != "keep" and self._preserved(record):
                    if policy == "delete":
                        self.delete_preserved(record)
                    else:
                        candidates.setdefault(record.get("_modem_path"), []).append(record)
            except Exception:
                continue
        for modem, retained in candidates.items():
            if not modem or self._live_counts.get(modem) is None:
                continue
            now = self.clock()
            cached = self._capacity.get(modem)
            capacity = cached[1] if cached and now < cached[0] else _storage_capacity(modem, self.runner)
            self._capacity[modem] = (now + _CAPACITY_TTL, capacity)
            if capacity:
                storage, used, total = capacity
                retained = [r for r in retained if r.get("storage", "") in ("", storage)]
            else:
                used, total = self._live_counts[modem], storage_limit(self.environ)
            excess = max(0, used - total + _STORAGE_HEADROOM)
            for record in sorted(retained, key=lambda r: r.get("ts", 0)):
                if excess <= 0:
                    break
                if self.delete_preserved(record):
                    excess -= 1
                    self._capacity.pop(modem, None)
        return found


def discover(instances: list[dict], runner=subprocess.run, *,
             drop_mms_wap_push: bool = False) -> list[dict]:
    """One-shot compatibility wrapper used by diagnostics and callers outside the poller.

    Deleting an SMS object is the poller's job: a diagnostic read stays free of side effects.
    """
    return Scanner(runner, drop_mms_wap_push=drop_mms_wap_push).discover(instances)
