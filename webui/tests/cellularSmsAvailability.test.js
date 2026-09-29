import test from 'node:test'
import assert from 'node:assert/strict'
import { retainedSmsAvailability, retainedSmsError } from '../src/cellularSmsAvailability.js'

const modem = () => ({
  shared: { modemmanager_active: true },
  sim: { present: true }, cellular: { registration: 'searching', data_active: false },
  capabilities: { cellular: { actual: 'off' }, flight: { desired: false, actual: 'off' } },
})

test('import does not require mobile data or network registration', () => {
  assert.equal(retainedSmsAvailability(modem()).available, true)
})
test('flight mode with a stopped service explains how to recover without enabling data', () => {
  const device = modem()
  device.shared.modemmanager_active = false
  device.capabilities.flight = { desired: true, actual: 'on' }
  const result = retainedSmsAvailability(device)
  assert.equal(result.available, false)
  assert.match(result.reason, /flight mode/)
  assert.match(result.reason, /mobile data can remain off/)
})
test('flight mode alone does not forbid reading an available modem message store', () => {
  const device = modem()
  device.capabilities.flight = { desired: true, actual: 'on' }
  assert.equal(retainedSmsAvailability(device).available, true)
})
test('absence, stale status, transitions, and unconfirmed SIM cannot admit import', () => {
  assert.equal(retainedSmsAvailability(null).available, false)
  assert.equal(retainedSmsAvailability(modem(), true).available, false)
  for (const change of [{ shared: { transitioning: true } }, { cellular: null }, { sim: { present: false } }]) {
    assert.equal(retainedSmsAvailability({ ...modem(), ...change }).available, false)
  }
})
test('VoWiFi-only mode and service failure have different explanations', () => {
  const device = modem()
  device.capabilities.cellular.actual = 'unsupported'
  assert.match(retainedSmsAvailability(device).reason, /VoWiFi-only/)
  const unavailable = { ...modem(), shared: { modemmanager_active: false } }
  assert.match(retainedSmsAvailability(unavailable).reason, /service is unavailable/)
})
test('legacy device responses defer to authoritative API and racing service failures are translated', () => {
  assert.equal(retainedSmsAvailability({}).available, true)
  const result = retainedSmsError({ data: { detail: { message: 'ModemManager is unavailable.' } } }, s => `translated: ${s}`)
  assert.match(result, /^translated: The cellular service is unavailable/)
})
