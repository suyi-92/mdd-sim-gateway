"""
lpa.py - Thin async wrapper around a locally-built lpac binary (PC/SC APDU + curl HTTP).

lpac speaks NDJSON on stdout (`type: progress` then a final `type: lpa`). We spawn one
process per operation, pin it to a reader via LPAC_APDU_PCSC_DRV_NAME, and surface
progress via an optional callback. Cancellation sends SIGINT so lpac can cancel the
ES9+/ES10b session cleanly.

Concurrency with vowifi's own PC/SC access is handled by the caller (Hub per-reader
locks + engine-running gate in main.py) — lpac itself uses SCARD_SHARE_EXCLUSIVE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import config as cfg
from . import lpa_diagnostics

log = logging.getLogger("vowifi.lpa")

ProgressCb = Callable[[dict], Awaitable[None] | None]
NotificationProcessedCb = Callable[[], Awaitable[None] | None]


class LpaError(Exception):
    """Raised when lpac exits with a non-success payload or cannot be started."""

    def __init__(self, message: str, *, detail: Any = None, code: int = -1,
                 stage: str = "", category: str = "", transport_detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.code = code
        self.stage = stage
        self.category = category
        self.transport_detail = transport_detail

    def diagnostic(self) -> dict:
        result = lpa_diagnostics.diagnostic(
            self.message, self.detail, operation=self.stage, code=self.code,
            category=classify_lpa_error(self))
        transport = lpa_diagnostics.diagnostic(
            "", self.transport_detail, operation="", code=-1, category="unknown_error")
        for field in ("pcsc_code", "status_word"):
            if field in transport:
                result.setdefault(field, transport[field])
        if self.message in lpa_diagnostics.PROFILE_STEPS:
            scoped = lpa_diagnostics.profile_transport_diagnostic(self.transport_detail)
            if "MDD_LPAC_DIAG begin=profile\n" in self.transport_detail:
                # Ignore unrelated init/cleanup codes when scoped evidence exists.
                for field in ("pcsc_code", "status_word"):
                    result.pop(field, None)
            result.update(scoped)
        return result

    def public_detail(self) -> dict:
        return {"code": "esim_operation_failed", "message": self.user_message(),
                "diagnostic": self.diagnostic()}

    def user_message(self) -> str:
        """User-facing text without raw helper/server payloads or card identifiers."""
        if _reader_busy_error(self):
            return "The eSIM reader is temporarily busy. Wait for the current card operation, then retry."
        msg = (self.message or "").strip().lower()
        detail = self.detail
        detail_s = ""
        if isinstance(detail, str) and detail.strip():
            detail_s = detail.strip()
        elif detail not in (None, "", {}):
            detail_s = str(detail)
        reason = self.diagnostic().get("reason")
        if reason in lpa_diagnostics.REASON_MESSAGES:
            return lpa_diagnostics.REASON_MESSAGES[reason]
        if msg == "euicc_init" or msg.startswith("euicc_init"):
            return "Could not initialize eSIM access. The card type and profile state are unconfirmed."
        if msg in ("cancelled", "cancel"):
            return "Operation cancelled."
        if "timed out" in msg:
            return "eSIM operation timed out. Try again."
        if "install_failed_due_to_iccid_already_exists_on_euicc" in detail_s.lower():
            return ("This eSIM profile is already installed on this eUICC. "
                    "Refresh the profile list instead of downloading it again.")
        return "The eSIM operation failed. The card's detailed result could not be confirmed."


@dataclass
class LpaResult:
    data: Any = None
    progress: list[dict] = field(default_factory=list)


def _classification_text(error: LpaError) -> str:
    if error.message in lpa_diagnostics.PROFILE_STEPS:
        scoped = lpa_diagnostics.profile_transport_text(error.transport_detail)
        if scoped is not None:
            detail = error.detail if isinstance(error.detail, str) else ""
            reason = detail if detail.casefold() in lpa_diagnostics.PROFILE_REASONS else ""
            return f"{error.message} {reason} {scoped}".casefold()
    return f"{error.message} {error.detail} {error.transport_detail}".casefold()


def _reader_busy_error(error: LpaError) -> bool:
    detail = _classification_text(error)
    return any(value in detail for value in ("8010000b", "scard_e_sharing_violation", "sharing violation"))


def classify_lpa_error(error: BaseException) -> str:
    """Reduce an LPA failure to a closed, identity-free diagnostic category."""
    if isinstance(error, asyncio.CancelledError):
        return "interrupted"
    if not isinstance(error, LpaError):
        return "unknown_error"
    if error.category:
        return error.category
    if _reader_busy_error(error):
        return "reader_busy"
    text = _classification_text(error)
    if any(value in text for value in (
        "scard_e_no_smartcard", "scard_w_removed_card", "no smartcard",
        "no smart card", "card absent", "card removed", "card not present",
    )):
        return "card_unavailable"
    if "timed out" in text or "timeout" in text:
        return "notification_timeout"
    if any(value in text for value in (
        "cancelled", "canceled", "interrupted", "sigint",
    )):
        return "interrupted"
    if "functionexecutionstatus" in text and "failed" in text:
        return "remote_rejected"
    if any(value in text for value in (
        "http 400", "http 401", "http 403", "http 404", "http 409",
        "forbidden", "unauthorized", "remote rejected", "server rejected",
    )):
        return "remote_rejected"
    if any(value in text for value in (
        "curl", "network", "dns", "resolve", "connection refused",
        "connection reset", "tls", "certificate", "transport",
    )):
        return "network_transport"
    if any(value in text for value in (
        "failed to spawn", "produced no result", "broken pipe", "exit=",
    )):
        return "process_error"
    if str(error.message).strip() == "euicc_init":
        return "card_unavailable"
    return "unknown_error"


# Active download process per reader name — used by cancel_download().
_active: dict[str, asyncio.subprocess.Process] = {}


def lpac_bin() -> str:
    settings = cfg.get_settings()
    path = (settings.get("esim") or {}).get("lpac_bin") or ""
    if path:
        return path
    return cfg.default_lpac_bin()


def download_timeout() -> float:
    settings = cfg.get_settings()
    try:
        return float((settings.get("esim") or {}).get("download_timeout") or 300)
    except (TypeError, ValueError):
        return 300.0


def auto_process_notifications() -> bool:
    settings = cfg.get_settings()
    return bool((settings.get("esim") or {}).get("auto_process_notifications", True))


def lpac_available() -> bool:
    return os.path.isfile(lpac_bin()) and os.access(lpac_bin(), os.X_OK)


def _env_for_reader(reader_name: str | None, aid: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LIBEUICC_DEBUG_APDU", None)
    env["LPAC_APDU"] = "pcsc"
    env["LPAC_HTTP"] = "curl"
    # Clear any host-level overrides that would pick the wrong reader / ISD-R.
    env.pop("LPAC_APDU_PCSC_DRV_IFID", None)
    env.pop("DRIVER_IFID", None)
    if reader_name:
        env["LPAC_APDU_PCSC_DRV_NAME"] = reader_name
    else:
        env.pop("LPAC_APDU_PCSC_DRV_NAME", None)
        env.pop("DRIVER_NAME", None)
    # Dual-SE (e.g. ESTKme Max/Plus+): pin lpac to a specific ISD-R AID.
    # Empty/None → lpac default GSMA ISD-R.
    aid_n = (aid or "").strip().upper().replace(" ", "")
    if aid_n:
        env["LPAC_CUSTOM_ISD_R_AID"] = aid_n
    else:
        env.pop("LPAC_CUSTOM_ISD_R_AID", None)
    return env


async def _maybe_await(cb: ProgressCb | None, event: dict):
    if not cb:
        return
    res = cb(event)
    if asyncio.iscoroutine(res) or isinstance(res, Awaitable):
        await res


async def run_lpac(
    *args: str,
    reader_name: str | None = None,
    aid: str | None = None,
    on_progress: ProgressCb | None = None,
    timeout: float | None = None,
    stdin_data: str | None = None,
    track_key: str | None = None,
) -> LpaResult:
    """Run `lpac <args>` and return the final LPA payload data.

    Raises LpaError on non-zero payload code, missing binary, timeout, or cancel.
    """
    started_at = asyncio.get_running_loop().time()
    binary = lpac_bin()
    if not os.path.isfile(binary):
        raise LpaError(
            f"lpac binary not found at {binary}. "
            "The active build is incomplete; repair it through the managed update."
        )
    if not os.access(binary, os.X_OK):
        raise LpaError(f"lpac binary is not executable: {binary}")

    cmd = [binary, *args]
    env = _env_for_reader(reader_name, aid=aid)
    # Profile commands can carry ICCIDs, activation/matching/confirmation codes, IMEIs,
    # nicknames, and SM-DP+ addresses. Keep every argument value out of persistent logs.
    operation = " ".join(args[:2]) or "unknown"
    log.info("lpac exec reader=%s custom_aid=%s operation=%s",
             reader_name or "*", bool(aid), operation)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError as e:
        raise LpaError(f"failed to spawn lpac: {e}") from e

    if track_key:
        _active[track_key] = proc

    if stdin_data is not None and proc.stdin:
        try:
            proc.stdin.write(stdin_data.encode("utf-8"))
            await proc.stdin.drain()
        except Exception:  # noqa
            pass
        try:
            proc.stdin.close()
        except Exception:  # noqa
            pass

    result = LpaResult()
    final: dict | None = None
    stderr_chunks: list[bytes] = []

    async def _drain_stderr():
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    stderr_task = asyncio.create_task(_drain_stderr())

    try:
        assert proc.stdout is not None
        deadline = (asyncio.get_event_loop().time() + timeout) if timeout else None
        while True:
            if deadline is not None:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    _signal_cancel(proc)
                    raise LpaError("lpac timed out", code=-1, stage=operation,
                                   category="notification_timeout")
                try:
                    line_b = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    _signal_cancel(proc)
                    raise LpaError("lpac timed out", code=-1, stage=operation,
                                   category="notification_timeout") from None
            else:
                line_b = await proc.stdout.readline()
            if not line_b:
                break
            line = line_b.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.debug("lpac emitted non-JSON output (%d characters)", len(line))
                continue
            typ = obj.get("type")
            if typ == "progress":
                payload = obj.get("payload") or {}
                event = {
                    "step": payload.get("message") or "",
                    "data": payload.get("data"),
                    "code": payload.get("code", 0),
                }
                result.progress.append(event)
                await _maybe_await(on_progress, event)
            elif typ == "lpa":
                final = obj.get("payload") or {}
            # ignore type=driver etc.
        remaining = max(0.01, deadline - asyncio.get_running_loop().time()) if deadline else None
        try:
            rc = await asyncio.wait_for(proc.wait(), remaining)
        except asyncio.TimeoutError:
            raise LpaError("lpac timed out", stage=operation, category="notification_timeout") from None
        await stderr_task
    finally:
        # The caller's reader lock may only be released after the subprocess exits.
        if proc.returncode is None:
            _signal_cancel(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if not stderr_task.done():
            stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)
        if track_key and _active.get(track_key) is proc:
            _active.pop(track_key, None)

    stderr_txt = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
    if final is None:
        # Process died without a final envelope (e.g. driver failed to open reader).
        detail = stderr_txt or f"exit={rc}"
        raise LpaError(f"lpac produced no result ({detail})", detail=detail,
                       code=rc or -1, stage=operation, category="process_error")

    code = int(final.get("code", -1))
    message = final.get("message") or ("success" if code == 0 else "error")
    data = final.get("data")
    if code != 0:
        detail = data if data not in (None, "", {}) else stderr_txt or None
        error = LpaError(str(message), detail=detail, code=code, stage=operation,
                         transport_detail=stderr_txt)
        log.warning("lpac failed diagnostic=%s", json.dumps(error.diagnostic(), sort_keys=True))
        raise error

    result.data = data
    log.info("lpac complete operation=%s duration_ms=%d",
             operation, round((asyncio.get_running_loop().time() - started_at) * 1000))
    return result


def _signal_cancel(proc: asyncio.subprocess.Process):
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
    except ProcessLookupError:
        pass
    except Exception as e:  # noqa
        log.debug("lpac cancel signal failed: %r", e)
        try:
            proc.kill()
        except Exception:  # noqa
            pass


async def cancel_download(reader_name: str) -> bool:
    """Send SIGINT to an in-flight download for this reader. Returns True if a process was signalled."""
    proc = _active.get(reader_name)
    if not proc or proc.returncode is not None:
        return False
    _signal_cancel(proc)
    return True


# ----------------------------- high-level ops -----------------------------

async def chip_info(reader_name: str, *, aid: str | None = None) -> dict:
    r = await run_lpac("chip", "info", reader_name=reader_name, aid=aid, timeout=60)
    data = r.data or {}
    # Normalize a few fields the UI cares about.
    eid = data.get("eidValue") or data.get("eid") or ""
    addrs = data.get("EuiccConfiguredAddresses") or {}
    info2 = data.get("EUICCInfo2") or {}
    ext = info2.get("extCardResource") or {}
    return {
        "eid": eid,
        "defaultDpAddress": addrs.get("defaultDpAddress"),
        "rootDsAddress": addrs.get("rootDsAddress"),
        "freeNonVolatileMemory": ext.get("freeNonVolatileMemory"),
        "freeVolatileMemory": ext.get("freeVolatileMemory"),
        "sasAccreditationNumber": (
            info2.get("sasAccreditationNumber") or info2.get("sasAcreditationNumber")
        ),
        "raw": data,
    }


async def profile_list(reader_name: str, *, aid: str | None = None, timeout: float = 60) -> list[dict]:
    r = await run_lpac("profile", "list", reader_name=reader_name, aid=aid, timeout=timeout)
    data = r.data
    if data is None:
        return []
    if isinstance(data, list):
        return data
    return []


async def profile_enable(
    reader_name: str,
    iccid: str,
    *,
    aid: str | None = None,
    on_notifications_processed: NotificationProcessedCb | None = None,
    process_notifications: bool = True,
    on_notification_status: ProgressCb | None = None,
    refresh: bool = True,
) -> Any:
    r = await run_lpac(
        "profile", "enable", iccid, "1" if refresh else "0",
        reader_name=reader_name, aid=aid, timeout=90)
    if process_notifications:
        await maybe_process_notifications(
            reader_name, aid=aid, on_processed=on_notifications_processed,
            on_status=on_notification_status)
    return r.data


async def profile_disable(
    reader_name: str,
    iccid: str,
    *,
    aid: str | None = None,
    on_notifications_processed: NotificationProcessedCb | None = None,
) -> Any:
    r = await run_lpac(
        "profile", "disable", iccid, reader_name=reader_name, aid=aid, timeout=90)
    await maybe_process_notifications(
        reader_name, aid=aid, on_processed=on_notifications_processed)
    return r.data


async def profile_delete(
    reader_name: str,
    iccid: str,
    *,
    aid: str | None = None,
    on_notifications_processed: NotificationProcessedCb | None = None,
) -> Any:
    r = await run_lpac(
        "profile", "delete", iccid, reader_name=reader_name, aid=aid, timeout=90)
    await maybe_process_notifications(
        reader_name, aid=aid, on_processed=on_notifications_processed)
    return r.data


async def profile_nickname(
    reader_name: str, iccid: str, nickname: str, *, aid: str | None = None,
) -> Any:
    r = await run_lpac(
        "profile", "nickname", iccid, nickname,
        reader_name=reader_name, aid=aid, timeout=60,
    )
    return r.data


async def download(
    reader_name: str,
    *,
    activation_code: str | None = None,
    smdp: str | None = None,
    matching_id: str | None = None,
    confirmation_code: str | None = None,
    imei: str | None = None,
    aid: str | None = None,
    on_progress: ProgressCb | None = None,
    on_notifications_processed: NotificationProcessedCb | None = None,
    interactive_preview: bool = False,
    stdin_data: str | None = None,
) -> Any:
    args = ["profile", "download"]
    if activation_code:
        args += ["-a", activation_code]
    else:
        if smdp:
            args += ["-s", smdp]
        if matching_id:
            args += ["-m", matching_id]
    if confirmation_code:
        args += ["-c", confirmation_code]
    if imei:
        args += ["-i", imei]
    if interactive_preview:
        args += ["-p"]
    r = await run_lpac(
        *args,
        reader_name=reader_name,
        aid=aid,
        on_progress=on_progress,
        timeout=download_timeout(),
        stdin_data=stdin_data,
        track_key=reader_name,
    )
    await maybe_process_notifications(
        reader_name, aid=aid, on_processed=on_notifications_processed)
    return r.data


async def discovery(
    reader_name: str,
    *,
    imei: str | None = None,
    smds: str | None = None,
    aid: str | None = None,
) -> Any:
    args = ["profile", "discovery"]
    if smds:
        args += ["-s", smds]
    if imei:
        args += ["-i", imei]
    r = await run_lpac(*args, reader_name=reader_name, aid=aid, timeout=120)
    return r.data


async def notification_list(reader_name: str, *, aid: str | None = None) -> list[dict]:
    r = await run_lpac(
        "notification", "list", reader_name=reader_name, aid=aid, timeout=60)
    data = r.data
    if isinstance(data, list):
        return data
    raise LpaError("notification list returned an invalid result")


async def notification_process(
    reader_name: str,
    seq: int | None = None,
    *,
    all_notifications: bool = False,
    autoremove: bool = True,
    aid: str | None = None,
    timeout: float = 180,
) -> Any:
    args = ["notification", "process"]
    if all_notifications:
        args.append("-a")
    if autoremove:
        args.append("-r")
    if seq is not None and not all_notifications:
        args.append(str(seq))
    r = await run_lpac(*args, reader_name=reader_name, aid=aid, timeout=timeout)
    return r.data


async def notification_remove(
    reader_name: str,
    seq: int | None = None,
    *,
    all_notifications: bool = False,
    aid: str | None = None,
) -> Any:
    args = ["notification", "remove"]
    if all_notifications:
        args.append("-a")
    elif seq is not None:
        args.append(str(seq))
    r = await run_lpac(*args, reader_name=reader_name, aid=aid, timeout=60)
    return r.data


async def maybe_process_notifications(
    reader_name: str,
    *,
    aid: str | None = None,
    on_processed: NotificationProcessedCb | None = None,
    on_status: ProgressCb | None = None,
) -> bool:
    """Bounded best-effort delivery; only transient PC/SC sharing conflicts retry."""
    started_at = asyncio.get_running_loop().time()

    async def report(state, attempts, reason_code="", stage="notification_process"):
        if on_status:
            try:
                result = on_status({
                    "state": state, "attempts": attempts, "reason_code": reason_code,
                    "stage": stage,
                    "elapsed_ms": max(0, round(
                        (asyncio.get_running_loop().time() - started_at) * 1000)),
                })
                if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                    await result
            except Exception as error:  # noqa - feedback cannot turn an enabled profile into a failure
                log.warning("notification status callback failed error=%s", type(error).__name__)

    if not auto_process_notifications():
        await report("disabled", 0)
        return False
    deadline = asyncio.get_running_loop().time() + 45
    attempts = 0
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(.75)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            await report("failed", attempts, "notification_timeout")
            return False
        attempts = attempt + 1
        await report("processing", attempts)
        try:
            await notification_process(reader_name, all_notifications=True, autoremove=True,
                                       aid=aid, timeout=remaining)
            break
        except LpaError as error:
            busy = _reader_busy_error(error)
            if busy and attempts < 3:
                continue
            code = classify_lpa_error(error)
        except asyncio.CancelledError:
            await report("failed", attempts, "interrupted")
            raise
        except Exception as error:  # noqa - do not publish arbitrary endpoint/card details
            code = classify_lpa_error(error)
        log.warning("auto notification process failed reader=%s code=%s attempts=%d",
                    reader_name, code, attempts)
        await report("failed", attempts, code)
        return False
    if on_processed:
        try:
            result = on_processed()
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                await result
        except Exception as e:  # noqa - delivery succeeded; cache/UI refresh is best effort
            log.warning("notification post-process hook failed reader=%s error=%s",
                        reader_name, type(e).__name__)
    await report("processed", attempts)
    return True


async def load_all_ses(reader_name: str, reader_index: int = 0) -> dict:
    """Discover SEs and load chip + profiles + notifications for each.

    Returns {ses: [{id,label,aid,eid,freeSpace,...,profiles,notifications}], dual: bool}.
    """
    from . import estkme

    ses = await asyncio.to_thread(estkme.discover_ses, reader_name, reader_index)
    out: list[dict] = []
    for se in ses:
        aid = se.get("aid")
        entry = {
            "id": se["id"],
            "label": se["label"],
            "aid": aid,
            "eid": None,
            "freeSpace": None,
            "defaultDpAddress": None,
            "rootDsAddress": None,
            "profiles": [],
            "notifications": [],
            "notifications_loaded": False,
            "error": None,
        }
        try:
            chip = await chip_info(reader_name, aid=aid)
            entry.update(
                eid=chip.get("eid") or None,
                freeSpace=chip.get("freeNonVolatileMemory"),
                defaultDpAddress=chip.get("defaultDpAddress"),
                rootDsAddress=chip.get("rootDsAddress"),
                chip=chip,
            )
            profiles = await profile_list(reader_name, aid=aid)
            for p in profiles:
                if isinstance(p, dict):
                    p = {**p, "seId": se["id"], "seLabel": se["label"], "seEid": entry["eid"]}
                entry["profiles"].append(p)
            try:
                notes = await notification_list(reader_name, aid=aid)
                entry["notifications_loaded"] = True
                entry["notifications_checked_at"] = time.time()
            except LpaError:
                notes = []
            for n in notes:
                if isinstance(n, dict):
                    n = {**n, "seId": se["id"], "seLabel": se["label"], "seEid": entry["eid"]}
                entry["notifications"].append(n)
        except LpaError as e:
            entry["error"] = e.user_message()
            log.info("SE %s load failed reader=%s: %s", se["id"], reader_name, e.message)
        out.append(entry)
    return {"ses": out, "dual": len(out) > 1}
