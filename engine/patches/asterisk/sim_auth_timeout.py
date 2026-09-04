"""Give serial-backed eUICCs enough time to answer IMS-AKA challenges.

The sysmocom Asterisk fork allows only three seconds from its AMI ``AuthRequest`` to the
matching ``AuthResponse``.  A native USB CCID reader answers comfortably inside that window,
but an EC25 path serializes several APDUs through PC/SC, VPCD and AT+CSIM; a successful field
exchange took 2.55 seconds and ordinary jitter made otherwise valid registrations intermittent.

Eight seconds remains bounded well below the registration transaction timeout while leaving
enough margin for the supported modem bridge.  Patch the exact pinned-source constant and fail
the Engine build if upstream changes it, rather than silently shipping the old deadline.
"""

import os
import sys
from pathlib import Path


SOURCE = Path(os.environ.get("AST_SRC", "/home/asterisk-build/asterisk")) \
    / "res/res_pjsip_outbound_registration.c"

ORIGINAL = "#define SIM_TIMEOUT 3\n"
PATCHED = "#define SIM_TIMEOUT 8 /* PATCH sim_auth_timeout */\n"


def patch(source: str) -> str:
    if PATCHED in source:
        return source
    if source.count(ORIGINAL) != 1:
        raise ValueError("expected one unpatched SIM_TIMEOUT definition")
    return source.replace(ORIGINAL, PATCHED, 1)


try:
    original = SOURCE.read_text()
    updated = patch(original)
except (OSError, ValueError) as exc:
    print(f"SIM authentication timeout patch failed: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc

if updated == original:
    print("SIM authentication timeout already patched")
else:
    SOURCE.write_text(updated)
    print("extended SIM authentication timeout for serial-backed eUICCs")
