import assert from 'node:assert/strict'
import test from 'node:test'
import { createCellularNetworkState } from '../src/cellularNetworkState.js'

const device = { id: 'modem-fixture', cellular_network: { mode: 'automatic' } }
const network = { operator_id: '00101', name: 'Fixture Mobile', access_technology: 'lte', status: 'available' }
const timers = { setTimeout: () => 1, clearTimeout() {} }
const running = action => ({ context: 'fixture-context', networks: [network],
  operation: { id: 'fixture-op', action, state: 'running', selection: { mode: 'manual', operator_id: '00101' } } })

test('draft, scan names and running application survive page unsubscription', async () => {
  let status = { context: 'fixture-context', networks: [network], operation: null }
  const client = { cellularNetworkOperation: async () => status,
    selectCellularNetwork: async () => (status = running('apply')) }
  const store = createCellularNetworkState(client, timers)
  store.ensure(device)
  const leave = store.subscribe(device.id, () => {})
  await store.refresh(device.id)
  store.edit(device.id, { mode: 'manual', operatorId: '00101' })
  await store.start(device.id, 'apply', { mode: 'manual', operator_id: '00101' })
  leave()
  assert.equal(store.get(device.id).operation.state, 'running')
  assert.equal(store.get(device.id).networks[0].name, 'Fixture Mobile')
  status = { ...status, operation: { ...status.operation, state: 'success', result: { registration: { operator_id: '00101' } } } }
  const again = store.subscribe(device.id, () => {})
  await store.refresh(device.id)
  assert.equal(store.get(device.id).operation.state, 'success')
  assert.equal(store.get(device.id).mode, 'manual')
  assert.equal(store.get(device.id).operatorId, '00101')
  again()
})

test('a fresh browser recovers backend progress and prevents duplicate commands', async () => {
  let writes = 0
  const store = createCellularNetworkState({ cellularNetworkOperation: async () => running('apply'),
    scanCellularNetworks: async () => { writes++ } }, timers)
  store.ensure(device)
  await store.refresh(device.id)
  await store.start(device.id, 'scan')
  assert.equal(writes, 0)
  assert.equal(store.get(device.id).mode, 'manual')
})

test('partial scan preserves networks and a new SIM gets an empty independent context', async () => {
  let status = { ...running('scan'), operation: { ...running('scan').operation,
    state: 'partial', error: { code: 'scan_recovery', recovery: { state: 'failed' } } } }
  const store = createCellularNetworkState({ cellularNetworkOperation: async () => status }, timers)
  store.ensure(device)
  await store.refresh(device.id)
  assert.equal(store.get(device.id).networks[0].name, 'Fixture Mobile')
  status = { context: 'replacement-context', networks: [], operation: null }
  await store.refresh(device.id)
  assert.equal(store.get(device.id).networks.length, 0)
  assert.equal(store.get(device.id).operatorId, '')
})

test('temporary polling failure keeps the running operation instead of claiming idle', async () => {
  let broken = false
  const store = createCellularNetworkState({ cellularNetworkOperation: async () => {
    if (broken) throw new Error('offline')
    return running('scan')
  } }, timers)
  store.ensure(device)
  await store.refresh(device.id)
  broken = true
  await store.refresh(device.id)
  assert.equal(store.get(device.id).operation.state, 'running')
  assert.equal(store.get(device.id).readError, true)
})

test('a stale read from before submission cannot erase the new job', async () => {
  let resolveRead
  let delayed = false
  const idle = { context: 'fixture-context', networks: [network], operation: null }
  const store = createCellularNetworkState({ cellularNetworkOperation: () => delayed
    ? new Promise(resolve => { resolveRead = resolve }) : Promise.resolve(idle),
    scanCellularNetworks: async () => running('scan') }, timers)
  store.ensure(device)
  await store.refresh(device.id)
  delayed = true
  const old = store.refresh(device.id)
  await store.start(device.id, 'scan')
  resolveRead(idle)
  await old
  assert.equal(store.get(device.id).operation.state, 'running')
  assert.equal(store.get(device.id).operation.action, 'scan')
})
