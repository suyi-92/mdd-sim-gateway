import assert from 'node:assert/strict'
import test from 'node:test'
import { deviceSelection, physicallyPresentDevices } from '../webui/src/devicePresence.js'

const modem = Object.freeze({ id: 'modem-fixture', name: 'USB modem', present: true,
  capabilities: { flight: { desired: true }, vowifi: { desired: true } } })
const reader = Object.freeze({ id: 'reader-fixture', device_type: 'reader', present: true })
const unplugged = Object.freeze({ ...modem, present: false })

test('unplugged-only list clears the selected device and shows the empty state', () => {
  const result = deviceSelection([unplugged], modem.id)
  assert.deepEqual(result.visibleDevices, [])
  assert.equal(result.activeDeviceId, null)
  assert.equal(result.device, undefined)
  assert.equal(result.disconnectedCount, 1)
})

test('unplugging the selected modem immediately selects the remaining reader', () => {
  const result = deviceSelection([unplugged, reader], modem.id)
  assert.deepEqual(result.visibleDevices, [reader])
  assert.equal(result.activeDeviceId, reader.id)
  assert.equal(result.device, reader)
})

test('an already selected connected device stays selected', () => {
  assert.equal(deviceSelection([modem, reader], reader.id).activeDeviceId, reader.id)
})

test('remembered configuration survives unplug and reappears on reconnect', () => {
  const list = Object.freeze([unplugged, reader])
  deviceSelection(list, modem.id)
  assert.deepEqual(list, [unplugged, reader])
  assert.equal(list[0].capabilities.flight.desired, true)
  assert.equal(list[0].capabilities.vowifi.desired, true)
  const result = deviceSelection([modem], null)
  assert.equal(result.device, modem)
})

test('offline records require explicitly opening history and disappear when it closes', () => {
  assert.equal(deviceSelection([unplugged], null, true).device, unplugged)
  assert.equal(deviceSelection([unplugged], unplugged.id, false).device, undefined)
})

test('missing device selection and empty discovery results are safe', () => {
  assert.equal(deviceSelection([reader], 'removed-device').device, reader)
  assert.equal(deviceSelection([], modem.id).activeDeviceId, null)
  assert.equal(deviceSelection().activeDeviceId, null)
})

test('presence is independent of SIM, flight mode and capability state', () => {
  assert.deepEqual(physicallyPresentDevices([modem, { ...reader, sim: { present: false } }, unplugged]),
    [modem, { ...reader, sim: { present: false } }])
  assert.deepEqual(physicallyPresentDevices([{ id: 'legacy-without-presence' }]),
    [{ id: 'legacy-without-presence' }])
})
