import test from 'node:test'
import assert from 'node:assert/strict'
import { capabilityDetail } from '../src/capabilityPresentation.js'

test('steady VoWiFi wording is identical across device and engine snapshots on all hardware', () => {
  for (const device_type of ['modem', 'reader']) {
    const device = { device_type, present: true }
    for (const reason of ['', 'Stopped.', 'The VoWiFi line is stopped']) {
      assert.equal(capabilityDetail('vowifi', { desired: false, actual: 'off', reason }, device),
        'VoWiFi is disabled.')
    }
    for (const reason of ['', 'IMS registered.', 'Working']) {
      assert.equal(capabilityDetail('vowifi', { desired: true, actual: 'on', reason }, device),
        'Working — connected to the carrier over Wi-Fi.')
    }
  }
})

test('real faults, hardware absence, blocked capabilities and transitions keep their reasons', () => {
  for (const actual of ['error', 'degraded', 'starting', 'stopping', 'unsupported']) {
    assert.equal(capabilityDetail('vowifi', { desired: true, actual, reason: 'Current reason' },
      { present: true }), 'Current reason')
  }
  assert.equal(capabilityDetail('vowifi', { desired: false, actual: 'off', reason: 'Device not connected' },
    { present: false }), 'Device not connected')
  assert.equal(capabilityDetail('vowifi', { desired: false, actual: 'off', available: false,
    reason: 'Insert a readable SIM before enabling VoWiFi' }, { present: true }),
  'Insert a readable SIM before enabling VoWiFi')
  assert.equal(capabilityDetail('cellular', { desired: true, actual: 'off', reason: 'Flight mode is enabled' },
    { present: true }), 'Flight mode is enabled')
})
