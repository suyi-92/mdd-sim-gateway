#!/usr/bin/env python3
"""Exercise the compiled, pinned lpac library with fake APDUs; no hardware access."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HARNESS = r'''
#include <euicc/euicc.h>
#include <euicc/es10c.h>
#include <euicc/interface.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int scenario, calls;
static unsigned char tag;
static int transmit(struct euicc_ctx *ctx, uint8_t **rx, uint32_t *len,
                    const uint8_t *tx, uint32_t txlen) {
    const unsigned char ok[] = {0xBF, tag, 3, 0x80, 1, 0, 0x90, 0};
    unsigned char buf[16];
    (void)ctx;
    calls++;
    /* This callback is the only transport; no PC/SC or HTTP driver is linked. */
    if (calls == 1 && (txlen < 8 || tx[1] != 0xE2 || tx[5] != 0xBF || tx[6] != tag))
        abort();
    memcpy(buf, ok, sizeof(ok));
    *len = sizeof(ok);
    switch (scenario) {
    case 1: fprintf(stderr, "SCardTransmit() failed: 80100068 (reset fixture)\n"); return -1;
    case 2: buf[0] = 0x69; buf[1] = 0x85; *len = 2; break;
    case 3: buf[0] = 0x90; *len = 1; break;
    case 4: buf[1] = 0x01; break;
    case 5: buf[3] = 0x81; break;
    case 6: buf[5] = 3; break;
    case 7:
        if (calls == 1) { buf[0] = 0x61; buf[1] = 8; *len = 2; }
        else { buf[0] = 0x6A; buf[1] = 0x82; *len = 2; }
        break;
    }
    *rx = malloc(*len);
    memcpy(*rx, buf, *len);
    return 0;
}
int main(int argc, char **argv) {
    struct euicc_ctx ctx = {0};
    struct euicc_apdu_interface interface = {0};
    int result;
    const char *id;
    if (argc != 3) return 64;
    scenario = atoi(argv[1]); tag = (unsigned char)atoi(argv[2]);
    interface.transmit = transmit; ctx.apdu.interface = &interface; ctx.es10x_mss = 120;
    id = scenario == 8 ? "not-a-profile" : "8900000000000000001";
    fprintf(stderr, "SW=6F00\n"); /* unrelated initialization */
    if (tag == 0x31) result = es10c_enable_profile(&ctx, id, 1);
    else if (tag == 0x32) result = es10c_disable_profile(&ctx, id, 0);
    else result = es10c_delete_profile(&ctx, id);
    fprintf(stderr, "SCardTransmit() failed: 80100069 (cleanup fixture)\n");
    printf("%d %d\n", result, calls);
    return 0;
}
'''


def check(source: Path, build: Path) -> None:
    module_path = Path(__file__).resolve().parents[1] / 'control/app/lpa_diagnostics.py'
    spec = importlib.util.spec_from_file_location('lpa_diagnostics', module_path)
    diagnostics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostics)
    with tempfile.TemporaryDirectory(prefix='mdd-lpac-check-') as directory:
        root = Path(directory)
        (root / 'check.c').write_text(HARNESS)
        subprocess.run([os.environ.get('CC', 'cc'), '-I', str(source), str(root / 'check.c'),
                        str(build / 'euicc/libeuicc.a'), str(build / 'cjson/libcjson-static.a'),
                        '-o', str(root / 'check')], check=True)
        expectations = {
            0: {'card_result': 0, 'status_word': '9000'},
            1: {'failure_stage': 'apdu_transport', 'pcsc_code': '80100068'},
            2: {'failure_stage': 'response_status', 'status_word': '6985'},
            3: {'failure_stage': 'apdu_response'},
            4: {'failure_stage': 'response_tag', 'status_word': '9000'},
            5: {'failure_stage': 'response_result', 'status_word': '9000'},
            6: {'card_result': 3, 'status_word': '9000'},
            7: {'failure_stage': 'response_status', 'status_word': '6A82'},
            8: {'failure_stage': 'identifier_encoding'},
        }
        for tag in (0x31, 0x32, 0x33):
            for scenario, expected in expectations.items():
                result = subprocess.run([str(root / 'check'), str(scenario), str(tag)],
                                        check=True, capture_output=True, text=True)
                actual = diagnostics.profile_transport_diagnostic(result.stderr)
                if actual != expected:
                    raise SystemExit(f'lpac diagnostic check failed: scenario={scenario} tag={tag} {actual!r}')
                code, calls = map(int, result.stdout.split())
                expected_code = 0 if scenario == 0 else 3 if scenario == 6 else -1
                expected_calls = 0 if scenario == 8 else 2 if scenario == 7 else 1
                if (code, calls) != (expected_code, expected_calls):
                    raise SystemExit('lpac changed command result or replayed a write')
    print('lpac native diagnostics: 27 simulated exchanges passed; no hardware accessed')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: check-lpac-diagnostics.py PINNED_SOURCE BUILD_DIR')
    check(Path(sys.argv[1]), Path(sys.argv[2]))
