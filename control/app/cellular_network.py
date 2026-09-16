"""Bounded ModemManager 3GPP network scan and registration helpers."""
from __future__ import annotations

import os
import re
import subprocess


MODEM_PATH_RE = re.compile(r"^/org/freedesktop/ModemManager1/Modem/\d+$")
OPERATOR_ID_RE = re.compile(r"^\d{5,6}$")
NETWORK_LINE_RE = re.compile(
    r"^(?:\s*3GPP scan\s*\|\s*networks:|\s*\|)\s*"
    r"(?P<code>\d{5,6})\s+-\s*(?P<name>.*?)\s+\((?P<detail>[^()]*)\)\s*$")
SCAN_TIMEOUT_SECONDS = 300
REGISTER_TIMEOUT_SECONDS = 120
NETWORK_STATUSES = {"available", "current", "forbidden", "unknown"}


class CellularNetworkError(RuntimeError):
    pass


def _error(result, fallback: str) -> str:
    detail = " ".join(str(getattr(result, "stderr", "") or "").split())
    return detail[:300] if detail else fallback


def parse_scan_output(value: str) -> list[dict]:
    """Parse stable C-locale mmcli scan rows without accepting arbitrary identifiers."""
    networks: dict[str, dict] = {}
    for raw in str(value or "").splitlines():
        match = NETWORK_LINE_RE.fullmatch(raw)
        if not match:
            continue
        code = match.group("code")
        detail = match.group("detail").rsplit(",", 1)
        technology = re.sub(r"[^A-Za-z0-9_+./ -]", "", detail[0]).strip().lower()
        status = detail[1].strip().lower() if len(detail) == 2 else "unknown"
        if status not in NETWORK_STATUSES:
            status = "unknown"
        name = " ".join(match.group("name").split())[:100]
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


def scan(modem_path: str, runner=subprocess.run,
         timeout: float = SCAN_TIMEOUT_SECONDS) -> list[dict]:
    if not MODEM_PATH_RE.fullmatch(str(modem_path or "")):
        raise CellularNetworkError("The cellular modem path is invalid.")
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
        raise CellularNetworkError(_error(result, "Cellular network scan failed."))
    return parse_scan_output(result.stdout)


def register(modem_path: str, *, mode: str, operator_id: str = "",
             runner=subprocess.run, timeout: float = REGISTER_TIMEOUT_SECONDS) -> dict:
    if not MODEM_PATH_RE.fullmatch(str(modem_path or "")):
        raise CellularNetworkError("The cellular modem path is invalid.")
    mode = str(mode or "").lower()
    operator_id = str(operator_id or "").strip()
    if mode == "automatic":
        args = ["mmcli", "-m", modem_path, "--3gpp-register-home",
                f"--timeout={int(timeout)}"]
        operator_id = ""
    elif mode == "manual" and OPERATOR_ID_RE.fullmatch(operator_id):
        args = ["mmcli", "-m", modem_path,
                f"--3gpp-register-in-operator={operator_id}",
                f"--timeout={int(timeout)}"]
    else:
        raise CellularNetworkError(
            "Use automatic mode or provide a 5-6 digit operator MCC/MNC.")
    try:
        result = runner(args, capture_output=True, text=True,
                        timeout=timeout + 15, check=False,
                        env={**os.environ, "LC_ALL": "C"})
    except subprocess.TimeoutExpired as exc:
        raise CellularNetworkError("Cellular network registration timed out.") from exc
    except OSError as exc:
        raise CellularNetworkError("ModemManager is unavailable.") from exc
    if result.returncode:
        raise CellularNetworkError(_error(result, "Cellular network registration failed."))
    return {"mode": mode, "operator_id": operator_id}
