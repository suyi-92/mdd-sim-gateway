#!/usr/bin/env python3
"""Expose modem AT+CSIM as three independent VPCD slots.

Each VPCD slot is mapped to one ISO 7816 logical channel.  The modem has one
physical AT port, so all AT commands are serialized while the selected-file
state remains isolated by the UICC logical channels.  A card that grants fewer
channels than slots has slots share a channel (see ChannelArbiter).
"""

import argparse
from contextlib import contextmanager, nullcontext
import errno
import json
import math
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

try:
    import serial
except ImportError:  # ModemManager-backed mode does not need pyserial
    serial = None


DEFAULT_ATR = bytes.fromhex("3B9F95801FC78031E073FE211B66D0017797020C000B")
CSIM_RE = re.compile(rb'\+CSIM:\s*(\d+)\s*,\s*"([0-9A-Fa-f]*)"')
IMEI_RE = re.compile(rb'(?<!\d)(\d{15})(?!\d)')
ICCID_RE = re.compile(rb'(?<!\d)(89\d{17,20})(?!\d)')
LOGICAL_CHANNEL_CAPACITY = 3
LOGICAL_CHANNEL_ROLES = ("pin", "swu", "ims")
LOGICAL_CHANNEL_MAX = 19
# A slot pcscd never opens a socket for is a normal steady state, not an incident: the reader
# may expose fewer slots than the modem offers. Retrying every second and logging every attempt
# turned that into a permanent write stream — one line per slot per second, forever, on hosts
# whose storage is an SD card. Retries back off to a minute and only the first failure of an
# outage is logged, so a genuinely broken slot is still visible without the repetition.
SLOT_RETRY_SECONDS = 1.0
SLOT_RETRY_CEILING_SECONDS = 60.0
# MANAGE CHANNEL OPEN answering with a channel this bridge already holds is not proof that the
# UICC is out of channels. On virtualized USB passthrough a late reply can be read as the answer
# to the next command, so the previous OPEN's channel number comes back verbatim. Settle the port
# and ask again before declaring the SIM unusable — the alternative is a bridge that exits and
# takes both lines down over a transport artefact.
CHANNEL_OPEN_ATTEMPTS = 3
CHANNEL_SETTLE_SECONDS = 0.5
# One missing refresh can be ordinary AT/APDU contention. Repeated loss after this bridge
# already proved a card is evidence that its physical SIM generation changed or became
# unusable; the current logical channels must then be retired instead of serving stale state.
IDENTITY_LOSS_LIMIT = 3
# Some UICCs expose a single supplementary logical channel (a China Unicom USIM answered the
# second MANAGE CHANNEL OPEN with 6A81). Slots then share a channel, which is only safe if a
# slot finds the card where it left it: each slot's SELECTs are replayed when the channel
# changes hands, and a slot owed a GET RESPONSE keeps the channel until it asks for it.
PENDING_RESPONSE_HOLD_SECONDS = 2.0
MAX_SELECT_HISTORY = 16
SELECT_MF = bytes.fromhex("00A40004023F00")
SIM_ACCESS_FAILURES = 3
SIM_ACCESS_FAILURE_SECONDS = 30.0
SIM_ACCESS_RECOVERY_EXIT = 75
SIM_ACCESS_CLEANUP_EXIT = 76
BRIDGE_RECOVERY_WINDOW = 3600.0
BRIDGE_RECOVERY_ATTEMPTS = 3
BRIDGE_WORKER_STOP_SECONDS = 15.0


class SIMAccessHealth:
    """Retire a repeatedly unusable owned channel without replaying its APDUs.

    Success on a sibling or the basic channel cannot clear this channel's failures. Card
    verdicts such as PIN/authentication errors remain ordinary responses, not transport
    failures. Production callers supply the exact channels allocated by this process.
    """

    def __init__(self, channels=range(1, LOGICAL_CHANNEL_MAX + 1)):
        self.channels = frozenset(channels)
        self.lock = threading.Lock()
        self.failed = threading.Event()
        self.streaks = {}
        self.local = threading.local()

    @contextmanager
    def preserve_streak(self):
        """Internal SELECT restoration is not a successful client transaction."""
        previous = getattr(self.local, "preserve", False)
        self.local.preserve = True
        try:
            yield
        finally:
            self.local.preserve = previous

    def observe(self, channel, failed):
        if channel not in self.channels or channel not in range(1, LOGICAL_CHANNEL_MAX + 1):
            return
        with self.lock:
            if not failed:
                if not getattr(self.local, "preserve", False):
                    self.streaks.pop(channel, None)
                return
            now = time.monotonic()
            count, since = self.streaks.get(channel, (0, now))
            self.streaks[channel] = (count + 1, since)
            if count + 1 >= SIM_ACCESS_FAILURES and now - since >= SIM_ACCESS_FAILURE_SECONDS:
                self.failed.set()


