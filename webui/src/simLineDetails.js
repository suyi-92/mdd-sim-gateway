const clean = value => String(value || '').trim()

const distinct = values => values.reduce((result, value) => {
  const text = clean(value)
  if (text && !result.some(item => item.toLocaleLowerCase() === text.toLocaleLowerCase())) result.push(text)
  return result
}, [])

export function lineDisplayName(line = {}, t = value => value) {
  return clean(line.name)
    || [line.mcc, line.mnc].map(clean).filter(Boolean).join('-')
    || t('Line {number}', { number: line.id || '?' })
}

export function maskedLineNumber(value, t = text => text) {
  const digits = clean(value).replace(/\D/g, '')
  return digits ? `••••${digits.slice(-4)}` : t('Number unavailable')
}

export function countryDisplayName(value, language = 'en', t = text => text) {
  const code = clean(value).toLowerCase()
  if (!/^[a-z]{2}$/.test(code)) return t('Unknown')
  try {
    const locale = language === 'zh' ? 'zh-CN' : 'en'
    return `${new Intl.DisplayNames([locale], { type: 'region' }).of(code.toUpperCase())} (${code.toUpperCase()})`
  } catch {
    return code.toUpperCase()
  }
}

export function communicationLineDetails(line = {}, device = {}, t = value => value, language = 'en') {
  const described = device?.sim?.carrier
  const carrier = described && typeof described === 'object' ? described : {}
  const plmn = clean(carrier.plmn)
    || [line.mcc, line.mnc].map(clean).filter(Boolean).join('-')
  const configuredCarrier = typeof line.carrier === 'string' ? clean(line.carrier) : ''
  const carrierName = clean(carrier.name) || configuredCarrier || t('Unknown carrier')
  const carrierLabel = plmn && !carrierName.includes(plmn)
    ? `${carrierName} (${plmn})`
    : carrierName
  const networkNames = distinct([carrier.current_network, carrier.home_network])
  const network = networkNames.join(' · ') || plmn || t('Unknown network')
  const route = device?.egress || line.egress || {}
  const countryCode = clean(route.detected_country || route.country || line.proxy_country_effective)
  let networkRoute = clean(route.node)
  if (!networkRoute && ['direct', 'disabled', 'legacy'].includes(clean(route.mode).toLowerCase())) {
    networkRoute = route.mode === 'direct' ? t('Explicit direct connection') : t('Host network')
  }
  if (!networkRoute && route.ready) networkRoute = t('Country exit')

  return {
    carrier: carrierLabel,
    line: lineDisplayName(line, t),
    number: maskedLineNumber(line.msisdn || device?.sim?.number, t),
    country: countryDisplayName(countryCode, language, t),
    network,
    networkRoute: networkRoute || t('Not connected'),
  }
}
