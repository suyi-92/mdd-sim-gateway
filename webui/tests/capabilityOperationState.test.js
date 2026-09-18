import test from 'node:test'
import assert from 'node:assert/strict'
import { createCapabilityOperationState } from '../src/capabilityOperationState.js'

const timers = { setTimeout: () => 1, clearTimeout: () => {} }

test('page unsubscribe keeps the server-owned capability operation', async () => {
  let status = { operation_id: 'op-1', state: 'running', phase: 'reconciling',
    target: { flight_mode: false }, updated_at: 2 }
  const client = {
    deviceCapabilityOperation: async () => ({ operation: status }),
    patchDeviceCapabilities: async () => ({ accepted: true, operation: status }),
  }
  const store = createCapabilityOperationState(client, timers)
  store.ensure({ id: 'modem-a', capability_operation: {} })
  const unsubscribe = store.subscribe('modem-a', () => {})
  await store.refresh('modem-a')
  unsubscribe()
  assert.equal(store.get('modem-a').operation.phase, 'reconciling')

  status = { ...status, state: 'success', phase: 'complete', updated_at: 3 }
  const again = store.subscribe('modem-a', () => {})
  await store.refresh('modem-a')
  assert.equal(store.get('modem-a').operation.state, 'success')
  again()
})

test('a fresh browser hydrates an accepted target from the device snapshot', () => {
  const store = createCapabilityOperationState({}, timers)
  store.ensure({ id: 'modem-a', capability_operation: {
    operation_id: 'op-2', state: 'running', phase: 'starting',
    target: { vowifi_enabled: true }, updated_at: 4,
  } })
  assert.deepEqual(store.get('modem-a').operation.target, { vowifi_enabled: true })
})

test('one accepted request cannot be submitted twice', async () => {
  let posts = 0
  let release
  const pending = new Promise(resolve => { release = resolve })
  const client = {
    patchDeviceCapabilities: async () => {
      posts += 1
      await pending
      return { accepted: true, operation: { operation_id: 'op-3', state: 'running',
        phase: 'reconciling', target: { cellular_enabled: true }, updated_at: 5 } }
    },
    deviceCapabilityOperation: async () => ({ operation: {} }),
  }
  const store = createCapabilityOperationState(client, timers)
  store.ensure({ id: 'modem-a', capability_operation: {} })
  const first = store.start('modem-a', { cellular_enabled: true })
  const second = await store.start('modem-a', { cellular_enabled: true })
  assert.equal(second, null)
  assert.equal(posts, 1)
  release()
  await first
})

test('a polling failure preserves running progress instead of claiming idle', async () => {
  const client = { deviceCapabilityOperation: async () => { throw new Error('offline') } }
  const store = createCapabilityOperationState(client, timers)
  store.ensure({ id: 'modem-a', capability_operation: {
    operation_id: 'op-4', state: 'running', phase: 'stopping', updated_at: 6,
  } })
  await store.refresh('modem-a')
  assert.equal(store.get('modem-a').operation.state, 'running')
  assert.equal(store.get('modem-a').readError, true)
})

test('server clock skew cannot strand the optimistic request or regress terminal state', async () => {
  let status = { operation_id: 'op-skew', state: 'running', phase: 'reconciling',
    target: { flight_mode: false }, updated_at: 10 }
  const client = {
    patchDeviceCapabilities: async () => ({ accepted: true, operation: status }),
    deviceCapabilityOperation: async () => ({ operation: status }),
  }
  const store = createCapabilityOperationState(client, timers)
  store.ensure({ id: 'modem-a', capability_operation: {} })
  await store.start('modem-a', { flight_mode: false })
  assert.equal(store.get('modem-a').operation.operation_id, 'op-skew')
  assert.equal(store.get('modem-a').submitting, false)

  status = { ...status, state: 'success', phase: 'complete', updated_at: 11 }
  await store.refresh('modem-a')
  assert.equal(store.get('modem-a').operation.state, 'success')
  status = { ...status, state: 'running', phase: 'reconciling', updated_at: 12 }
  await store.refresh('modem-a')
  assert.equal(store.get('modem-a').operation.state, 'success')
})

test('an old GET during delayed POST cannot clear the submission lock', async () => {
  let release
  const delayed = new Promise(resolve => { release = resolve })
  let posts = 0
  const client = {
    patchDeviceCapabilities: async () => {
      posts += 1
      await delayed
      return { accepted: true, operation: { operation_id: 'new-op', state: 'running',
        phase: 'reconciling', target: { flight_mode: false }, created_at: 10, updated_at: 10 } }
    },
    deviceCapabilityOperation: async () => ({ operation: {
      operation_id: 'old-op', state: 'success', phase: 'complete', created_at: 1, updated_at: 999,
    } }),
  }
  const store = createCapabilityOperationState(client, timers)
  store.ensure({ id: 'modem-a', capability_operation: {} })
  const first = store.start('modem-a', { flight_mode: false })
  await store.refresh('modem-a')
  assert.equal(store.get('modem-a').submitting, true)
  assert.match(store.get('modem-a').operation.operation_id, /^request-/)
  assert.equal(await store.start('modem-a', { flight_mode: false }), null)
  assert.equal(posts, 1)
  release()
  await first
  assert.equal(store.get('modem-a').operation.operation_id, 'new-op')
  assert.equal(store.get('modem-a').submitting, false)
})

test('a newer server operation replaces a missed older running operation', async () => {
  let status = { operation_id: 'op-1', state: 'running', phase: 'reconciling',
    created_at: 10, updated_at: 11, target: { flight_mode: false } }
  const client = { deviceCapabilityOperation: async () => ({ operation: status }) }
  const store = createCapabilityOperationState(client, timers)
  store.ensure({ id: 'modem-a', capability_operation: status })
  await store.refresh('modem-a')

  status = { operation_id: 'op-2', state: 'success', phase: 'complete',
    created_at: 20, updated_at: 21, target: { vowifi_enabled: false } }
  await store.refresh('modem-a')
  assert.equal(store.get('modem-a').operation.operation_id, 'op-2')
  assert.equal(store.get('modem-a').operation.state, 'success')
})
