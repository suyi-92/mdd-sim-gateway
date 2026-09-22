import test from 'node:test'
import assert from 'node:assert/strict'
import { newerEsimRecovery, profileRecoveryStatus, waitForEsimLine } from '../src/esimRecovery.js'

const pending = { id: 'task-a', device_id: 'modem-a', state: 'registering',
  created_at: 100, updated_at: 110 }
const profile = { iccid: 'fixture-card', profileState: 'enabled', recovery_status: pending }
const card = { iccid: 'fixture-card', hardware_id: 'modem-a', present: true }
const device = { id: 'modem-a', esim_recovery: { ...pending, state: 'success', updated_at: 120 } }

test('device polling repairs a missed recovery event for the same enabled profile', () => {
  for (const state of ['success', 'failed', 'cancelled', 'network_rejected', 'waiting_flight_mode']) {
    const latest = { ...device.esim_recovery, state }
    assert.equal(profileRecoveryStatus(profile, { ...device, esim_recovery: latest }, card), latest)
  }
  assert.equal(profile.recovery_status.state, 'registering', 'the persisted snapshot is not mutated')
})

test('recovery snapshots cannot cross task, device, card or enabled-profile boundaries', () => {
  for (const [otherDevice, otherCard] of [
    [undefined, card],
    [{ ...device, esim_recovery: { ...device.esim_recovery, id: 'task-b' } }, card],
    [{ ...device, id: 'modem-b' }, card],
    [{ ...device, esim_recovery: { ...device.esim_recovery, device_id: 'modem-b' } }, card],
    [device, { ...card, hardware_id: 'modem-b' }],
    [device, { ...card, iccid: 'replacement-card' }],
    [device, { ...card, present: false }],
  ]) assert.equal(profileRecoveryStatus(profile, otherDevice, otherCard), pending)
  assert.equal(profileRecoveryStatus({ ...profile, profileState: 'disabled' }, device, card), null)
  assert.equal(profileRecoveryStatus({ ...profile, recovery_status: undefined }, device, card), undefined)
})

test('older HTTP snapshots and delayed WS progress cannot replace a newer result', () => {
  const latest = device.esim_recovery
  assert.equal(newerEsimRecovery(latest, pending), latest)
  assert.equal(profileRecoveryStatus({ ...profile, recovery_status: latest },
    { ...device, esim_recovery: pending }, card), latest)
  const next = { ...pending, id: 'task-b', created_at: 130, updated_at: 130 }
  assert.equal(newerEsimRecovery(latest, next), next)
  assert.equal(newerEsimRecovery(next, { ...latest, updated_at: 140 }), next)
})

test('a created container with NO_CARD is not a recovered line', async () => {
  let reads = 0
  const api = { status: async id => {
    assert.equal(id, 'fixture')
    return { state: ++reads === 1 ? 'NO_CARD' : 'OK' }
  } }
  assert.equal(await waitForEsimLine(api, 'fixture', 3000), true)
  assert.equal(reads, 2)
})

test('a persistent PIN/registration failure has a deadline', async () => {
  assert.equal(await waitForEsimLine({ status: async () => ({ state: 'NO_CARD' }) }, 'fixture', 10), false)
})

test('an unresponsive status request is aborted at the deadline', async () => {
  let signal
  const api = { status: (_id, current) => { signal = current; return new Promise(() => {}) } }
  assert.equal(await waitForEsimLine(api, 'fixture', 10), false)
  assert.equal(signal.aborted, true)
})
