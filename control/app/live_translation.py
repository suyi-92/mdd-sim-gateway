"""OpenAI Realtime Translation credentials for optional in-call subtitles.

The long-lived API key never leaves Control.  An authenticated browser receives only the
short-lived client secret that OpenAI documents for a browser WebRTC translation sidecar.
Audio and transcripts do not pass through or persist on the gateway.
"""
from __future__ import annotations

import re
from typing import Callable

import requests


MODEL = "gpt-realtime-translate"
TARGET_LANGUAGE = "zh"
CLIENT_SECRET_URL = "https://api.openai.com/v1/realtime/translations/client_secrets"
REQUEST_TIMEOUT_SECONDS = 15
_API_KEY = re.compile(r"[!-~]{8,512}\Z")


class LiveTranslationError(RuntimeError):
    """A closed-schema error safe to return to an authenticated WebUI."""

    def __init__(self, code: str, status_code: int = 502):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def normalize_config(value: object, previous: object = None) -> dict:
    """Validate a WebUI settings patch while preserving an already-stored secret.

    GET /api/settings deliberately returns an empty ``api_key`` plus ``api_key_set``.  The
    settings page submits that public shape when another field is saved, so an empty key means
    "keep the existing key" unless ``clear_api_key`` is explicitly true.
    """
    if not isinstance(value, dict):
        raise ValueError("live_translation.invalid_settings")
    old = previous if isinstance(previous, dict) else {}
    unknown = set(value) - {"enabled", "api_key", "api_key_set", "clear_api_key"}
    if unknown:
        raise ValueError("live_translation.invalid_settings")
    if "enabled" in value and not isinstance(value["enabled"], bool):
        raise ValueError("live_translation.invalid_settings")
    if "clear_api_key" in value and not isinstance(value["clear_api_key"], bool):
        raise ValueError("live_translation.invalid_settings")

    supplied = str(value.get("api_key") or "").strip()
    if supplied and not _API_KEY.fullmatch(supplied):
        raise ValueError("live_translation.invalid_api_key")
    if value.get("clear_api_key") is True:
        api_key = ""
    else:
        api_key = supplied or str(old.get("api_key") or "").strip()
    enabled = bool(value.get("enabled", old.get("enabled", False)))
    if enabled and not _API_KEY.fullmatch(api_key):
        raise ValueError("live_translation.not_configured")
    return {"enabled": enabled, "api_key": api_key}


def public_config(settings: object) -> dict:
    """Return the non-secret configuration shape exposed to browsers."""
    value = settings if isinstance(settings, dict) else {}
    api_key = str(value.get("api_key") or "").strip()
    return {
        "enabled": bool(value.get("enabled", False)),
        "api_key": "",
        "api_key_set": bool(_API_KEY.fullmatch(api_key)),
    }


def status(settings: object) -> dict:
    public = public_config(settings)
    return {
        "enabled": public["enabled"],
        "configured": public["api_key_set"],
        "model": MODEL,
        "target_language": TARGET_LANGUAGE,
    }


def create_client_secret(settings: object, safety_identifier: str,
                         post: Callable = requests.post) -> dict:
    """Create one bounded, short-lived browser credential without exposing provider errors."""
    value = settings if isinstance(settings, dict) else {}
    if not value.get("enabled"):
        raise LiveTranslationError("live_translation.disabled", 409)
    api_key = str(value.get("api_key") or "").strip()
    if not _API_KEY.fullmatch(api_key):
        raise LiveTranslationError("live_translation.not_configured", 409)

    try:
        response = post(
            CLIENT_SECRET_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "OpenAI-Safety-Identifier": safety_identifier,
            },
            json={
                "session": {
                    "model": MODEL,
                    "audio": {"output": {"language": TARGET_LANGUAGE}},
                },
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise LiveTranslationError("live_translation.provider_unavailable") from exc

    if response.status_code in {401, 403}:
        raise LiveTranslationError("live_translation.api_key_rejected", 502)
    if response.status_code == 429:
        raise LiveTranslationError("live_translation.rate_limited", 503)
    if not 200 <= response.status_code < 300:
        raise LiveTranslationError("live_translation.provider_unavailable")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise LiveTranslationError("live_translation.invalid_response") from exc
    secret = str(payload.get("value") or "") if isinstance(payload, dict) else ""
    if not secret or not _API_KEY.fullmatch(secret):
        raise LiveTranslationError("live_translation.invalid_response")

    result = {"value": secret, "model": MODEL, "target_language": TARGET_LANGUAGE}
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        result["expires_at"] = expires_at
    return result
