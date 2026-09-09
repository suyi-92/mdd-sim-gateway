"""Emit address-free TCP/TLS transport state and status codes over the existing AMI."""
import os
from pathlib import Path

SOURCE = Path(os.environ.get("AST_SRC", "/home/asterisk-build/asterisk")) / "res/res_pjsip/pjsip_transport_management.c"
MARKER = "PATCH transport_stability_events"
ANCHOR = '\tstruct ao2_container *transports;\n\n\t/* We only care about reliable transports */'
INSERT = '''\tstruct ao2_container *transports;
\t/* PATCH transport_stability_events: no addresses, transport names or SIP identities. */
\tconst char *mdd_state = NULL;
\tswitch (state) {
\tcase PJSIP_TP_STATE_CONNECTED: mdd_state = "connected"; break;
\tcase PJSIP_TP_STATE_DISCONNECTED: mdd_state = "disconnected"; break;
\tcase PJSIP_TP_STATE_SHUTDOWN: mdd_state = "shutdown"; break;
\tdefault: break;
\t}
\tif (mdd_state && PJSIP_TRANSPORT_IS_RELIABLE(transport)) {
\t\tmanager_event(EVENT_FLAG_SYSTEM, "MDDTransportState",
\t\t\t"State: %s\\r\\nProtocol: %s\\r\\nStatusCode: %d\\r\\nDirectionCode: %d\\r\\n",
\t\t\tmdd_state, transport->type_name, info ? (int) info->status : 0, (int) transport->dir);
\t}

\t/* We only care about reliable transports */'''


def main():
    source = SOURCE.read_text()
    if MARKER in source:
        return
    include = '#include "asterisk/module.h"\n'
    if source.count(ANCHOR) != 1 or source.count(include) != 1:
        raise SystemExit("transport stability patch: pinned source anchor mismatch")
    source = source.replace(include, include + '#include "asterisk/manager.h"\n', 1)
    SOURCE.write_text(source.replace(ANCHOR, INSERT, 1))


if __name__ == "__main__":
    main()
