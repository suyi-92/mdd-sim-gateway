"""Record actual REGISTER response/timeout metadata without SIP identities or payloads."""
import os
from pathlib import Path

SOURCE = Path(os.environ.get("AST_SRC", "/home/asterisk-build/asterisk")) / "res/res_pjsip_outbound_registration.c"
MARKER = "PATCH registration_stability_events"
ANCHOR = '\tint *callback_invoked;\n\n\tcallback_invoked = ast_threadstorage_get(&register_callback_invoked, sizeof(int));'
INSERT = '''\tint *callback_invoked;
\t/* PATCH registration_stability_events: codes only; no URI, challenge or keys. */
\tmanager_event(EVENT_FLAG_SYSTEM, "MDDRegisterResponse",
\t\t"StatusCode: %d\\r\\nResponseReceived: %d\\r\\nExpirationSeconds: %d\\r\\n"
\t\t"CSeq: %d\\r\\nProcessId: %ld\\r\\nTransportId: %p\\r\\n",
\t\tparam->code, param->rdata != NULL, (int) param->expiration,
\t\tparam->rdata && param->rdata->msg_info.cseq ? param->rdata->msg_info.cseq->cseq : 0,
\t\t(long) getpid(), param->rdata ? (void *) param->rdata->tp_info.transport : NULL);

\tcallback_invoked = ast_threadstorage_get(&register_callback_invoked, sizeof(int));'''


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
