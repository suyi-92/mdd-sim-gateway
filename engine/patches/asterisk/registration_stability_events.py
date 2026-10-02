"""Record actual REGISTER response/timeout metadata without SIP identities or payloads."""
import os
from pathlib import Path

SOURCE = Path(os.environ.get("AST_SRC", "/home/asterisk-build/asterisk")) / "res/res_pjsip_outbound_registration.c"
MARKER = "PATCH registration_stability_events"
# Run after response expiration is populated. The later Contact-expiry patch inserts its
# correction before this assignment, so diagnostics report the granted lifetime too.
ANCHOR = '\tresponse->client_state = client_state;\n\tpj_timer_entry_init(&response->sim_timer, 0, response, sim_timeout_cb);'
INSERT = '''\t/* PATCH registration_stability_events: codes only; no URI, challenge or keys. */
\tmanager_event(EVENT_FLAG_SYSTEM, "MDDRegisterResponse",
\t\t"StatusCode: %d\\r\\nResponseReceived: %d\\r\\nExpirationSeconds: %d\\r\\n"
\t\t"CSeq: %d\\r\\nProcessId: %ld\\r\\nTransportId: %p\\r\\n",
\t\tparam->code, param->rdata != NULL, response->expiration,
\t\tparam->rdata && param->rdata->msg_info.cseq ? param->rdata->msg_info.cseq->cseq : 0,
\t\t(long) getpid(), param->rdata ? (void *) param->rdata->tp_info.transport : NULL);

\tresponse->client_state = client_state;
\tpj_timer_entry_init(&response->sim_timer, 0, response, sim_timeout_cb);'''


def main():
    source = SOURCE.read_text()
    if MARKER in source:
        return
    if source.count(ANCHOR) != 1:
        raise SystemExit("registration stability patch: pinned source anchor mismatch")
    if '#include "asterisk/manager.h"' not in source:
        source = source.replace('#include "asterisk.h"', '#include "asterisk.h"\n#include "asterisk/manager.h"\n#include <unistd.h>', 1)
    SOURCE.write_text(source.replace(ANCHOR, INSERT, 1))


if __name__ == "__main__":
    main()
