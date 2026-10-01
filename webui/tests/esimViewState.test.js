import test from 'node:test'
import assert from 'node:assert/strict'
import { canRetainEsimView, esimText } from '../src/esimViewState.js'

test('only the same physical eUICC can retain its historical view during recovery', () => {
  const owner = { reader: 'fixture-reader', hardwareId: 'fixture-device', hardwareGeneration: 'usb-1', profileIds: ['old-card', 'new-card'] }
  const devices = [{ id: 'fixture-device', present: true, hardware_generation: 'usb-1' }]
  for (const card of [undefined, { iccid: '' }, { iccid: 'new-card' }]) {
    assert.equal(canRetainEsimView(owner, owner.reader, card, devices), true)
  }
  assert.equal(canRetainEsimView(owner, owner.reader, { iccid: 'different-card' }, devices), false)
  assert.equal(canRetainEsimView(owner, owner.reader, { iccid: 'new-card', hardware_id: 'another-device' }, devices), false)
  assert.equal(canRetainEsimView(owner, 'other-reader', undefined, devices), false)
  assert.equal(canRetainEsimView(owner, owner.reader, undefined, [{ ...devices[0], hardware_generation: 'usb-2' }]), false)
  assert.equal(canRetainEsimView(owner, owner.reader, undefined, [{ ...devices[0], present: false }]), false)
  assert.equal(canRetainEsimView(owner, owner.reader, undefined, []), false)
  assert.equal(esimText({ unexpected: 'object' }, 'profile'), 'profile')
})
