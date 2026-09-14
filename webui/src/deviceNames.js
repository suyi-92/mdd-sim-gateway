const SCR_PRIME = /SCR[\s_-]*Prime/i
const VPCD_MODEM = /^VoWiFi Modem\b/i

const translate = (t, value) => (typeof t === 'function' ? t(value) : value)

export function defaultDeviceName(device = {}, index = 0, t) {
  const declared = String(device.default_name || '').trim()
  if (declared) return translate(t, declared)

  const raw = String(device.hardware_name || device.modem_name || device.name || '').trim()
  if (SCR_PRIME.test(raw)) return translate(t, '3T Electronics SCR Prime reader')
  if (VPCD_MODEM.test(raw)) return translate(t, 'Cellular modem')
  if (raw) return translate(t, raw)

  const fallback = String(device.label || device.model || '').trim()
  return fallback || `${translate(t, 'Device')} ${index + 1}`
}

export function deviceTitle(device = {}, index = 0, t) {
  const custom = String(device.display_name || '').trim()
  return custom || defaultDeviceName(device, index, t)
}
