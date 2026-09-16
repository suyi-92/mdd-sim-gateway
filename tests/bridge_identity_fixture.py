"""Direct-card metadata owned by this test process, with no hardware access."""
import os
import time
from pathlib import Path


def verified_bridge():
    start = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()[19]
    return {'iccid_verified': True, 'iccid_source': 'card', 'channel_status': 'ready',
            'bridge_pid': os.getpid(), 'bridge_start': start, 'updated_at': time.time(),
            'bridge_generation': 'fixture-session'}
