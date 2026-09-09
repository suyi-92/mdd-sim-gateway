// Thin REST + WebSocket client for the manager API (same origin).
import { boundedRead } from './pollRequest.js'
const base = ''
let csrfToken = ''
const clientEvents = []
let clientSequence = 0, flushingEvents = false
const failedPolls = new Set()

function clientEvent(scope, outcome, statusCode, elapsedMs) {
  clientEvents.push({ scope, outcome, status_code: Number(statusCode) || 0,
    elapsed_ms: Math.round(elapsedMs), client_epoch: Math.floor(Date.now() / 1000),
    sequence: ++clientSequence })
  if (clientEvents.length > 20) clientEvents.splice(10, 1) // Keep the first failure and recent outcomes.
}

async function flushClientEvents() {
  if (flushingEvents || !clientEvents.length || !csrfToken) return
  flushingEvents = true
  const batch = clientEvents.slice()
  try {
    const response = await boundedRead(signal => fetch('/api/diagnostics/client-events', {
      method: 'POST', signal, headers: { 'Content-Type': 'application/json', 'X-MDD-CSRF-Token': csrfToken },
      body: JSON.stringify({ events: batch }),
    }), 5000)
    if (response.ok) {
      const acknowledged = batch[batch.length - 1].sequence
      while (clientEvents.length && clientEvents[0].sequence <= acknowledged) clientEvents.shift()
    }
  } catch { /* Keep at most 20 closed-schema records until connectivity returns. */ }
  finally { flushingEvents = false }
}

async function poll(scope, path) {
  const started = performance.now()
  try {
    const result = await boundedRead(signal => j('GET', path, undefined, signal))
    if (['devices', 'cards', 'instances'].includes(scope)
        && !Array.isArray(result?.[scope]) && !(scope === 'devices' && Array.isArray(result))) {
      throw Object.assign(new Error('Invalid polling response'), { code: 'invalid_response' })
    }
    if (failedPolls.delete(scope)) clientEvent(scope, 'recovered', 200, performance.now() - started)
    void flushClientEvents()
    return result
  } catch (error) {
    failedPolls.add(scope)
    clientEvent(scope, ['timeout', 'invalid_response'].includes(error.code) ? error.code : error.status ? 'http' : 'network',
      error.status, performance.now() - started)
    throw error
  }
}

export function setCsrf(token) { csrfToken = token || '' }

async function transfer(path, file) {
  const opt = file ? { method: 'POST', body: file, headers: {
    'Content-Type': 'application/octet-stream', 'X-MDD-CSRF-Token': csrfToken,
  } } : { cache: 'no-store' }
  const response = await fetch(base + path, opt)
  if (!response.ok) {
    if (response.status === 401 && csrfToken) {
      csrfToken = ''
      window.dispatchEvent(new CustomEvent('mdd-auth-expired'))
    }
    const data = await response.json().catch(() => ({}))
    throw new Error(typeof data.detail === 'string' ? data.detail : 'backup.transfer.failed')
  }
  return file ? response.json() : response.blob()
}

async function j(method, path, body, signal) {
  const requestCsrf = csrfToken
  const opt = { method, headers: {}, ...(signal ? { signal } : {}) }
  if (csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method)) opt.headers['X-MDD-CSRF-Token'] = csrfToken
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body) }
  const r = await fetch(base + path, opt)
  const text = await r.text()
  let data
  try { data = text ? JSON.parse(text) : {} } catch { data = { raw: text } }
  // A non-empty CSRF token means this tab previously had an authenticated session.
  // Notify the app once if the persisted session has expired or been revoked.
  if (r.status === 401 && csrfToken && requestCsrf === csrfToken) {
    csrfToken = ''
    window.dispatchEvent(new CustomEvent('mdd-auth-expired'))
  }
  // detail may be a structured dict (e.g. {code, message}); prefer its message so
  // alerts show readable text instead of "[object Object]".
  const detailMsg = data.detail && typeof data.detail === 'object' ? (data.detail.message || data.detail.code) : data.detail
  if (!r.ok) throw Object.assign(new Error(detailMsg || data.error || r.statusText), { status: r.status, data })
  return data
}

/** Build query string. Prefer reader NAME (stable); index is optional fallback. */
function readerQuery(readerOrIndex, maybeName) {
  const q = new URLSearchParams()
  if (typeof readerOrIndex === 'string' && readerOrIndex) {
    q.set('reader', readerOrIndex)
  } else if (typeof readerOrIndex === 'number') {
    q.set('reader_index', String(readerOrIndex))
    if (maybeName) q.set('reader', maybeName)
  } else if (maybeName) {
    q.set('reader', maybeName)
  } else {
    q.set('reader_index', '0')
  }
  return q
}

