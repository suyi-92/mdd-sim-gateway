import { api } from './api.js'

// Page lifetime is shorter than a radio operation. Keep drafts here; the server
// owns running jobs, scan results and completion, including across a page reload.
export function createCellularNetworkState(client = api, timers = globalThis) {
  const records = new Map()
  let sequence = 0
  const initial = saved => ({ mode: saved?.mode === 'manual' ? 'manual' : 'automatic',
    operatorId: saved?.operator_id || '', networks: [], operation: null,
    context: '', loading: true, readError: false, blocked: false, dismissed: '' })
  const emit = record => record.listeners.forEach(listener => listener())
  const update = (record, patch) => { record.value = { ...record.value, ...patch }; emit(record) }
  const busy = value => value.operation?.state === 'running'
  const schedule = (id, record) => {
    timers.clearTimeout(record.timer)
    if (record.listeners.size) record.timer = timers.setTimeout(() => refresh(id), busy(record.value) ? 1500 : 5000)
  }
  const accept = (record, status) => {
    if (!status?.context || !Array.isArray(status.networks)) throw new Error('Invalid operation status')
    const changed = record.value.context && record.value.context !== status.context
    const operation = status.operation || null
    const patch = { ...status, operation, loading: false, readError: false }
    if (changed) Object.assign(patch, initial(status.saved_selection || record.saved), status, { loading: false })
    if (!record.value.context && status.saved_selection && !operation) {
      patch.mode = status.saved_selection.mode
      patch.operatorId = status.saved_selection.operator_id || ''
    }
    if (operation?.action === 'scan' && !record.value.context) patch.mode = 'manual'
    if (operation?.action === 'apply' && operation.id !== record.value.operation?.id) {
      patch.mode = operation.selection.mode
      patch.operatorId = operation.selection.operator_id || ''
      patch.dismissed = ''
    }
    if (changed && busy(record.value) && !operation && !status.selection_reset) {
      patch.operation = { id: `interrupted-${++sequence}`, action: record.value.operation.action,
        state: 'failed', error: { code: 'interrupted' } }
    }
    update(record, patch)
  }
  const ensure = device => {
    let record = records.get(device.id)
    if (!record) {
      record = { value: initial(device.cellular_network), saved: device.cellular_network,
        listeners: new Set(), epoch: 0, timer: null, read: null }
      records.set(device.id, record)
      for (const [id, old] of records) {
        if (records.size <= 64) break
        if (id !== device.id && !old.listeners.size && !busy(old.value)) {
          timers.clearTimeout(old.timer); records.delete(id)
        }
      }
    }
    record.saved = device.cellular_network
    return record
  }
  async function refresh(id) {
    const record = records.get(id)
    if (!record || record.read) return record?.read
    const epoch = record.epoch
    record.read = (async () => {
      try {
        const status = await client.cellularNetworkOperation(id)
        if (record.epoch === epoch) accept(record, status)
      } catch {
        if (record.epoch === epoch) update(record, { loading: false, readError: true })
      } finally { record.read = null; schedule(id, record) }
    })()
    return record.read
  }
  return {
    ensure,
    get: id => records.get(id)?.value,
    subscribe(id, listener) {
      const record = records.get(id)
      record.listeners.add(listener)
      void refresh(id)
      return () => { record.listeners.delete(listener); if (!record.listeners.size) timers.clearTimeout(record.timer) }
    },
    edit(id, patch) {
      const record = records.get(id)
      if (!busy(record.value)) update(record, { ...patch, dismissed: record.value.operation?.id || '' })
    },
    refresh,
    async start(id, action, selection = {}) {
      const record = records.get(id)
      if (busy(record.value) || record.value.loading || record.value.blocked) return
      const epoch = ++record.epoch
      update(record, { dismissed: '', readError: false, operation: {
        id: `request-${++sequence}`, action, state: 'running', selection,
      } })
      try {
        const status = action === 'scan' ? await client.scanCellularNetworks(id)
          : await client.selectCellularNetwork(id, selection)
        if (record.epoch === epoch) accept(record, status)
      } catch (error) {
        if (record.epoch === epoch) update(record, { operation: {
          id: `request-failed-${++sequence}`, action, state: 'failed',
          error: typeof error.data?.detail === 'object' ? error.data.detail : { code: 'request_failed' },
        } })
      } finally { void refresh(id); schedule(id, record) }
    },
  }
}

export const cellularNetworkState = createCellularNetworkState()