class BridgeRecoveryBudget:
    """Persist at most three replacement attempts per modem in one hourly window.

    The first watchdog exit opens an incident. Before spawning any replacement the caller
    reserves its attempt durably, including failed Popen calls and a supervisor crash before
    observing the child's exit. Live processes never clear the hourly window.
    """

    def __init__(self, path):
        self.path = str(path)
        self.records = {}
        self.load_error = ""
        try:
            with open(self.path, encoding="utf-8") as handle:
                records = json.load(handle)
            if not isinstance(records, dict) or any(
                    not isinstance(key, str) or not self._valid_record(value)
                    for key, value in records.items()):
                raise ValueError("invalid recovery budget")
            self.records = records
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self.load_error = (
                "SIM access recovery budget could not be read; automatic bridge rebuild is blocked")

    @staticmethod
    def _valid_record(value):
        return (isinstance(value, dict)
                and all(isinstance(value.get(field), (int, float))
                        and not isinstance(value.get(field), bool)
                        and math.isfinite(value[field]) and value[field] >= 0
                        for field in ("since", "at", "retry_at"))
                and type(value.get("failures")) is int and value["failures"] >= 1
                and type(value.get("attempts")) is int
                and 0 <= value["attempts"] <= BRIDGE_RECOVERY_ATTEMPTS
                and type(value.get("limited")) is bool)

    def record_exit(self, device_id, returncode, uptime=0.0):
        if self.load_error:
            return
        if returncode == 0:
            # Identity retirement and an orderly stop are not transport failures. Existing
            # pre-spawn reservations remain in force for this physical modem's window.
            return
        now = time.time()
        previous = self.records.get(device_id, {})
        active = bool(previous) and now < previous["since"] + BRIDGE_RECOVERY_WINDOW
        if returncode != SIM_ACCESS_RECOVERY_EXIT and not active:
            return
        count = previous["failures"] + 1 if active else 1
        attempts = previous["attempts"] if active else 0
        since = previous["since"] if active else now
        limited = attempts >= BRIDGE_RECOVERY_ATTEMPTS
        retry_at = (since + BRIDGE_RECOVERY_WINDOW if limited
                    else now + 15 * 2 ** attempts)
        self.records[device_id] = {"since": since, "at": now, "failures": count,
                                   "attempts": attempts, "retry_at": retry_at, "limited": limited}
        write_metadata(self.path, self.records)

    def reserve_attempt(self, device_id):
        """Durably consume an active incident's replacement slot before starting a child.

        A failed state write raises, so the caller must not spawn. Ordinary startup remains
        outside this watchdog budget until the first runtime SIM-access failure.
        """
        if self.load_error:
            return False
        now = time.time()
        previous = self.records.get(device_id, {})
        if not previous or now >= previous["since"] + BRIDGE_RECOVERY_WINDOW:
            return True
        if now < previous["retry_at"] or previous["attempts"] >= BRIDGE_RECOVERY_ATTEMPTS:
            return False
        attempts = previous["attempts"] + 1
        limited = attempts >= BRIDGE_RECOVERY_ATTEMPTS
        record = {**previous, "at": now, "attempts": attempts, "limited": limited,
                  "retry_at": (previous["since"] + BRIDGE_RECOVERY_WINDOW if limited
                               else now + 15 * 2 ** attempts)}
        records = {**self.records, device_id: record}
        write_metadata(self.path, records)
        self.records = records
        return True

    def snapshot(self):
        if self.load_error:
            return {"budget_error": {"reason": self.load_error, "limited": True}}
        now = time.time()
        return {device_id: {"count": value["failures"],
                            "attempts": value["attempts"],
                            "at": value.get("at", value["since"]),
                            "reason": self.error(device_id) or "SIM access recovery attempted",
                            "retry_at": value["retry_at"],
                            "limited": bool(value.get("limited"))}
                for device_id, value in self.records.items()
                if now < value["since"] + BRIDGE_RECOVERY_WINDOW}

    def retry_at(self, device_id):
        if self.load_error:
            return float("inf")
        value = self.records.get(device_id, {})
        if not value or time.time() >= value["since"] + BRIDGE_RECOVERY_WINDOW:
            return 0
        return value["retry_at"]

    def error(self, device_id):
        if self.load_error:
            return self.load_error
        value = self.records.get(device_id, {})
        if time.time() >= self.retry_at(device_id):
            return ""
        if value.get("limited"):
            return ("SIM access recovery paused after three rebuild attempts; "
                    "automatic retry at %d" % value["retry_at"])
        return "SIM access failed; waiting to rebuild this modem's bridge"


class ModemError(RuntimeError):
    pass


class BridgeStopping(ModemError):
    """A queued worker may not begin another AT exchange after shutdown starts."""


class ChannelCapacityError(ModemError):
    """Only a verified 6A81 MANAGE CHANNEL reply permits channel sharing."""


def decode_bcd_iccid(data):
    """Decode EF_ICCID swapped-nibble BCD; empty string when the bytes are not an ICCID."""
    digits = []
    for byte in data:
        for nibble in (byte & 0x0F, byte >> 4):
            if nibble == 0x0F:
                continue
            if nibble > 9:
                return ""
            digits.append(str(nibble))
    value = "".join(digits)
    return value if value.startswith("89") and 18 <= len(value) <= 20 else ""


def logical_channel_metadata(channels, requested=3, status="ready", error="",
                             slot_channels=None):
    """Return stable, UI-safe resource metadata for one physical UICC.

    `slot_channels` maps every served slot to its channel; it repeats a channel when the
    card had fewer channels than slots and the slots share one.
    """
    values = [int(channel) for channel in channels]
    served = [int(channel) for channel in (values if slot_channels is None else slot_channels)]
    return {
        "channel_capacity": LOGICAL_CHANNEL_CAPACITY,
        "channel_requested": int(requested),
        "channel_allocated": len(values),
        "channel_status": str(status),
        "channel_error": str(error),
        "channel_shared": len(set(served)) < len(served),
        "slots_served": len(served),
        "logical_channels": [
            {"slot": slot, "channel": channel, "role": LOGICAL_CHANNEL_ROLES[slot]}
            for slot, channel in enumerate(served)
        ],
    }


