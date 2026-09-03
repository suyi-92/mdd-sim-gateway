# SPDX-License-Identifier: GPL-2.0-only
#
# LICENSE EXCEPTION TO THE REPOSITORY DEFAULT. The replacement below is a modified copy of
# Asterisk's volte_get_p_associated_uri(), so this patcher and the Asterisk source it produces
# remain GPL-2.0-only. See THIRD_PARTY_LICENSES.md.
"""Prefer the registered telephone identity used for originating IMS requests.

The sysmocom fork stores only the first URI from P-Associated-URI and later copies it into the
From header of every VoLTE/VoWiFi request. Some IMS registrations list an IMSI-derived IMPU
first and the subscriber's dialable SIP/TEL identities afterwards. The control plane already
skips that first IMPU when learning the line number, but the Engine still originated calls as
the IMSI identity; an IMS core can accept the request at the P-CSCF and terminate it at the TAS.

Scan every angle-bracketed identity in every P-Associated-URI header. Select the first dialable
identity (the same primary-number rule used by the control plane), preferring its matching SIP
form when the carrier also supplies one because that retains the published home domain. Fall
back to the original first-entry behaviour. Only identities actually returned by this
successful registration can be selected; no number or domain is guessed from configuration.
"""

import os
import sys
from pathlib import Path


SOURCE = Path(os.environ.get("AST_SRC", "/home/asterisk-build/asterisk")) \
    / "res/res_pjsip_outbound_registration/volte.c"

MARKER = "PATCH prefer_dialable_public_identity"

ORIGINAL_FN = r'''/* Store and get P-Associated-URI. */
pj_status_t volte_get_p_associated_uri(struct ast_sip_transport_state *transport_state, pjsip_rx_data *rdata)
{
	pjsip_generic_string_hdr *pau_hdr;
	int i, start = 0, end = 0;

	/* Get P-Associated-URI from header. */
	pau_hdr = pjsip_msg_find_hdr_by_name(rdata->msg_info.msg, &STR_P_ASSOCIATED_URI, NULL);
	if (!pau_hdr || !pau_hdr->hvalue.ptr) {
		ast_log(LOG_NOTICE, "Missing 'P-Associated-URI' in REGISTER response.");
		return -EINVAL;
	}

	/* Get first entry, enclosed by <>. */
	for (i = 0; i < pau_hdr->hvalue.slen; i++) {
		if (!start && pau_hdr->hvalue.ptr[i] == '<')
			start = i + 1;
		if (!end && pau_hdr->hvalue.ptr[i] == '>')
			end = i;
	}
	if (!start || !end) {
		ast_log(LOG_ERROR, "Missing a value, enclosed by '<....>' in 'P-Associated-URI' in REGISTER response.");
		return -EINVAL;
	}

	/* Store. */
	if (end - start < sizeof(transport_state->volte.p_associated_uri)) {
		memcpy(transport_state->volte.p_associated_uri, pau_hdr->hvalue.ptr + start, end - start);
		transport_state->volte.p_associated_uri[end - start] = '\0';
	} else {
		ast_log(LOG_ERROR, "P-Associated-URI' too large.");
		return -EINVAL;
	}

	return PJ_SUCCESS;
}'''

