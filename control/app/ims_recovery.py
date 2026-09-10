"""Bounded IMS recovery before escalating to a complete SIM/Engine rebuild."""
import asyncio
import time
from pathlib import Path

from . import config as cfg, egress, engine, stability

REGISTER_WAIT = 160.0  # PJSIP timer B is 128 seconds; allow the complete transaction.
ASTERISK_WAIT = 200.0
_attempts = {}


async def _evidence(iid, inst, generation):
    try:
        runtime = await asyncio.to_thread(engine.container_runtime, iid)
        if runtime.get("container_id") != generation:
            return
        await asyncio.to_thread(engine.capture_diagnostics, iid, inst,
                                str(Path(cfg.DATA_DIR) / "instances" / iid), "ims-soft-recovery")
        country = egress.line_country(inst)
        state = (egress.status().get("exits") or {}).get(country) or {}
        revision = state.get("config_revision")
        if state.get("ready") and state.get("proxy_port"):
            try:
                latency = await asyncio.to_thread(egress.test_udp_proxy, "127.0.0.1",
                                                  int(state["proxy_port"]), 12.0)
                ok = True
            except Exception:
                latency, ok = 0, False
            current = (egress.status().get("exits") or {}).get(country) or {}
            after = await asyncio.to_thread(engine.container_runtime, iid)
            stability.event(iid, "exit_probe", inst=inst, exit_ready=ok, elapsed_ms=int(latency),
                            exit_revision=revision, container_ref=stability._writer.fingerprint(generation),
                            config_matches=current.get("config_revision") == revision
                            and after.get("container_id") == generation)
    except Exception:
        stability.event(iid, "diagnostics_failed", reason_code="unknown")


async def hold(iid, inst, status, runtime, ami) -> bool:
    """True means this observation must not spend the full Engine rebuild budget.

    No task mutates a different container generation. Active/unknown calls pause recovery;
    a missing card, PIN failure, disabled line or lost SWu leaves its existing policy intact.
    """
    iid = str(iid)
    generation = runtime.get("container_id")
    attempt = _attempts.get(iid)
    if attempt and attempt["generation"] != generation:
        _attempts.pop(iid, None)
        attempt = None
    if (not inst.get("enabled", True) or not runtime.get("running")
            or status.get("state") != "REGISTERING"):
        if attempt and status.get("state") == "OK":
            stability.event(iid, "ims_recovery_succeeded", inst=inst, phase=attempt["phase"],
                            elapsed_ms=int((time.monotonic() - attempt["started"]) * 1000))
        _attempts.pop(iid, None)
        return False
    if not generation or (attempt is None and status.get("reason_code") != "reg_unanswered"):
        return False
    if not await asyncio.to_thread(engine.tunnel_installed, iid):
        _attempts.pop(iid, None)
        return False
    current = await asyncio.to_thread(cfg.get_instance, iid)
    if not current or not current.get("enabled", True):
        _attempts.pop(iid, None)
        return True  # The next poll enforces the user's stop; do not launch recovery now.
    channels = (status.get("detail") or {}).get("active_channels")
    if channels is None and ami:
        channels = await ami.active_channel_count()
    if channels != 0:
        return True  # Never let the generic timeout tear down an active/unobservable call.
    now = time.monotonic()
    if attempt is None:
        attempt = {"generation": generation, "phase": "register", "started": now,
                   "deadline": now + REGISTER_WAIT}
        _attempts[iid] = attempt
        task = asyncio.create_task(_evidence(iid, inst, generation))
        # Retain the evidence task until completion; it never controls recovery timing.
        attempt["evidence_task"] = task
        accepted = await ami.reregister() if ami else False
        stability.event(iid, "ims_recovery_started", inst=inst, phase="register",
                        response_received=bool(accepted), active_channels=0,
                        container_ref=stability._writer.fingerprint(generation))
        if not accepted:
            attempt["deadline"] = now  # proceed to process recovery on the next poll
        return True
    if attempt["phase"] == "engine":
        return False
    if now < attempt["deadline"]:
        return True
    if attempt["phase"] == "register":
        ticket = await asyncio.to_thread(engine.request_ims_restart, iid, generation)
        attempt.update(phase="asterisk", deadline=now + ASTERISK_WAIT, request_id=ticket)
        stability.event(iid, "ims_recovery_started", inst=inst, phase="asterisk", request_id=ticket,
                        active_channels=0, response_received=bool(ticket))
        if not ticket:
            attempt["deadline"] = now
        return True
    attempt["phase"] = "engine"
    stability.event(iid, "ims_recovery_failed", inst=inst, phase="asterisk",
                    reason_code="ims_recovery_exhausted",
                    elapsed_ms=int((now - attempt["started"]) * 1000))
    return False
