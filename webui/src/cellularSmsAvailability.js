// Presence alone does not prove that the modem's message store can be read.
// Mobile data and network registration are deliberately not prerequisites.
export function retainedSmsAvailability(device, stale = false) {
  if (stale) return { available: false, reason: 'Device status is out of date. Waiting for a fresh update.' }
  if (!device) return { available: false, reason: 'This line does not have an available cellular modem.' }
  const flight = device.capabilities?.flight
  if (device.capabilities?.cellular?.actual === 'unsupported') {
    return { available: false, reason: 'Retained modem SMS is unavailable in VoWiFi-only mode.' }
  }
  if (device.shared?.transitioning) {
    return { available: false, reason: 'The modem is changing state. Wait before importing retained SMS.' }
  }
  if (device.shared?.modemmanager_active === false) {
    return { available: false, reason: flight?.desired || flight?.actual === 'on'
      ? 'Retained modem SMS cannot be read while flight mode has stopped the cellular service. Turn off flight mode in Devices and wait for the SIM to be ready; mobile data can remain off.'
      : 'The cellular service is unavailable. Check the modem status in Devices before retrying.' }
  }
  // New servers explicitly return null while the SIM/MM identity is unconfirmed.
  // An older server may omit this field; the API remains the authoritative gate.
  if (device.cellular === null || device.sim?.present === false) {
    return { available: false, reason: 'The cellular SIM is not ready. Wait for the current SIM to be detected in Devices.' }
  }
  return { available: true, reason: '' }
}

export function retainedSmsError(error, tr) {
  const message = error?.data?.detail?.message || error?.message || ''
  const known = {
    'ModemManager is unavailable.': 'The cellular service is unavailable. Check the modem status in Devices before retrying.',
    'No cellular modem is available.': 'This line does not have an available cellular modem.',
    "Could not find the line's SIM among the readable cellular modems.": 'The cellular SIM is not ready. Wait for the current SIM to be detected in Devices.',
    "No cellular modem matches this line's ICCID or IMSI.": 'The cellular SIM is not ready. Wait for the current SIM to be detected in Devices.',
    'Timed out while listing cellular modems.': 'The cellular service did not respond in time. Retry after checking Devices.',
  }
  return tr(known[message] || message)
}
