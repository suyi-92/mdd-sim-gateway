const steps = new Set([
  'euicc_init', 'es10c_enable_profile', 'es10c_disable_profile', 'es10c_delete_profile',
  'es10c_set_nickname', 'es10c_get_profiles_info',
])

// Detailed errors are already reduced to a closed schema by Control. Do not render
// arbitrary data/detail objects, APDU payloads or unknown diagnostic fields.
export function esimErrorMessage(error, t = (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? `{${key}}`)) {
  const detail = error?.data?.detail
  const diagnostic = detail?.diagnostic
  const message = t(detail?.message || error?.message || 'The eSIM operation failed.')
  if (!diagnostic || typeof diagnostic !== 'object') return message
  const parts = []
  if (steps.has(diagnostic.step)) parts.push(t('Step: {value}', { value: diagnostic.step }))
  if (Number.isInteger(diagnostic.lpac_code) && Math.abs(diagnostic.lpac_code) <= 65535) {
    parts.push(t('Helper code: {value}', { value: diagnostic.lpac_code }))
  }
  if ([1, 2, 3, 4].includes(diagnostic.card_result)) {
    parts.push(t('Card result: {value}', { value: diagnostic.card_result }))
  }
  if (/^801000[0-9A-F]{2}$/.test(diagnostic.pcsc_code || '')) parts.push(`PC/SC ${diagnostic.pcsc_code}`)
  if (/^[69][0-9A-F]{3}$/.test(diagnostic.status_word || '')) parts.push(`SW ${diagnostic.status_word}`)
  return parts.length ? `${message} (${parts.join('; ')})` : message
}

export function profileOperationFeedback(status, t) {
  if (status?.state !== 'failed' || !status.error) return ''
  return t('Last operation failed: {error}', {
    error: esimErrorMessage({ data: { detail: status.error } }, t),
  })
}
