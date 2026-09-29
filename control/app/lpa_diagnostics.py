"""Closed diagnostics from lpac 2.3.x results, never raw APDUs or private metadata.

The lpac envelope's -1 is a helper failure, not an ES10c card result. Its profile
applets translate the latter into fixed reason strings; only those exact strings
can establish a card result. Internal/transport failures leave it unconfirmed.
"""
from __future__ import annotations

import re


OPERATIONS = {
    "chip info", "profile list", "profile enable", "profile disable",
    "profile delete", "profile nickname", "profile download",
    "notification list", "notification process", "notification remove",
}
STEPS = {
    "euicc_init", "es10c_enable_profile", "es10c_disable_profile",
    "es10c_delete_profile", "es10c_set_nickname", "es10c_get_profiles_info",
    "es9p_authenticate_client", "es9p_initiate_authentication",
    "es10b_authenticate_server", "es10b_prepare_download",
    "es9p_get_bound_profile_package", "es10b_load_bound_profile_package",
    "es10b_list_notification", "es10b_retrieve_notifications_list",
    "es9p_handle_notification", "es10b_remove_notification_from_list",
}
PROFILE_STEPS = {"es10c_enable_profile", "es10c_disable_profile", "es10c_delete_profile"}
# Fixed strings and result values in the pinned lpac profile applets.
PROFILE_REASONS = {
    "iccid or aid not found": ("profile_not_found", 1),
    "profile not in disabled state": ("profile_not_disabled", 2),
    "profile not in enabled state": ("profile_not_enabled", 2),
    "disallowed by policy": ("policy_disallowed", 3),
    "wrong profile reenabling": ("wrong_profile_reenabling", 4),
    "internal error, maybe illegal iccid/aid coding": ("internal_error", None),
}
REASON_MESSAGES = {
    "profile_not_found": "The card did not find the requested profile. Refresh the profile list.",
    "profile_not_disabled": "The card requires this profile to be disabled before this operation.",
    "profile_not_enabled": "The card reports that this profile is not enabled.",
    "policy_disallowed": "The card's profile policy does not allow this operation.",
    "wrong_profile_reenabling": "The card rejected re-enabling this profile.",
    "internal_error": "The eSIM helper could not encode, exchange or parse the card command. No card result was confirmed.",
}
PCSC_CODES = {
    "SCARD_E_SHARING_VIOLATION": "8010000B",
    "SCARD_E_NO_SMARTCARD": "8010000C",
    "SCARD_E_NOT_TRANSACTED": "80100016",
    "SCARD_E_READER_UNAVAILABLE": "80100017",
    "SCARD_W_UNRESPONSIVE_CARD": "80100066",
    "SCARD_W_RESET_CARD": "80100068",
    "SCARD_W_REMOVED_CARD": "80100069",
}
CATEGORIES = {
    "reader_busy", "reader_unavailable", "card_unavailable", "notification_timeout",
    "interrupted", "remote_rejected", "network_transport", "process_error", "unknown_error",
}


def diagnostic(message: str, detail, *, operation: str, code: int, category: str) -> dict:
    """Only export fixed tokens and explicitly identified short status codes."""
    result = {
        "operation": operation if operation in OPERATIONS else "unknown",
        "step": message if message in STEPS else "unknown",
        "category": category if category in CATEGORIES else "unknown_error",
    }
    if type(code) is int and -65535 <= code <= 65535:
        result["lpac_code"] = code
    # Do not mine arbitrary nested server/config payloads for apparent card results.
    text = detail.strip() if isinstance(detail, str) else ""
    reason, card_result = PROFILE_REASONS.get(text.casefold(), ("", None)) \
        if message in PROFILE_STEPS else ("", None)
    if reason:
        result["reason"] = reason
    if card_result is not None:
        result["card_result"] = card_result
    # stderr can contain identities and APDU bodies. Extract just known PC/SC errors
    # or a status explicitly marked as SW, never an arbitrary suffix of an APDU.
    text = text[:4096].upper()
    for symbol, value in PCSC_CODES.items():
        if symbol in text or re.search(r"(?<![0-9A-F])(?:0X)?(?:FFFFFFFF)?" + value + r"(?![0-9A-F])", text):
            result["pcsc_code"] = value
            break
    match = re.search(r"\b(?:SW|SW1SW2|STATUS[ _-]*WORD)\s*[:=]\s*(?:0X)?([0-9A-F]{4})(?![0-9A-F])", text)
    if match and match[1].startswith(("6", "9")):
        result["status_word"] = match[1]
    return result


FAILURE_STAGES = {
    "identifier_encoding", "request_encoding", "apdu_transport", "apdu_response",
    "command_exchange", "response_status", "response_tag", "response_result",
}

def profile_transport_text(stderr: str) -> str | None:
    """Exclude ignored initialization failures and subsequent channel cleanup."""
    begin = "MDD_LPAC_DIAG begin=profile\n"
    if begin not in stderr:
        return None
    return stderr.split(begin, 1)[1].split("MDD_LPAC_DIAG end=profile", 1)[0][:16384]


def profile_transport_diagnostic(stderr: str) -> dict:
    """Read only fixed tokens inside the pinned patch's profile exchange markers."""
    scope = profile_transport_text(stderr)
    if scope is None:
        return {}
    result = {}
    for line in scope.splitlines():
        match = re.fullmatch(r"MDD_LPAC_DIAG sw=([0-9A-F]{4})", line)
        if match:
            result["status_word"] = match[1]
        match = re.fullmatch(r"MDD_LPAC_DIAG stage=([a-z_]+)(?: sw=([0-9A-F]{4}))?", line)
        if match and match[1] in FAILURE_STAGES:
            result.setdefault("failure_stage", match[1])
            if match[2] and match[1] == "response_status":
                result.setdefault("status_word", match[2])
        match = re.fullmatch(r"MDD_LPAC_DIAG card_result=([0-9]{1,3})", line)
        if match and 0 <= int(match[1]) <= 255:
            result["card_result"] = int(match[1])
        match = re.fullmatch(r"SCardTransmit\(\) failed: (8010[0-9A-F]{4}) \(.*\)", line)
        if match:
            result.setdefault("pcsc_code", match[1])
    return result