def allocate_logical_channels(card, count):
    """Allocate up to `count` unique UICC channels; at least one is required.

    A card that runs out of channels part way is served with what it granted (see
    slot_channel_map) rather than refused outright. Nothing is left open on failure.
    """
    channels = []
    try:
        for _slot in range(int(count)):
            try:
                channel = card.open_channel()
                for attempt in range(2, CHANNEL_OPEN_ATTEMPTS + 1):
                    if channel not in channels:
                        break
                    print("[bridge] MANAGE CHANNEL OPEN returned channel %d again; settling "
                          "the AT port and retrying (attempt %d/%d)" %
                          (channel, attempt, CHANNEL_OPEN_ATTEMPTS), flush=True)
                    card.settle()
                    channel = card.open_channel()
                if channel in channels:
                    raise ModemError("duplicate logical channel allocated: %d" % channel)
            except ChannelCapacityError as exc:
                if not channels:
                    raise
                print("[bridge] SIM granted %d of %d logical channels (%s); slots will share"
                      % (len(channels), int(count), exc), flush=True)
                break
            channels.append(channel)
        return channels
    except Exception as exc:
        for channel in channels:
            card.close_channel(channel)
        raise ModemError(
            "SIM logical channel allocation failed (%d/%d allocated): %s" %
            (len(channels), LOGICAL_CHANNEL_CAPACITY, exc)) from exc


def allocate_logical_channels_with_recovery(card, count):
    """Allocate channels without closing sessions this process cannot prove it owns.

    A graceful predecessor closes its exact channel list.  If a crashed process leaked a
    session, blindly closing channels 1-3 can instead tear down the baseband or a sibling
    client.  Surface the bounded allocation failure to the orchestrator; its scoped UIM
    recovery has physical-device and USB-generation evidence.
    """
    try:
        return allocate_logical_channels(card, count)
    except ModemError as error:
        raise ModemError(
            "SIM logical channel allocation failed without closing unowned channels: %s"
            % error) from error
def slot_channel_map(channels, slots):
    """Assign every slot a channel, sharing when the card granted fewer than `slots`.

    With two channels the IMS slot keeps one to itself: Asterisk abandons an AKA request
    after a fixed timeout, while PIN checks and the SWu handshake are not that sensitive
    and do not run at the same moment as an IMS registration challenge.
    """
    channels = list(channels)
    if len(channels) >= slots:
        return channels[:slots]
    if len(channels) == 1:
        return channels * slots
    # Two channels, three slots: pin and swu share, ims ("ims" is the last role) is alone.
    return [channels[0]] * (slots - 1) + [channels[1]]


def _is_select(apdu):
    return len(apdu) >= 4 and apdu[1] == 0xA4


def _is_absolute_select(apdu):
    """True when a SELECT fixes the whole file context by itself, independent of history."""
    p1 = apdu[2]
    if p1 in (0x04, 0x08):  # by DF name (AID), by path from MF
        return True
    if p1 == 0x00:
        data = apdu[5:5 + apdu[4]] if len(apdu) > 5 else b""
        return data in (b"", b"\x3F\x00")
    return False


def _select_succeeded(response):
    return len(response) >= 2 and (response[-2:] == b"\x90\x00" or
                                   response[-2] in (0x91, 0x61, 0x9F, 0x62, 0x63))


class ChannelArbiter:
    """Serialize the slots mapped to one UICC logical channel.

    A slot that is alone on its channel pays nothing: the owner never changes, so no SELECT
    is replayed. The selected-file history is still kept, so the same object serves both
    the one-channel-per-slot and the shared layouts.
    """

    def __init__(self, card, channel):
        self.card = card
        self.channel = channel
        self.cond = threading.Condition()
        self.owner = None
        self.pending = None
        self.pending_deadline = 0.0
        self.selects = {}
        self.overflowed = set()

    def _restore(self, slot):
        health = getattr(self.card, "access_health", None)
        with health.preserve_streak() if isinstance(health, SIMAccessHealth) else nullcontext():
            self._restore_selected_files(slot)

    def _restore_selected_files(self, slot):
        if slot in self.overflowed:
            raise ModemError("shared channel SELECT history cannot be restored")
        history = self.selects.get(slot) or [SELECT_MF]
        for apdu in history:
            stopping = getattr(self.card, "stopping", None)
            if stopping is not None and stopping.is_set():
                raise BridgeStopping("bridge is stopping")
            command = apdu
            response = self.card.transmit(command, self.channel)
            for _ in range(8):
                if len(response) < 2:
                    raise ModemError("short shared channel SELECT response")
                sw1, sw2 = response[-2:]
                if sw1 in (0x61, 0x9F):
                    command = bytes((0, 0xC0, 0, 0, sw2))
                    response = self.card.transmit(command, self.channel)
                elif sw1 == 0x6C:
                    if len(command) == 5 or len(command) == 6 + command[4]:
                        command = command[:-1] + bytes((sw2,))
                    elif command[4] and len(command) == 5 + command[4]:
                        command += bytes((sw2,))
                    else:
                        raise ModemError("unsupported shared SELECT length correction")
                    response = self.card.transmit(command, self.channel)
                else:
                    break
            if not _select_succeeded(response) or response[-2] in (0x61, 0x9F):
                raise ModemError("shared channel SELECT restore failed")

    def _claim(self, slot):
        """Wait out another slot's owed GET RESPONSE, then make `slot` the owner."""
        while self.pending not in (None, slot):
            remaining = self.pending_deadline - time.monotonic()
            if remaining <= 0:
                self.overflowed.add(self.pending)
                self.pending = None
                break
            self.cond.wait(remaining)
        if slot in self.overflowed:
            raise ModemError("shared channel state expired or overflowed")
        if self.owner is not None and self.owner != slot:
            self._restore(slot)
        self.owner = slot

    def _record(self, slot, apdu, response):
        if not _is_select(apdu) or not _select_succeeded(response):
            return
        if _is_absolute_select(apdu):
            self.selects[slot] = [apdu]
            self.overflowed.discard(slot)
            return
        history = self.selects.setdefault(slot, [])
        history.append(apdu)
        if len(history) > MAX_SELECT_HISTORY:
            self.overflowed.add(slot)
            raise ModemError("shared channel SELECT history exceeded safe limit")

    def transmit(self, slot, apdu):
        with self.cond:
            self._claim(slot)
            response = self.card.transmit(apdu, self.channel)
            if self.card.closes_logical_channel(apdu, self.channel):
                # ModemCard.transmit put the channel back on MF for the next client.
                self.selects[slot] = [SELECT_MF]
            else:
                self._record(slot, apdu, response)
            if len(response) >= 2 and response[-2] in (0x61, 0x9F, 0x6C):
                self.pending = slot
                self.pending_deadline = time.monotonic() + PENDING_RESPONSE_HOLD_SECONDS
            elif self.pending == slot:
                self.pending = None
            self.cond.notify_all()
            return response

    def reset(self, slot):
        with self.cond:
            self._claim(slot)
            if self.pending == slot:
                self.pending = None
            self.selects[slot] = [SELECT_MF]
            try:
                self.card.reset_channel(self.channel)
            finally:
                self.cond.notify_all()


