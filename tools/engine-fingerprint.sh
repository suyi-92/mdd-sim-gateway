#!/bin/sh
# Print the content fingerprint used to match a locally built Engine image to this checkout.
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
kind=${1:-}

case "$kind" in
  runtime|base) ;;
  *)
    printf 'usage: %s runtime|base\n' "$0" >&2
    exit 2
    ;;
esac

python3 - "$repo_dir" "$kind" "${PCSC_VERSION:-2.3.3}" <<'PY'
import hashlib
import os
import subprocess
import sys

repo, kind, pcsc_version = sys.argv[1:]
digest = hashlib.sha256()


def tracked_files(*pathspecs):
    result = subprocess.run(
        ["git", "-C", repo, "ls-files", "-z", "--", *pathspecs],
        check=True,
        stdout=subprocess.PIPE,
    )
    return sorted(path for path in result.stdout.split(b"\0") if path)


def add_file(relative):
    path = os.path.join(os.fsencode(repo), relative)
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)


if kind == "runtime":
    runtime_files = [
        b"engine/pin_keeper.py",
        b"engine/ami_usim.py",
        b"engine/swu_ike.py",
        b"engine/log_capture.py",
        b"engine/render.py",
        b"engine/notify.py",
        b"engine/entrypoint.sh",
    ]
    managed_runtime = set(
        tracked_files(*(os.fsdecode(path) for path in runtime_files))
    )
    for relative in runtime_files:
        if relative in managed_runtime:
            add_file(relative)
    for relative in tracked_files("engine/templates"):
        add_file(relative)
else:
    dockerfile = b"engine/Dockerfile"
    if dockerfile not in tracked_files(os.fsdecode(dockerfile)):
        raise SystemExit("engine/Dockerfile is not managed by Git")
    add_file(dockerfile)
    digest.update(f"pcsc={pcsc_version}\n".encode())
    for relative in tracked_files("engine/patches"):
        add_file(relative)

print(digest.hexdigest())
PY
