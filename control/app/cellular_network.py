"""Bounded ModemManager 3GPP network scan and registration helpers."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time


MODEM_PATH_RE = re.compile(r"^/org/freedesktop/ModemManager1/Modem/\d+$")
OPERATOR_ID_RE = re.compile(r"^\d{5,6}$")
NETWORK_LINE_RE = re.compile(
    r"^(?:\s*3GPP scan\s*\|\s*networks:|\s*\|)\s*"
    r"(?P<code>\d{5,6})\s+-\s*(?P<name>.*?)\s+\((?P<detail>[^()]*)\)\s*$")
SCAN_TIMEOUT_SECONDS = 315
REGISTER_TIMEOUT_SECONDS = 120
NETWORK_STATUSES = {"available", "current", "forbidden", "unknown"}
MM_SERVICE = "org.freedesktop.ModemManager1"
EMPTY_MMCLI_SCAN = "error: couldn't scan networks in the modem: 'unknown error'"
COPS_NETWORK_RE = re.compile(
    r'\(\s*([0-3])\s*,\s*"([^"\x00-\x1f]*)"\s*,\s*'
    r'"([^"\x00-\x1f]*)"\s*,\s*"([0-9]{5,6})"\s*'
    r'(?:,\s*([0-9]{1,2})\s*)?\)')
COPS_CAPABILITIES_RE = re.compile(
    r',*\s*\(\s*[0-4](?:\s*[-,]\s*[0-4])*\s*\)\s*,\s*'
    r'\(\s*[0-2](?:\s*[-,]\s*[0-2])*\s*\)\s*')
COPS_TECHNOLOGIES = {
    0: "gsm", 1: "gsm-compact", 2: "umts", 3: "edge",
    4: "hsdpa", 5: "hsupa", 6: "hspa", 7: "lte", 9: "nb-iot",
}


class CellularNetworkError(RuntimeError):
    pass


class CellularRegistrationError(CellularNetworkError):
    """Closed, identity-free status for registration and its recovery transaction."""

    def __init__(self, code: str, recovery: dict | None = None):
        super().__init__("Cellular network registration failed.")
        self.detail = {"code": code, "message": str(self),
                       "recovery": recovery or {"state": "unchanged"}}


def _error(result, fallback: str) -> str:
    detail = " ".join(str(getattr(result, "stderr", "") or "").split())
    return detail[:300] if detail else fallback


def _merge_networks(rows: list[dict]) -> list[dict]:
    networks: dict[str, dict] = {}
    for row in rows:
        code = row["operator_id"]
        technology = row["access_technology"]
        status = row["status"]
        if status not in NETWORK_STATUSES:
            status = "unknown"
        name = " ".join(row["name"].split())[:100]
        if name.casefold() in {"", "--", "unknown", "none", "n/a"}:
            name = code
        current = networks.get(code)
        if not current:
            networks[code] = {
                "operator_id": code, "name": name,
                "access_technology": technology, "status": status,
            }
            continue
        technologies = {item for item in (
            str(current.get("access_technology") or "").split("/")) if item}
        if technology:
            technologies.add(technology)
        current["access_technology"] = "/".join(sorted(technologies))
        if current["name"] == code and name != code:
            current["name"] = name
        rank = {"current": 3, "available": 2, "forbidden": 1, "unknown": 0}
        if rank[status] > rank[str(current.get("status") or "unknown")]:
            current["status"] = status
    rank = {"current": 0, "available": 1, "unknown": 2, "forbidden": 3}
    return sorted(networks.values(), key=lambda item: (
        rank.get(str(item.get("status")), 2), str(item.get("name") or "").casefold(),
        str(item.get("operator_id") or "")))


def parse_scan_output(value: str) -> list[dict]:
    """Parse stable C-locale mmcli scan rows without accepting arbitrary identifiers."""
    rows = []
    for raw in str(value or "").splitlines():
        match = NETWORK_LINE_RE.fullmatch(raw)
        if not match:
            continue
        detail = match.group("detail").rsplit(",", 1)
        rows.append({
            "operator_id": match.group("code"), "name": match.group("name"),
            "access_technology": re.sub(
                r"[^A-Za-z0-9_+./ -]", "", detail[0]).strip().lower(),
            "status": detail[1].strip().lower() if len(detail) == 2 else "unknown",
        })
    return _merge_networks(rows)


def parse_cops_output(value: str) -> list[dict]:
    """Parse only complete COPS test tuples; never infer networks from capabilities."""
    value = str(value or "").strip()
    invalid = "Cellular network scan returned an invalid response."
    if not value.startswith("+COPS:") or len(value) > 65536:
        raise CellularNetworkError(invalid)
    remaining = value[len("+COPS:"):].strip()
    rows = []
    while remaining:
        if COPS_CAPABILITIES_RE.fullmatch(remaining):
            break
        if rows:
            if not remaining.startswith(","):
                raise CellularNetworkError(invalid)
            remaining = remaining[1:].lstrip()
        match = COPS_NETWORK_RE.match(remaining)
        if not match:
            raise CellularNetworkError(invalid)
        status, name, short_name, code, technology = match.groups()
        rows.append({
            "operator_id": code, "name": name or short_name,
            "access_technology": (COPS_TECHNOLOGIES.get(int(technology), f"act-{technology}")
                                  if technology is not None else ""),
            "status": ("unknown", "available", "current", "forbidden")[int(status)],
        })
        remaining = remaining[match.end():].strip()
    return _merge_networks(rows)


def _prefer_at_scan(modem_path: str, runner) -> bool:
    """Select from live MM ports/plugin, never a saved display name or a guessed tty."""
    try:
        result = runner(["mmcli", "-m", modem_path, "--output-json"],
                        capture_output=True, text=True, timeout=10, check=False,
                        env={**os.environ, "LC_ALL": "C"})
        if result.returncode:
            return False
        generic = json.loads(result.stdout)["modem"]["generic"]
        ports = generic.get("ports") or []
        return (generic.get("plugin") == "quectel" and isinstance(ports, list)
                and any(str(port).endswith(" (qmi)") for port in ports)
                and any(str(port).endswith(" (at)") for port in ports))
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, AttributeError):
        return False


def _at_command(modem_path: str, command: str, runner, timeout: float) -> str:
    # MM keeps ownership of the tty and serializes the command with its SIM bridge.
    # Pass both the actual AT timeout and a longer D-Bus/process deadline: mmcli's
    # default Command timeout is too short for a full COPS scan.
    try:
        result = runner([
            "busctl", "--system", "--json=short", f"--timeout={int(timeout) + 15}",
            "call", MM_SERVICE, modem_path, f"{MM_SERVICE}.Modem", "Command", "su",
            command, str(int(timeout)),
        ], capture_output=True, text=True, timeout=timeout + 30, check=False,
            env={**os.environ, "LC_ALL": "C"})
    except subprocess.TimeoutExpired as exc:
        raise CellularNetworkError("Cellular network scan timed out.") from exc
    except OSError as exc:
        raise CellularNetworkError("ModemManager is unavailable.") from exc
    if result.returncode:
        detail = _error(result, "Cellular network scan failed.")
        if detail == "Call failed: Operation not allowed":
            raise CellularNetworkError(
                "The modem rejected the scan. Wait for cellular activity to finish, then try again.")
        raise CellularNetworkError(detail)
    try:
        reply = json.loads(result.stdout)
        data = reply["data"]
        if reply.get("type") != "s" or not isinstance(data, list) \
                or len(data) != 1 or not isinstance(data[0], str):
            raise ValueError("invalid D-Bus command reply")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CellularNetworkError("Cellular network scan returned an invalid response.") from exc
    return data[0]


def _scan_at(modem_path: str, runner, timeout: float) -> list[dict]:
    return parse_cops_output(_at_command(modem_path, "AT+COPS=?", runner, timeout))


def _scan_quectel(modem_path: str, runner, timeout: float, sleeper) -> list[dict]:
    # COPS scans can be rejected while this firmware remains registered, even with
    # no data bearer. Temporarily deregister only when the exact selection can be
    # restored. A non-numeric manual selection is never guessed from its name.
    selection = _at_command(modem_path, "AT+COPS?", runner, 10).strip()
    automatic = re.fullmatch(r'\+COPS:\s*0(?:\s*,[^\r\n]*)?', selection)
    manual = re.fullmatch(r'\+COPS:\s*([14])\s*,\s*2\s*,\s*"([0-9]{5,6})"'
                          r'(?:\s*,\s*[0-9]{1,2})?', selection)
    restore = "AT+COPS=0" if automatic else (
        f'AT+COPS={manual[1]},2,"{manual[2]}"' if manual else "")
    if not restore:
        return _scan_at(modem_path, runner, timeout)
    try:
        _at_command(modem_path, "AT+COPS=2", runner, 60)
        sleeper(3)
        return _scan_at(modem_path, runner, timeout)
    finally:
        # Even a failed/timed-out deregistration may already have changed the RF
        # state. Never return a successful scan while registration restoration failed.
        try:
            _at_command(modem_path, restore, runner, REGISTER_TIMEOUT_SECONDS)
        except CellularNetworkError as exc:
            raise CellularNetworkError(
                "Restoring cellular registration failed. Re-apply the saved network selection.") from exc


def scan(modem_path: str, runner=subprocess.run,
         timeout: float = SCAN_TIMEOUT_SECONDS, sleeper=time.sleep) -> list[dict]:
    if not MODEM_PATH_RE.fullmatch(str(modem_path or "")):
        raise CellularNetworkError("The cellular modem path is invalid.")
    # Quectel QMI firmware can report a successful but empty NAS scan while its
    # AT scan returns real networks. Prefer the verified AT path for these modems.
    if _prefer_at_scan(modem_path, runner):
        return _scan_quectel(modem_path, runner, timeout, sleeper)
    try:
        result = runner(
            ["mmcli", "-m", modem_path, "--3gpp-scan", f"--timeout={int(timeout)}"],
            capture_output=True, text=True, timeout=timeout + 15, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise CellularNetworkError("Cellular network scan timed out.") from exc
    except OSError as exc:
        raise CellularNetworkError("ModemManager is unavailable.") from exc
    if result.returncode:
        # mmcli renders a successful empty Scan result as this exact error. Only
        # that completed-empty case gets one AT attempt; never overlap a timed-out
        # scan or hide an authorization, SIM, busy or transport failure.
        if str(result.stderr or "").strip() == EMPTY_MMCLI_SCAN:
            return _scan_at(modem_path, runner, timeout)
        raise CellularNetworkError(_error(result, "Cellular network scan failed."))
    return parse_scan_output(result.stdout)


def _selection(mode: str, operator_id: str) -> dict:
    mode = str(mode or "").lower()
    operator_id = str(operator_id or "").strip()
    if mode == "automatic":
        return {"mode": mode, "operator_id": ""}
    if mode == "manual" and re.fullmatch(r"[0-9]{5,6}", operator_id):
        return {"mode": mode, "operator_id": operator_id}
    raise CellularNetworkError("Use automatic mode or provide a 5-6 digit operator MCC/MNC.")


def _registration_error(detail: str) -> str:
    detail = detail.lower()
    if any(word in detail for word in ("networknotallowed", "network not allowed", "registration denied")):
        return "denied"
    if "no network service" in detail or "nonetwork" in detail:
        return "no_service"
    if "timeout" in detail or "timed out" in detail:
        return "network_timeout"
    if any(word in detail for word in ("couldn't find modem", "not available", "serviceunknown")):
        return "unavailable"
    return "failed"


def _request_registration(modem_path: str, selection: dict, runner, timeout: float,
                          *, use_at: bool = False) -> str:
    keep_registration = (use_at and selection["mode"] == "automatic"
                         and _is_registered(_registration_snapshot(modem_path, runner), selection))
    action = ("--3gpp-register-home" if selection["mode"] == "automatic"
              else f"--3gpp-register-in-operator={selection['operator_id']}")
    try:
        result = runner(["mmcli", "-m", modem_path, action, f"--timeout={int(timeout)}"],
                        capture_output=True, text=True, timeout=timeout + 15, check=False,
                        env={**os.environ, "LC_ALL": "C"})
    except subprocess.TimeoutExpired:
        error = "network_timeout"
    except OSError:
        return "unavailable"
    else:
        error = _registration_error(_error(result, "failed")) if result.returncode else ""
    if use_at and error in {"", "network_timeout"}:
        # Synchronize MM's selection intent FIRST. Applying it after COPS can
        # overwrite the freshly selected AT mode using a stale QMI registration.
        # MM remains the only tty owner; no port, band or APN configuration changes.
        command = ("AT+COPS=0" if selection["mode"] == "automatic"
                   else f'AT+COPS=1,2,"{selection["operator_id"]}"')
        try:
            if not keep_registration:
                _at_command(modem_path, "AT+COPS=2", runner, 60)
            _at_command(modem_path, command, runner, max(timeout, 180))
        except CellularNetworkError as exc:
            return _registration_error(str(exc))
        return ""
    return error


def _registration_snapshot(modem_path: str, runner) -> dict:
    try:
        result = runner(["mmcli", "-m", modem_path, "--output-json"],
                        capture_output=True, text=True, timeout=10, check=False,
                        env={**os.environ, "LC_ALL": "C"})
        if result.returncode:
            return {"state": "unavailable", "operator_id": ""}
        cell = json.loads(result.stdout)["modem"]["3gpp"]
        state = str(cell.get("registration-state") or "unknown")
        code = str(cell.get("operator-code") or "")
        return {"state": state if state in {"home", "roaming", "searching", "denied", "idle"} else "unknown",
                "operator_id": code if re.fullmatch(r"[0-9]{5,6}", code) else ""}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, AttributeError):
        return {"state": "unavailable", "operator_id": ""}


def _is_registered(snapshot: dict, selection: dict) -> bool:
    return (snapshot["state"] in {"home", "roaming"} and bool(snapshot["operator_id"])
            and (selection["mode"] == "automatic"
                 or snapshot["operator_id"] == selection["operator_id"]))


def _wait_registration(modem_path: str, selection: dict, runner, sleeper, attempts: int) -> dict:
    snapshot = {}
    confirmed = 0
    for attempt in range(attempts):
        snapshot = _registration_snapshot(modem_path, runner)
        confirmed = confirmed + 1 if _is_registered(snapshot, selection) else 0
        if confirmed >= min(3, attempts) or snapshot["state"] in {"denied", "unavailable"}:
            break
        if attempt + 1 < attempts:
            sleeper(3)
    if _is_registered(snapshot, selection) and confirmed < min(3, attempts):
        return {**snapshot, "state": "registering"}
    return snapshot


def _selection_is_applied(modem_path: str, selection: dict, runner) -> bool:
    """A registration timeout can still restore selection; prove that separately."""
    if not _prefer_at_scan(modem_path, runner):
        return False
    try:
        value = _at_command(modem_path, "AT+COPS?", runner, 10).strip()
    except CellularNetworkError:
        return False
    if selection["mode"] == "automatic":
        return bool(re.fullmatch(r'\+COPS:\s*0(?:\s*,[^\r\n]*)?', value))
    match = re.fullmatch(r'\+COPS:\s*1\s*,\s*2\s*,\s*"([0-9]{5,6})"'
                         r'(?:\s*,\s*[0-9]{1,2})?', value)
    return bool(match and match[1] == selection["operator_id"])


def register(modem_path: str, *, mode: str, operator_id: str = "", previous: dict | None = None,
             runner=subprocess.run, timeout: float = REGISTER_TIMEOUT_SECONDS,
             sleeper=time.sleep, settle_attempts: int = 41) -> dict:
    """Confirm the requested PLMN, restoring saved selection after a failed attempt.

    MM's Register method has its own 60-second registration check even when the
    CLI timeout is longer. Its NetworkTimeout does not cancel modem selection;
    allow late registration before recovery, without starting a competing command.
    """
    if not MODEM_PATH_RE.fullmatch(str(modem_path or "")):
        raise CellularNetworkError("The cellular modem path is invalid.")
    selected = _selection(mode, operator_id)
    previous = previous or {"mode": "automatic", "operator_id": ""}
    restore = _selection(previous.get("mode", "automatic"), previous.get("operator_id", ""))
    use_at = _prefer_at_scan(modem_path, runner)
    error = _request_registration(modem_path, selected, runner, timeout, use_at=use_at)
    snapshot = _wait_registration(modem_path, selected, runner, sleeper,
                                  settle_attempts if error in {"", "network_timeout"} else 1)
    if (error in {"", "network_timeout"} and _is_registered(snapshot, selected)
            and (not use_at or _selection_is_applied(modem_path, selected, runner))):
        return {**selected, "registration": snapshot}
    if snapshot["state"] == "denied":
        error = "denied"
    error = error or "not_registered"
    recovery_error = _request_registration(modem_path, restore, runner, timeout, use_at=use_at)
    recovered = _wait_registration(modem_path, restore, runner, sleeper,
                                   settle_attempts if recovery_error in {"", "network_timeout"} else 1)
    restored_selection = (_selection_is_applied(modem_path, restore, runner) if use_at
                          else not recovery_error or (recovery_error == "network_timeout"
                               and _selection_is_applied(modem_path, restore, runner)))
    recovery_state = ("restored" if restored_selection and recovery_error in {"", "network_timeout"} and _is_registered(recovered, restore)
                      else "pending" if restored_selection and recovered["state"] in {"searching", "idle", "registering"}
                      else "failed")
    raise CellularRegistrationError(error, {"state": recovery_state, **restore, "registration": recovered})
