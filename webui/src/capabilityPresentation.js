const ON_DETAILS = {
  cellular: 'Working — connected to the carrier over the cellular network.',
  vowifi: 'Working — connected to the carrier over Wi-Fi.',
  flight: 'Flight mode is active; the cellular radio is disabled.',
}
const OFF_DETAILS = {
  cellular: 'Mobile data is disconnected; the modem radio can remain registered to the cellular network.',
  flight: 'Flight mode is off; the cellular radio is available.',
  vowifi: 'VoWiFi is disabled.',
}

// Device snapshots describe the capability; status events describe its engine.
// Stable capability states need one explanation across both feeds. Raw engine
// reasons remain available in the background-status panel and for actual faults.
export function capabilityDetail(kind, capability, device, t = value => value) {
  const healthy = capability.actual === 'on'
  const disabled = capability.actual === 'off' && !capability.desired
  const canonical = healthy ? ON_DETAILS[kind] : disabled ? OFF_DETAILS[kind] : ''
  if (canonical && capability.available !== false && device.present !== false) return t(canonical)
  if (capability.reason) return t(capability.reason)
  const fallback = capability.actual === 'on' ? ON_DETAILS[kind]
    : capability.actual === 'off' ? OFF_DETAILS[kind] : ''
  return t(fallback || `cap.help.${capability.actual}`)
}