class ATSerial(serial.Serial if serial else object):
    """A Serial that tolerates absent modem control lines.

    pyserial raises DTR and RTS as part of open() and offers no way to skip that
    (pyserial#729 — setting .dtr first only swaps which ioctl runs). On virtualised USB
    passthrough the underlying control transfer can fail with EPROTO, and a port with no
    modem-control support at all answers ENOTTY. An AT command channel uses neither line,
    so either failure must not cost the port. Verified on the PVE guest that hit it:
    with these overrides the same port opens and the bridge proceeds.
    """

    _TOLERATED = (errno.EPROTO, errno.ENOTTY)

    def _update_dtr_state(self):
        try:
            super()._update_dtr_state()
        except OSError as exc:
            if exc.errno not in self._TOLERATED:
                raise

    def _update_rts_state(self):
        try:
            super()._update_rts_state()
        except OSError as exc:
            if exc.errno not in self._TOLERATED:
                raise


class ModemCard:
    def __init__(self, port, baud=115200, timeout=10.0, debug=False):
        if serial is None:
            raise ModemError("pyserial is required for direct modem access")
        self.lock = threading.RLock()
        self.debug = debug
        self.timeout = timeout
        self.ser = ATSerial(
            port, baud, timeout=0.25, write_timeout=2, exclusive=True
        )
        try:
            self._drain()
            self._at("ATE0")
            self._at("AT+CMEE=2")
        except Exception:
            # A port that is not an AT port (the modem probe tries several) must not stay
            # locked until garbage collection gets to it.
            self.close()
            raise

    def _drain(self):
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if not self.ser.read(4096):
                break

    def settle(self):
        """Discard whatever the modem is still sending, so the next reply is the next reply.

        Subclasses that do not own a serial port inherit the pause without the read.
        """
        time.sleep(CHANNEL_SETTLE_SECONDS)
        if getattr(self, "ser", None) is not None:
            with self.lock:
                self._drain()

    def _at(self, command):
        with self.lock:
            stopping = getattr(self, "stopping", None)
            if stopping is not None and stopping.is_set():
                raise BridgeStopping("bridge is stopping")
            self.ser.reset_input_buffer()
            self.ser.write((command + "\r").encode("ascii"))
            buf = bytearray()
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                chunk = self.ser.read(1024)
                if chunk:
                    buf.extend(chunk)
                    lines = bytes(buf).replace(b"\r", b"\n").splitlines()
                    if any(line.strip() == b"OK" for line in lines):
                        return bytes(buf)
                    if any(
                        line.strip() == b"ERROR"
                        or line.strip().startswith(b"+CME ERROR:")
                        for line in lines
                    ):
                        raise ModemError(bytes(buf).decode("ascii", "replace"))
            raise ModemError("timeout waiting for %s: %r" % (command, bytes(buf[-200:])))

    def csim(self, apdu):
        hex_apdu = apdu.hex().upper()
        if self.debug:
            print("[bridge] APDU --> header=%s bytes=%d" %
                  (apdu[:4].hex().upper(), len(apdu)), flush=True)
        health = getattr(self, "access_health", None)
        # Further-interindustry CLA coding uses the low nibble for channels 4-19. The
        # first coding's low two bits only describe channels 0-3. Observe only channels
        # this process actually owns; basic-channel identity probes are independent.
        channel = ((apdu[0] & 0x0F) + 4 if apdu[0] & 0x40 else apdu[0] & 3) if apdu else 0
        try:
            raw = self._at('AT+CSIM=%d,"%s"' % (len(hex_apdu), hex_apdu))
            match = CSIM_RE.search(raw)
            if not match:
                raise ModemError("missing +CSIM response")
            try:
                response = bytes.fromhex(match.group(2).decode("ascii"))
            except ValueError as exc:
                raise ModemError("malformed +CSIM response") from exc
            if len(response) < 2 or int(match.group(1)) != len(response) * 2:
                raise ModemError("truncated +CSIM response")
        except BridgeStopping:
            raise
        except (ModemError, OSError) as exc:
            if health:
                health.observe(channel, True)
            if isinstance(exc, ModemError):
                raise
            raise ModemError("SIM transport failed") from exc
        if health and response[-2] not in (0x61, 0x9F, 0x6C):
            health.observe(channel, response[-2:] == bytes.fromhex("6881"))
        if self.debug:
            status = response[-2:].hex().upper() if len(response) >= 2 else "unknown"
            print("[bridge] APDU <-- bytes=%d sw=%s" %
                  (max(0, len(response) - 2), status), flush=True)
        return response

    def _iccid_from_card(self):
        """Read EF_ICCID over AT+CSIM — the card's own answer on the basic channel.

        AT+CCID reports the baseband's cached SIM view, and an eSIM profile switch happens
        over AT+CSIM behind the baseband's back: its REFRESH goes unserved in passthrough,
        so the cache keeps the previous profile's ICCID indefinitely. The orchestrator
        verifies a post-switch bridge rebuild against this identity, so publishing the
        cache made every switch look stuck until the 45 s timeout (issue #26). Selecting
        MF afterwards restores the basic-channel file state the baseband expects.
        """
        try:
            for select in ("00A4080C022FE2", "00A40804022FE2"):
                response = self.csim(bytes.fromhex(select))
                if len(response) >= 2 and response[-2] in (0x90, 0x61):
                    break
            else:
                return ""
            response = self.csim(bytes.fromhex("00B000000A"))
            if len(response) != 12 or response[-2:] != b"\x90\x00":
                return ""
            return decode_bcd_iccid(response[:10])
        except ModemError:
            return ""
        finally:
            try:
                self.select_mf(0)
            except ModemError:
                pass

    def identity(self):
        """Read hardware IMEI and the currently inserted SIM ICCID while we own the AT port.

        The bridge is the only safe place to do this: the serial port is opened exclusively for
        its whole lifetime, so the control plane must consume published metadata instead of
        racing another AT client against APDU traffic.
        """
        imei = ""
        for command in ("AT+CGSN", "AT+GSN"):
            try:
                match = IMEI_RE.search(self._at(command))
                if match:
                    imei = match.group(1).decode("ascii")
                    break
            except ModemError:
                pass
        iccid = self._iccid_from_card()
        verified = bool(iccid)
        if not iccid:
            iccid = self.cached_iccid()
        return {"imei": imei, "iccid": iccid, "iccid_verified": verified,
                "iccid_source": "card" if verified else ("baseband_cache" if iccid else "unknown")}

    def cached_iccid(self):
        for command in ("AT+CCID", "AT+ICCID"):
            try:
                match = ICCID_RE.search(self._at(command))
                if match:
                    return match.group(1).decode("ascii")
            except ModemError:
                pass
        return ""

    def refresh_identity(self, previous):
        """Avoid periodic basic-channel APDUs while the baseband cache is unchanged.

        A newly spawned bridge still performs one direct-card proof.  Later refreshes first
        consult the baseband cache; a physical replacement changes that cache and triggers a
        fresh proof, while an eSIM switch explicitly restarts the bridge.  This keeps periodic
        metadata refreshes from selecting MF on the baseband's basic channel every minute.
        """
        previous = dict(previous or {})
        cached = self.cached_iccid()
        old = str(previous.get("iccid") or "")
        if cached and cached == old and previous.get("iccid_verified") is True:
            return previous
        direct = self._iccid_from_card()
        iccid = direct or cached
        return {**previous, "iccid": iccid, "iccid_verified": bool(direct),
                "iccid_source": "card" if direct else (
                    "baseband_cache" if iccid else "unknown")}

    def close_channel(self, channel):
        try:
            # EC25's AT+CSIM rejects the four-byte Case-1 form as a bare AT ERROR. Include
            # Le=00 so MANAGE CHANNEL CLOSE is accepted and an interrupted bridge does not
            # leak channels until the modem is reset.
            return self.csim(bytes((0x00, 0x70, 0x80, channel, 0x00))) == bytes.fromhex("9000")
        except (ModemError, OSError):
            return False

    def open_channel(self):
        response = self.csim(bytes.fromhex("0070000001"))
        if response == bytes.fromhex("6A81"):
            raise ChannelCapacityError("MANAGE CHANNEL capacity exhausted (6A81)")
        if len(response) != 3 or response[-2:] != b"\x90\x00":
            raise ModemError("MANAGE CHANNEL OPEN failed: %s" % response.hex())
        channel = response[0]
        if channel not in range(1, LOGICAL_CHANNEL_MAX + 1):
            # This OPEN succeeded, so this process owns the returned channel even though the
            # UICC returned a value outside the ISO/ETSI logical-channel range. Release exactly
            # that channel before failing; never guess at a different client's channel.
            self.close_channel(channel)
            raise ModemError("unsupported logical channel allocated: %d" % channel)
        return channel

    @staticmethod
    def channel_cla(cla, channel):
        """Encode one standard or extended UICC logical channel in the CLA byte.

        ETSI TS 102 221 uses the first interindustry coding for channels 0-3 and the
        further-interindustry coding for channels 4-19.  The bridge exposes three PC/SC
        slots, but their UICC channel numbers need not be 1, 2 and 3: a baseband client may
        legitimately occupy one of those numbers while MANAGE CHANNEL gives this process 4+.
        """
        if channel in range(0, 4):
            return (int(cla) & 0x9C) | channel
        if channel in range(4, LOGICAL_CHANNEL_MAX + 1):
            return (int(cla) & 0xB0) | 0x40 | (channel - 4)
        raise ModemError("cannot encode unsupported logical channel: %d" % channel)

    @staticmethod
    def on_channel(apdu, channel):
        if len(apdu) < 2:
            return None, bytes.fromhex("6700")
        if apdu[0] == 0xFF:
            return None, bytes.fromhex("6D00")
        if apdu[1] == 0x70:
            # MANAGE CHANNEL. Each VPCD slot already owns one physical UICC logical channel,
            # opened when the bridge starts. PC/SC eUICC clients such as lpac still perform
            # the standard handshake, but forwarding it would allocate a different channel
            # or close the bridge-owned one behind its back. Emulate only the exact OPEN and
            # CLOSE forms lpac uses: OPEN reports the channel this slot already holds, while
            # transmit() recycles that same physical channel before acknowledging CLOSE.
            # Reject every malformed/unsupported variant
            # with Incorrect parameters (6A86) so no ambiguous command reaches the modem.
            if apdu == bytes.fromhex("0070000001"):
                return None, bytes((channel, 0x90, 0x00))
            if apdu == bytes((0x00, 0x70, 0x80, channel, 0x00)):
                return None, bytes.fromhex("9000")
            return None, bytes.fromhex("6A86")
        # 0xA0 is the legacy GSM class and has no logical-channel encoding.
        if apdu[0] == 0xA0:
            return None, bytes.fromhex("6881")
        rewritten = bytes((ModemCard.channel_cla(apdu[0], channel),)) + apdu[1:]
        return rewritten, None

    @staticmethod
    def closes_logical_channel(apdu, channel) -> bool:
        return apdu == bytes((0x00, 0x70, 0x80, channel, 0x00))

    def reset_channel(self, channel):
        """Give this slot a fresh UICC session without touching sibling channels.

        ISD-R can reject ordinary SELECT MF with 6E00 after an LPA session. A virtual
        close/reset must therefore close and reopen the physical channel. Never silently
        rebind the slot to a different channel: its number is also returned to LPA clients.
        """
        if channel not in range(1, LOGICAL_CHANNEL_MAX + 1):
            raise ModemError("cannot reset an unowned logical channel")
        with self.lock:
            response = self.csim(bytes((0x00, 0x70, 0x80, channel, 0x00)))
            if response != bytes.fromhex("9000"):
                raise ModemError("logical channel close failed")
            replacement = self.open_channel()
            if replacement != channel:
                # A stale/duplicate reply may name a sibling. Do not close or steal it.
                raise ModemError("logical channel number changed during reset")

    def transmit(self, apdu, channel):
        rewritten, local_response = self.on_channel(apdu, channel)
        if local_response is not None:
            if self.closes_logical_channel(apdu, channel):
                self.reset_channel(channel)
            return local_response
        try:
            return self.csim(rewritten)
        except BridgeStopping:
            return bytes.fromhex("6F00")
        except ModemError as exc:
            print("[bridge] slot channel %d modem error: %s" % (channel, exc), flush=True)
            return bytes.fromhex("6F00")

    def select_mf(self, channel):
        apdu, _ = self.on_channel(bytes.fromhex("00A40004023F00"), channel)
        return self.csim(apdu)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class ModemManagerCard(ModemCard):
    """Send AT commands through ModemManager instead of racing its serial port.

    ModemManager remains the sole tty owner and serializes these commands with its QMI/AT
    activity.  This permits the VPCD APDU bridge to coexist experimentally with an active
    cellular bearer. ModemManager must be running with command support enabled (--debug).
    """

    def __init__(self, modem, timeout=10.0, debug=False):
        self.lock = threading.RLock()
        self.debug = debug
        self.timeout = timeout
        self.modem = str(modem)
        # Fail early with a harmless command and a clear error if command passthrough is off.
        self._at("AT")

    def _at(self, command):
        with self.lock:
            stopping = getattr(self, "stopping", None)
            if stopping is not None and stopping.is_set():
                raise BridgeStopping("bridge is stopping")
            try:
                result = subprocess.run(
                    ["mmcli", "-m", self.modem, "--command=" + command],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ModemError("ModemManager command failed: %s" % exc) from exc
            raw = result.stdout + result.stderr
            if result.returncode:
                raise ModemError(raw.decode("utf-8", "replace").strip())
            return raw

    def close(self):
        pass


def recv_exact(sock, size, stopping=None):
    chunks = bytearray()
    while len(chunks) < size:
        try:
            chunk = sock.recv(size - len(chunks))
        except socket.timeout:
            if stopping is not None and stopping.is_set():
                return None
            continue
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


def serve_slot(arbiter, host, port, slot, atr, debug, stopping=None):
    channel = arbiter.channel
    managed_stop = stopping is not None
    stopping = stopping or threading.Event()
    delay = SLOT_RETRY_SECONDS
    reported = ""
    while not stopping.is_set():
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=10)
            # A bounded timeout lets shutdown stop slot traffic before owned channels close.
            sock.settimeout(1.0)
            delay, reported = SLOT_RETRY_SECONDS, ""
            print(
                "[bridge] slot %d connected to %s:%d on channel %d"
                % (slot, host, port, channel),
                flush=True,
            )
            while not stopping.is_set():
                header = recv_exact(sock, 2, stopping)
                if header is None:
                    break
                (length,) = struct.unpack(">H", header)
                if length == 0:
                    continue
                payload = recv_exact(sock, length, stopping)
                if payload is None:
                    break
                if length == 1:
                    control = payload[0]
                    if control == 0x04:
                        sock.sendall(struct.pack(">H", len(atr)) + atr)
                    elif control in (0x01, 0x02):
                        arbiter.reset(slot)
                    if debug:
                        print(
                            "[bridge] slot %d control 0x%02X" % (slot, control),
                            flush=True,
                        )
                    continue
                response = arbiter.transmit(slot, payload)
                sock.sendall(struct.pack(">H", len(response)) + response)
        except ModemError:
            # A failed close/reopen leaves the slot unusable. Exiting this worker makes the
            # bridge supervisor retire the generation instead of publishing a false 9000.
            print("[bridge] slot %d channel reset failed; retiring bridge" % slot, flush=True)
            return
        except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
            # Repeating an unchanged reason says nothing the first line did not, so only a
            # new one is printed. Debug mode keeps every attempt for a live investigation.
            if debug or str(exc) != reported:
                reported = str(exc)
                print("[bridge] slot %d disconnected: %s" % (slot, exc), flush=True)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if managed_stop:
            if stopping.wait(delay):
                break
        else:
            time.sleep(delay)
        delay = min(delay * 2, SLOT_RETRY_CEILING_SECONDS)


