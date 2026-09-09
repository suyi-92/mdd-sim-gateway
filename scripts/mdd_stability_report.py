#!/usr/bin/env python3
"""Read only the closed stability records and print a shareable, bounded summary."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

EVENTS = {"session_started", "ike_request_timeout", "child_rekey_sent", "child_rekey_rejected", "child_rekey_retry",
          "child_rekey_fallback", "child_rekey_complete", "ike_rekey_sent", "ike_rekey_rejected",
          "ike_rekey_complete", "tunnel_down", "health_sample", "health_changed", "ims_transport",
          "recovery_scheduled", "recovery_started", "recovery_succeeded", "recovery_failed",
          "recovery_cancelled", "client_request"}
LOG_NAME = re.compile(r"stability-(?:ike|control)(?:\.[1-6])?\.jsonl\Z")


def records(path: Path, since: float):
    if not LOG_NAME.fullmatch(path.name):
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            item = os.fstat(stream.fileno())
            if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1
                    or item.st_uid != os.geteuid() or item.st_mode & 0o077
                    or item.st_size > 3 * 1024 * 1024):
                return
            for line in stream:
                if len(line) > 4096:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if (not isinstance(value, dict) or value.get("schema") != 1
                        or not isinstance(value.get("event"), str) or value["event"] not in EVENTS
                        or not isinstance(value.get("ts"), (int, float))
                        or not since <= value["ts"] <= time.time() + 60):
                    continue
                yield value
    except OSError:
        return


def summarize(data_dir: Path, hours: int = 24) -> dict:
    if not 1 <= hours <= 168:
        raise ValueError("hours must be 1-168")
    since = time.time() - hours * 3600
    roots = [("webui", data_dir / "logs")]
    base = data_dir / "instances"
    if base.is_dir() and not base.is_symlink():
        for directory in sorted(base.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            # Nonstandard ids could contain an operator label: use an opaque local alias.
            name = directory.name if re.fullmatch(r"[0-9]{1,3}", directory.name) else \
                hashlib.sha256(directory.name.encode()).hexdigest()[:8]
            roots.append(("line-" + name, directory / "logs"))
    result = []
    for name, directory in roots:
        if not directory.is_dir() or directory.is_symlink():
            continue
        counts, notify, transport = Counter(), Counter(), Counter()
        total = 0
        for path in sorted(directory.glob("stability-*.jsonl")):
            for record in records(path, since):
                event = record["event"]
                counts[event] += 1
                total += 1
                code = record.get("notify_code")
                if event == "child_rekey_rejected" and isinstance(code, int) and 0 <= code <= 65535:
                    notify[str(code)] += 1
                if event == "ims_transport" and record.get("transport_state") == "disconnected":
                    code = record.get("status_code")
                    if isinstance(code, int) and 0 <= code <= 0xFFFFFFFF:
                        transport[str(code)] += 1
        if total:
            result.append({"line": name, "records": total, "events": dict(sorted(counts.items())),
                           "child_rekey_rejections": dict(sorted(notify.items())),
                           "transport_disconnect_status": dict(sorted(transport.items()))})
    return {"hours": hours, "lines": result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = summarize(args.data_dir, args.hours)
    except ValueError as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"Stability evidence: last {result['hours']} hours")
        if not result["lines"]:
            print("No stability records in this window.")
        for line in result["lines"]:
            print(f"{line['line']}: {line['records']} records")
            print("  " + ", ".join(f"{key}={value}" for key, value in line["events"].items()))
            if line["child_rekey_rejections"]:
                print("  CHILD notify codes: " + json.dumps(line["child_rekey_rejections"], sort_keys=True))
            if line["transport_disconnect_status"]:
                print("  Transport status codes: " + json.dumps(line["transport_disconnect_status"], sort_keys=True))


if __name__ == "__main__":
    main()
