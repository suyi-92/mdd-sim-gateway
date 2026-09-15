import assert from 'node:assert/strict'
import test from 'node:test'
import { communicationLineDetails, maskedLineNumber } from '../webui/src/simLineDetails.js'

const tr = (key, values = {}) => key.replace(/\{(\w+)\}/g, (_, name) => values[name] ?? '')

test('communication details distinguish carrier, configured line, network and route', () => {
  const line = { id: '1', name: 'Desk line', mcc: '234', mnc: '33', msisdn: '+447700900357' }
  const device = {
    sim: { number: '+447700900357', carrier: {
      name: 'Fixture Mobile', home_network: 'EE', current_network: 'Visited Network', plmn: '234-33',
    } },
    egress: { country: 'gb', node: 'GB Fixture Node', mode: 'manual', ready: true },
  }

  assert.deepEqual(communicationLineDetails(line, device, tr, 'en'), {
    carrier: 'Fixture Mobile (234-33)',
    line: 'Desk line',
    number: '••••0357',
    country: 'United Kingdom (GB)',
    network: 'Visited Network · EE',
    networkRoute: 'GB Fixture Node',
  })
})

test('direct and unavailable routes are explicit without exposing the full number', () => {
  const line = { id: '2', mcc: '310', mnc: '280', msisdn: '+15555557654' }
  const direct = communicationLineDetails(line, {
    sim: { carrier: { home_network: 'Fixture Wireless', plmn: '310-280' } },
    egress: { country: 'us', mode: 'direct', ready: true },
  }, tr, 'en')
  assert.equal(direct.number, '••••7654')
  assert.equal(direct.networkRoute, 'Explicit direct connection')
  assert.equal(JSON.stringify(direct).includes('+15555557654'), false)

  const unavailable = communicationLineDetails({}, {}, tr, 'en')
  assert.equal(unavailable.number, 'Number unavailable')
  assert.equal(unavailable.networkRoute, 'Not connected')
})

test('number masking never exposes more than the final four digits', () => {
  assert.equal(maskedLineNumber('+44 (0) 7700 900357', tr), '••••0357')
  assert.equal(maskedLineNumber('', tr), 'Number unavailable')
})
