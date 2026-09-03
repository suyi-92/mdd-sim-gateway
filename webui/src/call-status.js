// The browser sees the SIP response on its local Asterisk leg. For failed outbound VoWiFi
// calls that response is deliberately rewritten to 603 so JsSIP stops retrying; it is not the
// carrier verdict. The manager's call_result (DIALSTATUS + Q.850) is the authoritative status.
export const CALL_STATUS_LABEL = {
  answered: 'Answered', missed: 'Missed', rejected: 'Declined', busy: 'Busy',
  'no answer': 'No answer', cancelled: 'Cancelled', failed: 'Failed',
  ringing: 'Ringing', dialing: 'Dialing', unknown: 'Unknown',
  'code accepted': 'Carrier accepted', 'code unsupported': 'Not supported by carrier',
  'code rejected': 'Carrier refused', 'code failed': 'Carrier could not handle it',
}

export const SETTLED_CODE_STATUS = new Set([
  'code accepted', 'code unsupported', 'code rejected', 'code failed',
])

const SETTLED_CALL_STATUS = new Set([
  'answered', 'missed', 'rejected', 'busy', 'no answer', 'cancelled', 'failed',
])
const FAILED_CALL_STATUS = new Set(['missed', 'rejected', 'busy', 'no answer', 'failed'])

export const hasSettledCallStatus = (status) => SETTLED_CALL_STATUS.has(String(status || '').toLowerCase())

export function ordinaryCallEndLabel(status, sipCause, synthesizedDecline = false) {
  const normalized = String(status || '').toLowerCase()
  if (SETTLED_CALL_STATUS.has(normalized)) return CALL_STATUS_LABEL[normalized]
  // The outbound dialplan maps every non-answer to a local 603. Until call_result arrives,
  // say only what is known instead of briefly accusing the carrier of rejecting the call.
  if (synthesizedDecline && sipCause === 'Rejected') return 'Call ended'
  if (sipCause === 'Rejected') return 'Call declined'
  if (sipCause === 'Busy') return 'Busy'
  if (sipCause === 'Canceled' || sipCause === 'Canceled/Rejected') return 'Call cancelled'
  return 'Call ended'
}

export function ordinaryCallEndIsFailure(status, sipCause, synthesizedDecline = false) {
  const normalized = String(status || '').toLowerCase()
  if (SETTLED_CALL_STATUS.has(normalized)) return FAILED_CALL_STATUS.has(normalized)
  return !synthesizedDecline && (sipCause === 'Rejected' || sipCause === 'Busy')
}
