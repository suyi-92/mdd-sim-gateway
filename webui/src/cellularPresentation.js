export function networkName(network = {}, language = 'zh') {
  return (language === 'zh' && network.name_zh) || network.name || network.operator_id || ''
}

export function networkLabel(network = {}, language = 'zh') {
  const name = networkName(network, language)
  return network.operator_id && name !== network.operator_id ? `${name} (${network.operator_id})` : name
}

export function currentCellularNetwork(device = {}) {
  const cellular = device.cellular || {}
  return { operator_id: cellular.operator_code || '', name: cellular.operator || '',
    name_zh: cellular.operator_zh || '', observed_at: Number(cellular.observed_at) || 0,
    connected: device.present !== false && device.sim?.present !== false
      && ['home', 'roaming', 'registered'].includes(cellular.registration)
      && /^[0-9]{5,6}$/.test(cellular.operator_code || '') }
}

export function networkAvailability(network, current) {
  if (current.connected && network.operator_id === current.operator_id) return 'current'
  return network.status === 'current' ? 'available' : network.status
}

// Operation results are historical. Only a newer host observation can establish
// the current connection or resolve a scan's former registration-recovery warning.
export function cellularOperationOutcome(operation, current, networks, language, t) {
  if (!operation) return null
  const fresh = !operation.finished_at || current.observed_at >= operation.finished_at
  const lateRegistration = operation.action === 'apply' && operation.state === 'failed'
    && ['operation_timeout', 'network_timeout'].includes(operation.error?.code)
    && Number(operation.finished_at || 0) > 0
    && current.observed_at > operation.finished_at && current.connected
  if (lateRegistration) {
    const requestedCode = operation.selection?.operator_id || ''
    const requested = networks.find(item => item.operator_id === requestedCode)
      || { operator_id: requestedCode }
    const requestedLabel = networkLabel(requested, language) || t('Automatic network selection')
    const currentLabel = networkLabel(current, language)
    const matches = operation.selection?.mode === 'automatic'
      || (operation.selection?.mode === 'manual' && requestedCode === current.operator_id)
    return matches
      ? { tone: 'info', resolved: true, text: t(
        'The network request timed out, but a newer modem sample confirms registration on {network}. The requested selection mode was not confirmed.',
        { network: currentLabel }) }
      : { tone: 'warning', resolved: true, text: t(
        'The network request timed out. Service has recovered on {current}, but the requested network {network} was not confirmed.',
        { current: currentLabel, network: requestedLabel }) }
  }
  if (operation.action === 'apply' && operation.state === 'success') {
    const code = operation.result?.registration?.operator_id || operation.selection?.operator_id
    const target = networks.find(item => item.operator_id === code)
      || (current.operator_id === code ? current : { operator_id: code })
    const network = networkLabel(target, language) || t('Automatic network selection')
    if (!fresh) return { tone: 'info', text: t('Last selection completed: {network}. Updating current registration…', { network }) }
    if (current.connected && current.operator_id === code) {
      return { tone: 'info', text: t('Currently registered on {network}.', { network: networkLabel(current, language) }) }
    }
    return { tone: 'warning', text: t(current.connected
      ? 'Last selection: {network}. Current network: {current}.'
      : 'Last selection completed: {network}. Currently not registered on a cellular network.',
    { network, current: networkLabel(current, language) }) }
  }
  if (operation.action === 'scan' && operation.state === 'partial') {
    const recovery = operation.error?.recovery || {}
    if (!fresh) return { tone: 'info', text: t('Scan complete: {count} networks. Updating current registration…',
      { count: networks.length }) }
    const matches = recovery.mode === 'automatic'
      || (recovery.mode === 'manual' && recovery.operator_id === current.operator_id)
    if (fresh && current.connected && matches) return { tone: 'info', text: t(
      'Scan complete: {count} networks. Registration has now recovered on {network}.',
      { count: networks.length, network: networkLabel(current, language) }) }
    const prefix = t('Scan complete: {count} networks.', { count: networks.length })
    const detail = fresh && current.connected ? t('The original selection was not restored. Current network: {network}.',
      { network: networkLabel(current, language) })
      : t(recovery.state === 'pending' ? 'Restoring cellular registration is still in progress.'
        : 'The modem could not restore registration after scanning. Choose Automatic, then Apply network to reconnect.')
    return { tone: 'warning', text: `${prefix} ${detail}` }
  }
  return null
}

