import { boundedRead } from './pollRequest.js'

// HTTP snapshots and WebSocket events can arrive out of order. Task creation time
// orders different switches; updated_at orders progress within the same switch.
export function newerEsimRecovery(current, incoming) {
  if (!incoming) return current
  if (!current) return incoming
  const field = current.id && incoming.id && current.id !== incoming.id
    ? 'created_at' : 'updated_at'
  if (Number(current[field]) > Number(incoming[field])) return current
  return incoming
}

// The regular device poll is also a recovery snapshot. Match the cached task, device
// and current enabled card before using it; a modem's latest task may belong to a
// different profile. No eUICC read or line interruption is needed to catch up.
export function profileRecoveryStatus(profile, device, card) {
  if (String(profile.profileState || '').toLowerCase() !== 'enabled') return null
  const cached = profile.recovery_status
  const live = device?.esim_recovery
  if (!cached?.id || live?.id !== cached.id
      || live.device_id !== device?.id || cached.device_id !== device?.id
      || card?.hardware_id !== device?.id || card?.present !== true
      || !card.iccid || card.iccid !== profile.iccid) return cached
  return newerEsimRecovery(cached, live)
}

// Starting a container does not prove its PIN reader or IMS registration recovered.
export async function waitForEsimLine(api, id, timeoutMs = 90000) {
  const deadline = performance.now() + timeoutMs
  while (performance.now() < deadline) {
    try {
      const status = await boundedRead(signal => api.status(id, signal),
        Math.min(5000, deadline - performance.now()))
      if (status?.state === 'OK') return true
    } catch { /* Transient status requests may fail while the line is starting. */ }
    const remaining = deadline - performance.now()
    if (remaining <= 0) break
    await new Promise(resolve => setTimeout(resolve, Math.min(1000, remaining)))
  }
  return false
}
