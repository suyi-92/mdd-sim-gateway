export function cellularRegistrationDetail(device, t = value => value) {
  const cellular = device?.cellular || {}
  const registration = String(cellular.registration || '').toLowerCase()
  if (!['home', 'roaming', 'registered'].includes(registration)) return ''
  const pieces = [registration === 'roaming' ? t('Roaming registered') : t('Home network registered')]
  const technology = String(cellular.access_technology || '').toUpperCase()
  if (technology) pieces.push(technology)
  if (cellular.operator) pieces.push(cellular.operator)
  if (cellular.signal != null) pieces.push(t('Signal {signal}%', { signal: cellular.signal }))
  pieces.push(cellular.data_active ? t('Data bearer connected') : t('No data bearer'))
  return pieces.join(' · ')
}