// A durable eSIM recovery result is historical once it reaches a terminal state.  Resolve its
// presentation only from a strictly newer, same-device, same-profile observation that proves
// every layer needed by the profile card: current VPCD channels, baseband identity and PLMN
// registration.  The stored task itself remains failed for auditability.
export function esimRecoveryOutcome(status, device = {}, profile = {}, card = {}, language = 'zh', t) {
  if (status?.state !== 'failed' || !Number(status.finished_at || 0)) return null
  if (!status.id || status.id !== device.esim_recovery?.id
      || status.device_id !== device.id || card.hardware_id !== device.id) return null
  if (card.present !== true || !card.iccid || card.iccid !== profile.iccid
      || String(profile.profileState || '').toLowerCase() !== 'enabled') return null
  const channels = device.logical_channels || {}
  const bridgeReady = channels.status === 'ready'
    && Number(channels.capacity || 0) > 0
    && Number(channels.allocated || 0) === Number(channels.capacity || 0)
  if (!bridgeReady || device.cellular_recovery?.state !== 'ready'
      || device.sim?.present !== true) return null
  const current = currentCellularNetwork(device)
  if (!current.connected || current.observed_at <= Number(status.finished_at)) return null
  return {
    tone: 'info', resolved: true,
    text: t(
      'The earlier cellular recovery ended with {code}, but newer state confirms that this enabled eSIM now has a ready SIM bridge and is registered on {network}. The original recovery attempt remains recorded as failed.',
      { code: status.error_code || t('unknown error'), network: networkLabel(current, language) },
    ),
  }
}

export function cellularOperationProgress(operation, t) {
  const stages = {
    queued: 'Preparing network selection…',
    registering: 'Requesting network registration…',
    confirming: 'Confirming cellular registration…',
    restoring: 'Registration was not confirmed; restoring the previous selection…',
  }
  const phase = t(stages[operation.phase] || 'Waiting for the modem to confirm registration…')
  return Number.isFinite(operation.remaining_seconds)
    ? `${phase} ${t('At most {seconds}s remaining.', { seconds: operation.remaining_seconds })}` : phase
}

export function cellularRegistrationDetail(device, t = value => value, language = 'zh') {
  const cellular = device?.cellular || {}
  const registration = String(cellular.registration || '').toLowerCase()
  if (!['home', 'roaming', 'registered'].includes(registration)) {
    return cellularNetworkRejectDetail(device, t, language)
  }
  const pieces = [registration === 'roaming' ? t('Roaming registered') : t('Home network registered')]
  const technology = String(cellular.access_technology || '').toUpperCase()
  if (technology) pieces.push(technology)
  const operator = networkName({ name: cellular.operator, name_zh: cellular.operator_zh }, language)
  if (operator) pieces.push(operator)
  if (cellular.signal != null) pieces.push(t('Signal {signal}%', { signal: cellular.signal }))
  pieces.push(cellular.data_active ? t('Data bearer connected') : t('No data bearer'))
  return pieces.join(' · ')
}

export function cellularNetworkRejectDetail(device, t = value => value, language = 'zh') {
  const rejection = device?.cellular?.network_reject || device?.network_reject || {}
  if (!rejection.observed_at) return ''
  const rat = String(rejection.rat || '').toUpperCase()
  const domain = String(rejection.service_domain || '').toUpperCase()
  const operator = /^[0-9]{5,6}$/.test(rejection.operator_id || '')
    ? rejection.operator_id : t('Unknown network')
  const cause = Number(rejection.cause_code) === 7
    ? t('EPS services not allowed (cause 7)')
    : rejection.cause_code != null
      ? t('Network rejection cause {code}', { code: rejection.cause_code })
      : t('Unknown network rejection cause')
  return [t('Cellular service rejected by the network'), rat, domain, cause,
    t('Access network: {network}', { network: operator })].filter(Boolean).join(' · ')
}
