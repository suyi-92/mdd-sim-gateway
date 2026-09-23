import test from 'node:test'
import assert from 'node:assert/strict'
import { savedLineMetadata, mergeSavedLine, mergeSavedDevice } from '../src/savedLineMetadata.js'
import { communicationLineDetails } from '../src/simLineDetails.js'

test('a confirmed save updates device, call and SMS number displays on the same line only', () => {
  const saved = savedLineMetadata({ id: '1', iccid: 'fixture-card', name: 'Fixture',
    msisdn: '+15555550100', msisdn_source: 'manual', number_country: 'us', pin: 'private' })
  const lines = [{ id: '1', iccid: 'fixture-card', msisdn: '', status: { state: 'STOPPED' } },
    { id: '2', iccid: 'other-card', msisdn: '+15555550101' }]
  const devices = [{ id: 'modem', instance_id: '1', sim: { number: '', present: true } },
    { id: 'reader', instance_id: '2', sim: { number: '+15555550101' } }]
  const nextLines = mergeSavedLine(lines, saved), nextDevices = mergeSavedDevice(devices, saved)
  assert.equal(nextDevices[0].sim.number, saved.msisdn)
  assert.equal(communicationLineDetails(nextLines[0], nextDevices[0]).number, saved.msisdn)
  assert.equal(nextLines[0].status.state, 'STOPPED')
  assert.equal(nextLines[1], lines[1]); assert.equal(nextDevices[1], devices[1])
  assert.equal('pin' in saved, false)
  assert.equal(mergeSavedLine([{ ...lines[0], iccid: 'replacement' }], saved)[0].msisdn, '')
  assert.equal(mergeSavedDevice(devices, { ...saved, msisdn: '' })[0].sim.number, '')
})