PATCHED_FN = r'''/* PATCH prefer_dialable_public_identity */
static pj_bool_t volte_pau_get_dialable_number(const char *uri, size_t len,
		pj_bool_t *sip, const char **number, size_t *number_len)
{
	size_t i;

	if (len >= 6 && !strncasecmp(uri, "sip:+", 5))
		*sip = PJ_TRUE;
	else if (len >= 6 && !strncasecmp(uri, "tel:+", 5))
		*sip = PJ_FALSE;
	else
		return PJ_FALSE;
	for (i = 5; i < len && uri[i] >= '0' && uri[i] <= '9'; i++)
		;
	/* Keep this aligned with the control plane's public-identity parser: a subscriber
	 * identity needs at least five digits after '+'. */
	if (i - 5 < 5)
		return PJ_FALSE;
	if ((*sip && (i >= len || uri[i] != '@'))
	    || (!*sip && i != len && uri[i] != ';' && uri[i] != '?'))
		return PJ_FALSE;
	*number = uri + 5;
	*number_len = i - 5;
	return PJ_TRUE;
}

/* Store the public identity used for originating From / P-Preferred-Identity headers. */
pj_status_t volte_get_p_associated_uri(struct ast_sip_transport_state *transport_state, pjsip_rx_data *rdata)
{
	pjsip_generic_string_hdr *pau_hdr = NULL;
	const char *fallback = NULL, *dialable = NULL, *dialable_sip = NULL;
	const char *dialable_number = NULL;
	size_t fallback_len = 0, dialable_len = 0, dialable_sip_len = 0;
	size_t dialable_number_len = 0;
	const char *selected;
	size_t selected_len;

	/* A registration can return several header fields and several comma-separated values in
	 * each field. The old code inspected only the first angle-bracketed value of the first
	 * field, which is commonly an IMSI-derived IMPU rather than the telephone identity. */
	while ((pau_hdr = pjsip_msg_find_hdr_by_name(rdata->msg_info.msg,
			&STR_P_ASSOCIATED_URI, pau_hdr ? pau_hdr->next : NULL))) {
		size_t i;

		if (!pau_hdr->hvalue.ptr || pau_hdr->hvalue.slen <= 0)
			continue;
		for (i = 0; i < (size_t) pau_hdr->hvalue.slen; i++) {
			size_t start, end;

			if (pau_hdr->hvalue.ptr[i] != '<')
				continue;
			start = i + 1;
			while (start < (size_t) pau_hdr->hvalue.slen
			       && (pau_hdr->hvalue.ptr[start] == ' '
				   || pau_hdr->hvalue.ptr[start] == '\t'))
				start++;
			for (end = start; end < (size_t) pau_hdr->hvalue.slen
			     && pau_hdr->hvalue.ptr[end] != '>'; end++)
				;
			if (end == (size_t) pau_hdr->hvalue.slen)
				break;
			while (end > start && (pau_hdr->hvalue.ptr[end - 1] == ' '
					       || pau_hdr->hvalue.ptr[end - 1] == '\t'))
				end--;
			if (end > start) {
				const char *uri = pau_hdr->hvalue.ptr + start;
				size_t len = end - start;
				const char *number = NULL;
				size_t number_len = 0;
				pj_bool_t sip = PJ_FALSE;

				if (!fallback) {
					fallback = uri;
					fallback_len = len;
				}
				if (volte_pau_get_dialable_number(uri, len, &sip,
						&number, &number_len)) {
					if (!dialable) {
						dialable = uri;
						dialable_len = len;
						dialable_number = number;
						dialable_number_len = number_len;
					}
					/* Prefer only the SIP form of that same primary number. A later
					 * secondary number must not silently replace the first one. */
					if (!dialable_sip && sip
					    && number_len == dialable_number_len
					    && !memcmp(number, dialable_number, number_len)) {
						dialable_sip = uri;
						dialable_sip_len = len;
					}
				}
			}
			i = end;
		}
	}

	selected = dialable_sip ? dialable_sip : (dialable ? dialable : fallback);
	selected_len = dialable_sip ? dialable_sip_len
		: (dialable ? dialable_len : fallback_len);
	if (!selected) {
		ast_log(LOG_ERROR, "Missing a value, enclosed by '<....>' in 'P-Associated-URI' in REGISTER response.");
		return -EINVAL;
	}

	if (selected_len < sizeof(transport_state->volte.p_associated_uri)) {
		memcpy(transport_state->volte.p_associated_uri, selected, selected_len);
		transport_state->volte.p_associated_uri[selected_len] = '\0';
	} else {
		ast_log(LOG_ERROR, "P-Associated-URI' too large.");
		return -EINVAL;
	}

	return PJ_SUCCESS;
}'''


def patch(source: str) -> str:
    if MARKER in source:
        return source
    matches = source.count(ORIGINAL_FN)
    if matches != 1:
        raise ValueError(f"expected one original P-Associated-URI function, found {matches}")
    return source.replace(ORIGINAL_FN, PATCHED_FN, 1)


def main() -> int:
    try:
        original = SOURCE.read_text()
        updated = patch(original)
    except (OSError, ValueError) as exc:
        print(f"preferred public identity patch failed: {exc}", file=sys.stderr)
        return 1

    if updated == original:
        print("preferred public identity already patched")
    else:
        SOURCE.write_text(updated)
        print("patched P-Associated-URI selection to prefer a registered dialable identity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