def write_metadata(path, data):
    """Atomically publish modem identity for the control plane's /data bind mount."""
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def retain_hardware_identity(previous, observed):
    """Keep a verified IMEI when a later AT refresh is temporarily incomplete.

    ICCID is deliberately *not* retained: an empty ICCID can be real card removal and has to
    reach the control plane.  The modem's hardware IMEI cannot change while this bridge owns
    the same physical device, so replacing it with an empty/garbled read only destroys the
    evidence needed by an automatic line rebuild.
    """
    result = dict(observed or {})
    imei = str(result.get("imei") or "")
    retained = str((previous or {}).get("imei") or "")
    if not re.fullmatch(r"\d{15}", imei) and re.fullmatch(r"\d{15}", retained):
        result["imei"] = retained
    return result


def refresh_metadata(card, path, static, interval, initial_identity=None, stopping=None,
                     retirement=None):
    """Refresh identity and retire this channel generation after a proven hot swap/loss."""
    identity = dict(initial_identity or {})
    stopping = stopping or threading.Event()
    last_verified = (str(identity.get("iccid") or "")
                     if identity.get("iccid_verified") is True else "")
    consecutive_loss = 0

    def retire(reason, observed):
        nonlocal identity
        identity = retain_hardware_identity(identity, observed)
        failed = logical_channel_metadata(
            [], int(static.get("channel_requested") or 0), "error", reason)
        if retirement is not None:
            retirement.update({**failed, **identity})
        write_metadata(path, {**static, **failed, **identity,
                              "updated_at": int(time.time())})
        print("[bridge] card identity generation changed; retiring bridge (%s)" % reason,
              flush=True)
        stopping.set()

    while not stopping.wait(interval):
        try:
            observed = retain_hardware_identity(identity, card.refresh_identity(identity))
            if stopping.is_set():
                return
            verified = (str(observed.get("iccid") or "")
                        if observed.get("iccid_verified") is True else "")
            if last_verified and verified and verified != last_verified:
                retire("card_identity_changed", observed)
                return
            if last_verified and not verified:
                consecutive_loss += 1
            else:
                consecutive_loss = 0
            identity = observed
            write_metadata(path, {**static, **identity, "updated_at": int(time.time())})
            if verified:
                last_verified = verified
            if last_verified and consecutive_loss >= IDENTITY_LOSS_LIMIT:
                retire("card_identity_unavailable", identity)
                return
        except Exception as exc:
            if stopping.is_set():
                return
            identity = retain_hardware_identity(identity, {
                "iccid": "", "iccid_verified": False, "iccid_source": "unknown"})
            write_metadata(path, {**static, **identity, "updated_at": int(time.time())})
            consecutive_loss = consecutive_loss + 1 if last_verified else 0
            print("[bridge] identity refresh failed error_type=%s" % type(exc).__name__,
                  flush=True)
            if last_verified and consecutive_loss >= IDENTITY_LOSS_LIMIT:
                retire("card_identity_unavailable", identity)
                return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--modem", default="/dev/ttyUSB2")
    parser.add_argument("-H", "--host", default="127.0.0.1")
    parser.add_argument("-p", "--base-port", type=int, default=0x8C7B)
    parser.add_argument("-n", "--slots", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("-a", "--atr", default=DEFAULT_ATR.hex())
    parser.add_argument("--metadata-file", default="",
                        help="publish modem IMEI/ICCID JSON for the control plane")
    parser.add_argument("--hardware-id", default="",
                        help="stable physical modem id assigned by the host orchestrator")
    parser.add_argument("--usb-generation", default="",
                        help="non-secret USB attachment generation assigned by the orchestrator")
    parser.add_argument("--modemmanager", default="",
                        help="route AT commands through this ModemManager modem index/path")
    parser.add_argument("--identity-refresh", type=int, default=15,
                        help="seconds between IMEI/ICCID metadata refreshes (0 disables)")
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()

    card = (ModemManagerCard(args.modemmanager, debug=args.debug)
            if args.modemmanager else ModemCard(args.modem, debug=args.debug))
    identity = card.identity()
    with open(f"/proc/{os.getpid()}/stat", encoding="ascii") as handle:
        bridge_start = handle.read().rsplit(")", 1)[1].split()[19]
    static_metadata = {
        "bridge_start": bridge_start,
        "bridge_generation": uuid.uuid4().hex,
        "version": 1,
        # Lets the orchestrator prove that a ready metadata document came from the
        # replacement bridge, rather than accepting the previous process's last write.
        "bridge_pid": os.getpid(),
        "modem": os.path.realpath(args.modem),
        "base_port": args.base_port,
        "slots": args.slots,
        "hardware_id": args.hardware_id,
        **logical_channel_metadata([], args.slots, "allocating"),
    }
    if args.usb_generation:
        static_metadata["usb_generation"] = args.usb_generation
    write_metadata(args.metadata_file, {**static_metadata, **identity,
                                        "updated_at": int(time.time())})
    print("[bridge] modem identity refreshed (imei=%s iccid=%s)" %
          ("available" if identity["imei"] else "unavailable",
           "available" if identity["iccid"] else "unavailable"), flush=True)
    try:
        channels = allocate_logical_channels_with_recovery(card, args.slots)
    except ModemError as exc:
        static_metadata.update(logical_channel_metadata([], args.slots, "error", str(exc)))
        write_metadata(args.metadata_file, {**static_metadata, **identity,
                                            "updated_at": int(time.time())})
        print("[bridge] %s" % exc, flush=True)
        card.close()
        raise
    slot_channels = slot_channel_map(channels, args.slots)
    static_metadata.update(logical_channel_metadata(channels, args.slots, "ready",
                                                    slot_channels=slot_channels))
    write_metadata(args.metadata_file, {**static_metadata, **identity,
                                        "updated_at": int(time.time())})
    print("[bridge] allocated logical channels %r" % channels, flush=True)
    if len(set(slot_channels)) < len(slot_channels):
        print("[bridge] slot channel map %r (shared)" % slot_channels, flush=True)

    # Allocation/identity work is not runtime slot traffic. Monitor only this generation's
    # owned channels after allocation, so a healthy basic channel cannot hide a dead slot.
    card.access_health = SIMAccessHealth(channels)
    atr = bytes.fromhex(args.atr)
    stopping = threading.Event()
    card.stopping = stopping
    retirement = {}
    arbiters = {channel: ChannelArbiter(card, channel) for channel in channels}
    threads = []
    for slot, channel in enumerate(slot_channels):
        thread = threading.Thread(
            target=serve_slot,
            args=(
                arbiters[channel],
                args.host,
                args.base_port + slot,
                slot,
                atr,
                args.debug,
                stopping,
            ),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    if args.metadata_file and args.identity_refresh > 0:
        metadata_thread = threading.Thread(
            target=refresh_metadata,
            args=(card, args.metadata_file, static_metadata,
                  args.identity_refresh, identity, stopping, retirement), daemon=True)
        metadata_thread.start()
        threads.append(metadata_thread)

    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    try:
        while (not stopping.is_set() and not card.access_health.failed.is_set()
               and all(thread.is_alive() for thread in threads)):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        deadline = time.monotonic() + BRIDGE_WORKER_STOP_SECONDS
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            # Python cannot safely cancel an AT worker. End the complete bridge process
            # without issuing CLOSE while a worker still owns the card/port. A successor
            # must allocate its own channels; it never guesses which old sessions to close.
            failed_metadata = logical_channel_metadata(
                channels, args.slots, "error",
                "SIM access workers did not stop; channel cleanup could not be completed",
                slot_channels=slot_channels)
            try:
                write_metadata(args.metadata_file, {**static_metadata, **identity, **retirement,
                                                    **failed_metadata, "channel_cleanup_pending": True,
                                                    "channel_error_code": "sim_access_cleanup_incomplete",
                                                    "updated_at": int(time.time())})
            finally:
                os._exit(SIM_ACCESS_CLEANUP_EXIT)
            return SIM_ACCESS_CLEANUP_EXIT
        card.stopping = None
        # Release the UICC logical channels before handing the modem to ModemManager.
        # A raw SIGTERM used to leak all three channels until the next modem reset.
        # A newly proved physical card cannot inherit the old card's channel ownership.
        # Its baseband may already own those numbers, so do not CLOSE old-generation slots.
        if retirement.get("channel_error") == "card_identity_changed":
            write_metadata(args.metadata_file, {**static_metadata, **identity, **retirement,
                                                "updated_at": int(time.time())})
            card.close()
            return 0
        unclosed = []
        for channel in channels:
            if card.close_channel(channel) is not True:
                unclosed.append(channel)
        if unclosed:
            failed_metadata = logical_channel_metadata(
                unclosed, args.slots, "error", "SIM logical channel cleanup could not be confirmed")
            try:
                write_metadata(args.metadata_file, {**static_metadata, **identity, **retirement,
                                                    **failed_metadata, "channel_cleanup_pending": True,
                                                    "channel_error_code": "sim_access_cleanup_incomplete",
                                                    "updated_at": int(time.time())})
            except OSError:
                print("[bridge] channel cleanup failed; metadata could not be published", flush=True)
            finally:
                card.close()
            return SIM_ACCESS_CLEANUP_EXIT
        failed = card.access_health.failed.is_set() and not retirement
        error = "SIM access failed repeatedly; rebuilding logical channels" if failed else ""
        static_metadata.update(logical_channel_metadata(
            [], args.slots, "error" if failed else "stopped", error))
        if failed:
            static_metadata["channel_error_code"] = "sim_access_runtime_failed"
        write_metadata(args.metadata_file, {**static_metadata, **identity, **retirement,
                                            "updated_at": int(time.time())})
        card.close()
    if failed:
        print("[bridge] %s" % error, flush=True)
        return SIM_ACCESS_RECOVERY_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main())