function readerBody(readerOrIndex, extra = {}) {
  if (typeof readerOrIndex === 'string' && readerOrIndex) {
    return { reader: readerOrIndex, ...extra }
  }
  if (typeof readerOrIndex === 'number') {
    return { reader_index: readerOrIndex, ...extra }
  }
  if (readerOrIndex && typeof readerOrIndex === 'object') {
    return { ...readerOrIndex, ...extra }
  }
  return { reader_index: 0, ...extra }
}

export const api = {
  authStatus: () => j('GET', '/api/auth/status'),
  authSetup: (username, password, remember) => j('POST', '/api/auth/setup', { username, password, remember }),
  authLogin: (username, password, remember) => j('POST', '/api/auth/login', { username, password, remember }),
  authLogout: () => j('POST', '/api/auth/logout', {}),
  authPassword: (current_password, new_password) => j('POST', '/api/auth/password', { current_password, new_password }),
  // Unified physical-device control plane. Older deployments may return 404;
  // App.jsx then derives read-only device cards from /api/instances + /api/cards.
  devices: () => poll('devices', '/api/devices'),
  patchDeviceCapabilities: (id, patch) => j('PATCH', `/api/devices/${encodeURIComponent(id)}/capabilities`, patch),
  deviceCellular: (id) => j('GET', `/api/devices/${encodeURIComponent(id)}/cellular`),
  deviceDiagnostics: (id) => j('POST', `/api/devices/${encodeURIComponent(id)}/diagnostics`, {}),
  saveDeviceHardware: (id, patch) => j('PUT', `/api/devices/${encodeURIComponent(id)}/hardware`, patch),
  deleteDevice: (id) => j('DELETE', `/api/devices/${encodeURIComponent(id)}`),
  readers: () => j('GET', '/api/readers'),
  detect: (i = 0) => j('GET', `/api/sim/detect?reader_index=${i}`),
  // `reader` (PC/SC reader NAME) lets the backend re-resolve the index at request time —
  // indices shift when another reader is unplugged, and a stale index could address the
  // wrong physical SIM.
  verifyPin: (pin, reader_index = 0, reader, reader_port) => j('POST', '/api/sim/verify-pin', { pin, reader_index, reader, reader_port }),
  changePin: (oldp, newp, reader_index = 0, reader, reader_port) => j('POST', '/api/sim/change-pin', { old: oldp, new: newp, reader_index, reader, reader_port }),
  setPinEnabled: (pin, enabled, reader_index = 0, reader, reader_port) => j('POST', '/api/sim/pin-enabled', { pin, enabled, reader_index, reader, reader_port }),

  settings: () => j('GET', '/api/settings'),
  saveSettings: (patch) => j('PUT', '/api/settings', patch),
  egressStatus: () => j('GET', '/api/egress/status'),
  testEgress: (country) => j('POST', `/api/egress/${encodeURIComponent(country)}/test`, {}),
  testProxyProfile: (profileId, profile) => j('POST', `/api/egress/profile/${encodeURIComponent(profileId)}/test`, profile || {}),
  refreshEgress: () => j('POST', '/api/egress/refresh', {}),
  testWebhook: (config) => j('POST', '/api/notifications/webhook/test', config || {}),
  testTelegram: (config) => j('POST', '/api/notifications/telegram/test', config || {}),
  testPushPlus: (config) => j('POST', '/api/notifications/pushplus/test', config || {}),
  testFeishu: (config) => j('POST', '/api/notifications/feishu/test', config || {}),
  notificationDeliveries: (limit = 100) => j('GET', `/api/notifications/deliveries?limit=${limit}`),
  clearNotificationDeliveries: () => j('DELETE', '/api/notifications/deliveries'),
  systemStatus: () => poll('system', '/api/system/status'),
  backups: () => j('GET', '/api/system/backups'),
  backupOperation: () => j('GET', '/api/system/backups/operation'),
  createBackup: () => j('POST', '/api/system/backups', {}),
  exportBackup: name => transfer(`/api/system/backups/${encodeURIComponent(name)}/export`),
  importBackup: file => transfer('/api/system/backups/import', file),
  restoreBackup: (name) => j(
    'POST', `/api/system/backups/${encodeURIComponent(name)}/restore`, { confirm: 'RESTORE' }),
  clearHostAlerts: () => j('DELETE', '/api/system/host-alerts'),
  maintenance: (action) => j('POST', '/api/system/maintenance', { action }),
  restartProgress: () => j('GET', '/api/system/maintenance/restart-progress'),
  supportBundleUrl: '/api/diagnostics/support-bundle',

  instances: () => poll('instances', '/api/instances'),
  cards: () => poll('cards', '/api/cards'),
  portsSuggest: () => j('GET', '/api/ports/suggest'),
  provision: (body) => j('POST', '/api/provision', body),
  saveInstance: (inst) => j('POST', '/api/instances', inst),
  setLineCountry: (id, country) => j('PUT', `/api/instances/${id}/country`, { country }),
  deleteInstance: (id, deleteHistory = true) => j('DELETE', `/api/instances/${id}?delete_history=${deleteHistory ? 'true' : 'false'}&confirm_id=${encodeURIComponent(id)}`),
  start: (id, body) => j('POST', `/api/instances/${id}/start`, body || {}),
  stop: (id) => j('POST', `/api/instances/${id}/stop`),
  reprovision: (id, body) => j('POST', `/api/instances/${id}/reprovision`, body || {}),
  clearPin: (id) => j('POST', `/api/instances/${id}/pin/clear`),
  status: (id) => j('GET', `/api/instances/${id}/status`),
  // Recorded VoWiFi up/down timeline; the window follows the accumulated history (max 2 days).
  lineAvailability: (id) => poll('availability', `/api/instances/${id}/availability`),
  logs: (id, tail = 300) => j('GET', `/api/instances/${id}/logs?tail=${tail}`),
  register: (id) => j('POST', `/api/instances/${id}/register`),

  threads: (id) => j('GET', `/api/instances/${id}/messages/threads`),
  messages: (id, peer) => j('GET', `/api/instances/${id}/messages/${encodeURIComponent(peer)}`),
  // Payloads that were filed instead of shown: binary / SIM-addressed SMS. Kept reachable so a
  // misclassified real text cannot vanish silently.
  binarySms: (id) => j('GET', `/api/instances/${id}/messages/binary`),
  sendSms: (id, to, body, transport = 'auto') => j(
    'POST',
    `/api/instances/${id}/sms/send`,
    { to, body, transport },
  ),
  allowance: (id) => j('GET', `/api/instances/${id}/allowance`),
  saveAllowance: (id, body) => j('PUT', `/api/instances/${id}/allowance`, body),
  allowanceQueryRule: (id) => j('GET', `/api/instances/${id}/allowance/query-rule`),
  saveAllowanceQueryRule: (id, body) => j('PUT', `/api/instances/${id}/allowance/query-rule`, body),
  resetAllowanceQueryRule: (id) => j('DELETE', `/api/instances/${id}/allowance/query-rule`),
  queryAllowance: (id, transport = 'auto') => j(
    'POST', `/api/instances/${id}/allowance/query`, { transport }),
  // Number keeping. Config is stored server-side rather than in the line config, so saving it
  // never restarts a running engine.
  keepalive: (id) => j('GET', `/api/instances/${id}/keepalive`),
  saveKeepalive: (id, body) => j('PUT', `/api/instances/${id}/keepalive`, body),
  keepaliveSummary: () => j('GET', '/api/keepalive/summary'),
  runKeepalive: (id) => j('POST', `/api/instances/${id}/keepalive/run`),
  // delete messages: { ids:[...] } | { peer } (whole conversation) | { all:true }
  deleteMessages: (id, sel) => j('POST', `/api/instances/${id}/messages/delete`, sel),

  voicemails: (id) => j('GET', `/api/instances/${id}/voicemails`),
  // Served as audio/wav by the control plane; the <audio> element fetches it directly
  // and the session cookie rides along same-origin, so it never goes through j().
  voicemailAudioUrl: (id, vid) => `/api/instances/${id}/voicemails/${vid}/audio`,
  markVoicemailListened: (id, vid) => j('POST', `/api/instances/${id}/voicemails/${vid}/listened`),
  deleteVoicemails: (id, sel) => j('POST', `/api/instances/${id}/voicemails/delete`, sel),
  calls: (id) => j('GET', `/api/instances/${id}/calls`),
  // delete call-log entries: { ids:[...] } | { all:true }
  deleteCalls: (id, sel) => j('POST', `/api/instances/${id}/calls/delete`, sel),
  call: (id, to, from_endpoint = 'webrtc') => j('POST', `/api/instances/${id}/call`, { to, from_endpoint }),
  hangup: (id) => j('POST', `/api/instances/${id}/hangup`),
  cellularCall: (id, to) => j('POST', `/api/instances/${id}/cellular-call`, { to }),
  cellularCallStatus: (id) => j('GET', `/api/instances/${id}/cellular-call/status`),
  cellularCallHangup: (id) => j('POST', `/api/instances/${id}/cellular-call/hangup`, {}),
  softphone: (id) => j('GET', `/api/instances/${id}/softphone`),

  // eSIM / LPA (lpac) — first arg is usually the PC/SC reader NAME (string).
  // Optional se_id / aid target a specific Secure Element on dual-SE cards.
  esimStatus: () => j('GET', '/api/esim/status'),
  esimChip: (readerOrIndex, maybeName) => j('GET', `/api/esim/chip?${readerQuery(readerOrIndex, maybeName)}`),
  esimChipCached: (readerOrIndex, maybeName) => j('GET', `/api/esim/chip/cached?${readerQuery(readerOrIndex, maybeName)}`),
  esimProfiles: (readerOrIndex, maybeName) => j('GET', `/api/esim/profiles?${readerQuery(readerOrIndex, maybeName)}`),
  esimEnable: (iccid, readerOrBody) => j(
    'POST',
    `/api/esim/profiles/${encodeURIComponent(iccid)}/enable`,
    readerBody(readerOrBody),
  ),
  esimDisable: (iccid, readerOrBody) => j(
    'POST',
    `/api/esim/profiles/${encodeURIComponent(iccid)}/disable`,
    readerBody(readerOrBody),
  ),
  esimDelete: (iccid, readerOrBody) => {
    if (readerOrBody && typeof readerOrBody === 'object') {
      const q = readerQuery(readerOrBody.reader ?? readerOrBody.reader_index)
      if (readerOrBody.se_id || readerOrBody.seId) q.set('se_id', readerOrBody.se_id || readerOrBody.seId)
      if (readerOrBody.aid) q.set('aid', readerOrBody.aid)
      return j('DELETE', `/api/esim/profiles/${encodeURIComponent(iccid)}?${q}`)
    }
    return j(
      'DELETE',
      `/api/esim/profiles/${encodeURIComponent(iccid)}?${readerQuery(readerOrBody)}`,
    )
  },
  esimNickname: (iccid, nickname, readerOrBody) => j(
    'POST',
    `/api/esim/profiles/${encodeURIComponent(iccid)}/nickname`,
    readerBody(readerOrBody, { nickname }),
  ),
  esimDownload: (body) => j('POST', '/api/esim/download', body),
  esimDownloadCancel: (readerOrBody) => j('POST', '/api/esim/download/cancel', readerBody(readerOrBody)),
  esimDiscovery: (body) => j('POST', '/api/esim/discovery', body || {}),
  esimNotifications: (readerOrIndex, maybeName) => j(
    'GET',
    `/api/esim/notifications?${readerQuery(readerOrIndex, maybeName)}`,
  ),
  // Aliases used by Esim.jsx
  esimProcessNotifications: (readerOrIndex, seq) => j(
    'POST',
    '/api/esim/notifications/process',
    readerBody(readerOrIndex, seq == null ? {} : { seq }),
  ),
  esimNotificationsProcess: (body) => j('POST', '/api/esim/notifications/process', body || {}),
  esimRemoveNotification: (seq, readerOrBody) => {
    if (readerOrBody && typeof readerOrBody === 'object') {
      const q = readerQuery(readerOrBody.reader ?? readerOrBody.reader_index)
      if (readerOrBody.se_id || readerOrBody.seId) q.set('se_id', readerOrBody.se_id || readerOrBody.seId)
      if (readerOrBody.aid) q.set('aid', readerOrBody.aid)
      return j('DELETE', `/api/esim/notifications/${seq}?${q}`)
    }
    return j(
      'DELETE',
      `/api/esim/notifications/${seq}?${readerQuery(readerOrBody)}`,
    )
  },
  esimNotificationRemove: (seq, readerOrBody) => {
    if (readerOrBody && typeof readerOrBody === 'object') {
      const q = readerQuery(readerOrBody.reader ?? readerOrBody.reader_index)
      if (readerOrBody.se_id || readerOrBody.seId) q.set('se_id', readerOrBody.se_id || readerOrBody.seId)
      if (readerOrBody.aid) q.set('aid', readerOrBody.aid)
      return j('DELETE', `/api/esim/notifications/${seq}?${q}`)
    }
    return j(
      'DELETE',
      `/api/esim/notifications/${seq}?${readerQuery(readerOrBody)}`,
    )
  },
}

export function connectWs(onMsg, onAuthLost) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  let ws, alive = true
  const open = () => {
    // The marker lets the server distinguish clients that understand the 4401 close code
    // from an already-open pre-upgrade tab that would otherwise reconnect forever.
    ws = new WebSocket(`${proto}://${location.host}/ws?auth_close=1`)
    ws.onmessage = (e) => { try { onMsg(JSON.parse(e.data)) } catch {} }
    ws.onclose = (event) => {
      if (event.code === 4401) {
        alive = false
        onAuthLost?.()
        return
      }
      if (alive) setTimeout(open, 2000)
    }
    ws.onerror = () => { try { ws.close() } catch {} }
  }
  open()
  return () => { alive = false; try { ws.close() } catch {} }
}
