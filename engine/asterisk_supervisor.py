#!/usr/bin/env python3
"""Own Asterisk's lifetime without disturbing PIN/AKA or the established SWu tunnel.

P-CSCF updates are requests, never an in-place PJSIP module reload. Only a confirmed
idle process is replaced; a crash is restarted separately from the Engine container.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

try:
    from . import log_capture, stability_log as evidence
except ImportError:
    import log_capture
    import stability_log as evidence

RUNDIR = Path(os.environ.get("MDD_RUNDIR", "/run/mdd-sim-gateway"))
LOGDIR = Path("/logs")


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def request_restart(reason, rundir=RUNDIR):
    if reason not in {"pcscf_changed", "reg_unanswered"}:
        raise ValueError("invalid Asterisk restart reason")
    ticket = {"request_id": uuid.uuid4().hex, "reason_code": reason, "ts": time.time()}
    atomic_json(rundir / "asterisk.request.json", ticket)
    return ticket["request_id"]


def cli(command):
    try:
        result = subprocess.run(["asterisk", "-rx", command], capture_output=True,
                                text=True, timeout=3)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def active_channels():
    match = re.search(r"\b(\d+) active channels?\b", cli("core show channels count"), re.I)
    return int(match.group(1)) if match else None


def restart_allowed(tunnel_state, channels):
    return tunnel_state == "CONNECTED" and channels == 0


def trace_file(logdir, session):
    directory = logdir / "crash"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise OSError("unsafe crash directory")
    paths = sorted((p for p in directory.glob("asterisk-*.trace")
                    if re.fullmatch(r"asterisk-[a-f0-9]{32}\.trace", p.name)),
                   key=lambda p: p.lstat().st_mtime, reverse=True)
    for index, path in enumerate(paths):
        if index >= 19 or path.lstat().st_mtime < time.time() - 7 * 86400:
            path.unlink()
    path = directory / f"asterisk-{session}.trace"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return path, fd


class Supervisor:
    def __init__(self, rundir=RUNDIR, logdir=LOGDIR):
        self.run = rundir
        self.logs = logdir
        self.child = None
        self.stopping = False
        self.session = ""
        self.restarts = 0
        self.applied = ""
        self.last_deferred = 0
        self.trace = None
        self.capture = None
        self.next_start = 0
        self.started_at = 0
        self.failures = 0
        self.restart_pending = False
        self.last_sample = 0
        self.stop_requested_at = 0

    def event(self, name, **facts):
        if not evidence.record(self.logs, "asterisk", name, asterisk_session=self.session,
                               asterisk_pid=self.child.pid if self.child else 0, **facts):
            print("[asterisk-supervisor] stability log write failed", flush=True)

    def status(self, phase):
        atomic_json(self.run / "asterisk.status.json", {
            "phase": phase, "session": self.session, "request_id": self.applied,
            "engine_session": os.environ.get("MDD_ENGINE_SESSION", ""),
            "restart_count": self.restarts, "ts": time.time(),
            "pid": self.child.pid if self.child else None})

    def start(self):
        # Capture the exact request rendered. A newer ticket arriving during render remains
        # pending for the next pass; it must never be acknowledged by this generation.
        ticket = read_json(self.run / "asterisk.request.json")
        self.session = uuid.uuid4().hex
        try:
            result = subprocess.run([sys.executable, "/usr/local/bin/render.py"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            self.event("diagnostics_failed", reason_code="render_failed")
            return False
        if result.returncode:
            self.event("diagnostics_failed", reason_code="render_failed")
            return False
        descriptor = None
        env = dict(os.environ)
        try:
            self.trace, descriptor = trace_file(self.logs, self.session)
            env.update(LD_PRELOAD="/usr/local/lib/mdd-crash-trace.so", MDD_CRASH_FD=str(descriptor))
        except OSError:
            self.trace = None
            self.event("diagnostics_failed", reason_code="log_write_failed")
        try:
            self.child = subprocess.Popen(["asterisk", "-f"], env=env, pass_fds=(descriptor,) if descriptor is not None else (),
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          text=True, errors="replace", start_new_session=True)
        except OSError:
            self.event("asterisk_exited", reason_code="engine_start_failed")
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)
        self.capture = threading.Thread(target=self.capture_console, args=(self.child,), daemon=True)
        self.capture.start()
        self.applied = ticket.get("request_id", "")
        self.started_at = time.monotonic()
        self.restart_pending = False
        self.status("running")
        self.event("asterisk_started", request_id=self.applied, engine_restart_count=self.restarts)
        if self.applied:
            self.event("asterisk_restart_applied", request_id=self.applied,
                       reason_code=ticket.get("reason_code"))
        return True

    def capture_console(self, child):
        def lines():
            for line in child.stdout:
                # Docker retains its normal console and diagnostics retain timestamps across
                # Asterisk-only restarts and container replacement. Never enable SIP payload tracing.
                print(line, end="", flush=True)
                yield line
        try:
            log_capture.capture(lines(), self.logs / "asterisk-current.log", self.logs / "asterisk",
                                segment_bytes=2 * 1024 * 1024, max_total_bytes=20 * 1024 * 1024)
        except Exception:
            self.event("diagnostics_failed", reason_code="log_write_failed")
            # Keep draining the pipe if the log filesystem fails; otherwise Asterisk blocks.
            for line in child.stdout:
                print(line, end="", flush=True)

    def stop_child(self):
        if self.child and self.child.poll() is None:
            self.status("stopping")
            # Caller proves idle; SIGTERM does not reload/free live PJSIP configuration.
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=2)

    def exited(self):
        code = self.child.returncode
        self.event("asterisk_exited", signal_code=-code if code < 0 else 0,
                   exit_code=code if code >= 0 else 128 - code,
                   reason_code="manual" if self.stopping or self.restart_pending else "process_exit")
        if code < 0 and -code in {signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS, signal.SIGILL, signal.SIGFPE}:
            try:
                trace = self.trace.read_text()[:65536] if self.trace else ""
            except OSError:
                trace = ""
            self.event("native_crash", signal_code=-code, trace_available=bool(trace),
                       trace_complete="MDD_CRASH_END" in trace, stack_frames=trace.count("frame="))
        if self.capture:
            self.capture.join(timeout=2)
        if self.trace and self.trace.exists() and self.trace.stat().st_size == 0:
            self.trace.unlink()
        self.restarts += 1
        self.failures = 0 if self.restart_pending or time.monotonic() - self.started_at > 60 else self.failures + 1
        self.next_start = time.monotonic() + (min(30, 2 ** min(self.failures, 5)) if self.failures else 0)
        self.child = None

    def sample(self):
        if time.monotonic() - self.last_sample < 30:
            return
        self.last_sample = time.monotonic()
        facts = {}
        for field in ("rx_packets", "tx_packets", "rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
            try:
                facts[field] = int((Path("/sys/class/net/ipsec0/statistics") / field).read_text())
            except (OSError, ValueError):
                pass
        self.event("network_sample", state=read_json(self.run / "swu_status.json").get("state"), **facts)

    def tick(self):
        if os.getpid() == 1:
            # Python is the container init: reap orphaned SWu workers as well as Asterisk.
            # Preserve Asterisk's wait status if it exits between poll and this collection.
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if not pid:
                    break
                if self.child and pid == self.child.pid:
                    self.child.returncode = os.waitstatus_to_exitcode(status)
        self.sample()
        if self.child is None:
            if time.monotonic() >= self.next_start:
                if not self.start():
                    self.next_start = time.monotonic() + 10
            return
        if self.child.poll() is not None:
            self.exited()
            return
        if self.restart_pending:
            if time.monotonic() - self.stop_requested_at > 15 and active_channels() == 0:
                self.stop_child()  # bounded fallback only after another confirmed idle check
            return  # Asterisk drains any call that raced the idle check before exiting.
        ticket = read_json(self.run / "asterisk.request.json")
        if ticket.get("request_id") and ticket["request_id"] != self.applied:
            state = read_json(self.run / "swu_status.json").get("state")
            channels = active_channels()
            if restart_allowed(state, channels):
                self.event("asterisk_restart_requested", request_id=ticket["request_id"],
                           reason_code=ticket.get("reason_code"), active_channels=channels)
                cli("core stop gracefully")
                self.restart_pending = True
                self.stop_requested_at = time.monotonic()
                self.status("stopping")
            elif time.monotonic() - self.last_deferred >= 30:
                self.last_deferred = time.monotonic()
                self.event("asterisk_restart_deferred", request_id=ticket["request_id"],
                           reason_code="tunnel_network" if state != "CONNECTED" else
                           ("call_state_unknown" if channels is None else "call_active"),
                           active_channels=channels)

    def main(self):
        def stop(*_):
            self.stopping = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        try:
            while not self.stopping:
                self.tick()
                time.sleep(1)
        finally:
            if os.getpid() == 1:
                # This process is the container init. Terminate the inherited PIN/AMI/SWu
                # workers too, so Docker stop releases PC/SC within its grace window.
                os.kill(-1, signal.SIGTERM)
            self.stop_child()
            if self.child:
                self.exited()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", choices=("reg_unanswered", "pcscf_changed"))
    args = parser.parse_args()
    os.umask(0o077)
    if args.request:
        print(request_restart(args.request))
    else:
        Supervisor().main()


if __name__ == "__main__":
    main()
