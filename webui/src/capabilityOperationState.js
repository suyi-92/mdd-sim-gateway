import { api } from './api.js'

const ACTIVE = new Set(['accepted', 'running'])

// Hardware work outlives a page component. The server owns the operation journal;
// this store only fans one recovered snapshot out to Overview and Devices views.
export function createCapabilityOperationState(client = api, timers = globalThis) {
  const records = new Map()
  let sequence = 0
  const active = operation => ACTIVE.has(operation?.state)
  const emit = record => record.listeners.forEach(listener => listener())
  const update = (record, patch) => { record.value = { ...record.value, ...patch }; emit(record) }
  const schedule = (id, record) => {
    timers.clearTimeout(record.timer)
    if (record.listeners.size && active(record.value.operation)) {
      record.timer = timers.setTimeout(() => refresh(id), 750)
    }
  }
  const accept = (record, operation, force = false) => {
    if (!operation || typeof operation !== 'object') operation = {}
    const current = record.value.operation || {}
    const localRequest = String(current.operation_id || '').startsWith('request-')
    if (localRequest && !force) return
    const same = current.operation_id && current.operation_id === operation.operation_id
    const currentTerminal = ['success', 'failed', 'interrupted'].includes(current.state)
    const currentActive = ['accepted', 'running'].includes(current.state)
    const incomingActive = ['accepted', 'running'].includes(operation.state)
    const newerOperation = !same && Number(operation.created_at || 0) > 0
      && Number(operation.created_at || 0) > Number(current.created_at || 0)
    if (currentActive && !same && !force && !newerOperation) return
    const newer = force || !current.operation_id
      || (same && !currentTerminal && Number(operation.updated_at || 0) >= Number(current.updated_at || 0))
      || newerOperation
      || (!same && !currentActive
        && Number(operation.updated_at || 0) >= Number(current.updated_at || 0))
    if (same && currentTerminal && incomingActive) return
    if (newer) update(record, { operation, submitting: false, readError: false })
  }
  const ensure = device => {
    let record = records.get(device.id)
    if (!record) {
      record = { value: { operation: device.capability_operation || {}, submitting: false,
        readError: false }, listeners: new Set(), timer: null, read: null, epoch: 0 }
      records.set(device.id, record)
    }
    return record
  }
  async function refresh(id) {
    const record = records.get(id)
    if (!record || record.read) return record?.read
    const epoch = record.epoch
    record.read = (async () => {
      try {
        const result = await client.deviceCapabilityOperation(id)
        if (record.epoch === epoch) accept(record, result?.operation || {})
      } catch {
        if (record.epoch === epoch) update(record, { readError: true })
      } finally {
        record.read = null
        schedule(id, record)
      }
    })()
    return record.read
  }
  return {
    ensure,
    observe(device) {
      const record = ensure(device)
      if (device.capability_operation?.operation_id) accept(record, device.capability_operation)
    },
    get: id => records.get(id)?.value,
    subscribe(id, listener) {
      const record = records.get(id)
      record.listeners.add(listener)
      void refresh(id)
      return () => {
        record.listeners.delete(listener)
        if (!record.listeners.size) timers.clearTimeout(record.timer)
      }
    },
    refresh,
    async start(id, patch) {
      const record = records.get(id)
      if (!record || record.value.submitting || active(record.value.operation)) return null
      const epoch = ++record.epoch
      update(record, { submitting: true, readError: false, operation: {
        operation_id: `request-${++sequence}`, state: 'accepted', phase: 'queued',
        target: { ...patch }, updated_at: Date.now() / 1000,
      } })
      try {
        const result = await client.patchDeviceCapabilities(id, patch)
        // This response belongs to the captured POST epoch; browser/server wall clocks
        // are irrelevant and may differ by minutes.
        if (record.epoch === epoch) accept(record, result?.operation || {}, true)
        return result
      } catch (error) {
        if (record.epoch === epoch) update(record, { submitting: false, operation: {},
          requestError: error })
        throw error
      } finally {
        void refresh(id)
        schedule(id, record)
      }
    },
  }
}

export const capabilityOperationState = createCapabilityOperationState()
