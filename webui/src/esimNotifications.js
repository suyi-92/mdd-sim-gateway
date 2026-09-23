import { isEsimRecoverySuperseded } from './cellularPresentation.js'

export function newerNotificationStatus(current, incoming) {
  if (!incoming) return current
  return Number(current?.updated_at || 0) > Number(incoming.updated_at || 0) ? current : incoming
}

// Cache reads never touch the eUICC. Merge only outcomes for an already displayed
// eUICC/SE/profile; keep the live notification list and local operation drafts.
export function mergeNotificationSnapshot(ses, snapshot) {
  if (!snapshot?.cached) return ses
  return ses.map(se => {
    const fresh = snapshot.ses?.find(item => item.id === se.id && item.eid && item.eid === se.eid)
    if (!fresh) return se
    return { ...se, profiles: (se.profiles || []).map(profile => {
      const incoming = fresh.profiles?.find(item => item.iccid === profile.iccid)
      if (!incoming) return profile
      const current = profile.notification_status
      let status = incoming.notification_status
      if (!status && Number(current?.updated_at) > 0
          && Number(snapshot.ts) >= Number(current.updated_at)) {
        status = { state: 'empty', updated_at: snapshot.ts }
      }
      return { ...profile, notification_status: newerNotificationStatus(current, status) }
    }) }
  })
}

export function notificationReaderFailureRecovered(profile, device, card) {
  const status = profile.notification_status
  const recovery = profile.recovery_status
  // No notification attempt ran: the failure was inherited from bridge recovery.
  // Stop calling a now-proven reader unavailable, but retain the unverified pending
  // work in the Notifications panel. Actual delivery failures stay visible.
  return status?.state === 'failed' && status.reason_code === 'reader_unavailable'
    && status.attempts === 0 && status.elapsed_ms === 0
    && Number(status.updated_at) >= Number(recovery?.finished_at)
    && Number(status.updated_at) < Number(device?.cellular?.observed_at)
    && isEsimRecoverySuperseded(recovery, device, profile, card)
}
