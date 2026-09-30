// Store only opaque job IDs: never persist reader/card identities or activation data.
const STORAGE_KEY = 'mdd.esim.dismissed-downloads.v1'
const LIMIT = 64
let dismissed = []

function readDismissed() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]')
    if (Array.isArray(saved)) dismissed = [...new Set([...dismissed, ...saved
      .filter(id => typeof id === 'string' && id.length <= 128)])].slice(-LIMIT)
  } catch { /* Browser storage may be unavailable; retain this page session's choices. */ }
  return dismissed
}

export function isDownloadDismissed(operationId) {
  return !!operationId && readDismissed().includes(operationId)
}

export function canDismissDownload(download) {
  return !!download?.operationId && !!(download.done || download.error)
    && (['started', 'not_needed', 'failed', 'cancelled'].includes(download.lineRecovery)
      // Older failed jobs predate the recovery field. They must remain dismissible.
      || (!!download.error && !download.lineRecovery))
}

export function shouldAutoDismissDownload(download) {
  return canDismissDownload(download) && !!download.done && !download.error
    && ['started', 'not_needed'].includes(download.lineRecovery)
}

export function dismissDownload(download) {
  if (!canDismissDownload(download)) return false
  dismissed = [...readDismissed().filter(id => id !== download.operationId), download.operationId].slice(-LIMIT)
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(dismissed)) } catch { /* In-memory fallback. */ }
  return true
}

// Terminal snapshots win over delayed WebSocket progress. A settled, dismissed job
// cannot reappear because of a late event; a different job remains independent.
export function acceptDownloadEvent(current, message) {
  const id = message.operation_id
  if (!id || isDownloadDismissed(id)) return false
  return current?.operationId !== id || !(current.done || current.error)
}
