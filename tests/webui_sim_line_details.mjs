import assert from 'node:assert/strict'
import test from 'node:test'
import { copyExactText } from '../webui/src/copyText.js'
import { communicationLineDetails, lineNumber } from '../webui/src/simLineDetails.js'

const tr = (key, values = {}) => key.replace(/\{(\w+)\}/g, (_, name) => values[name] ?? '')

test('communication details distinguish carrier, configured line, network and route', () => {
  const line = { id: '1', name: 'Desk line', mcc: '234', mnc: '33', msisdn: '+447700900357' }
  const device = {
    sim: { number: '+447700900357', number_country: 'gb', home_country: 'gb', carrier: {
      name: 'Fixture Mobile', home_network: 'EE', current_network: 'Visited Network', plmn: '234-33',
    } },
    egress: { country: 'gb', node: 'GB Fixture Node', mode: 'manual', ready: true },
  }

  assert.deepEqual(communicationLineDetails(line, device, tr, 'en'), {
    carrier: 'Fixture Mobile (234-33)',
    line: 'Desk line',
    number: '+447700900357',
    country: 'United Kingdom (GB)',
    homeNetwork: 'United Kingdom (GB) · 234-33',
    network: 'Visited Network · EE',
    networkRoute: 'GB Fixture Node',
  })
})

test('Saily US phone region remains separate from its Hong Kong SIM and route', () => {
  const details = communicationLineDetails({ msisdn: '+12025550123' }, {
    sim: { number_country: 'us', home_country: 'hk',
      carrier: { name: 'Saily', brand_source: 'esim_profile', plmn: '454-00' } },
    egress: { country: 'hk', node: 'HK Fixture', mode: 'manual', ready: true },
  }, tr, 'en')
  assert.equal(details.carrier, 'Saily')
  assert.equal(details.country, 'United States (US)')
  assert.match(details.homeNetwork, /\(HK\) · 454-00$/)
  assert.equal(details.homeNetwork.includes('United States'), false)
  assert.equal(details.networkRoute, 'HK Fixture')
})

test('direct and unavailable routes are explicit and retain the full number', () => {
  const line = { id: '2', mcc: '310', mnc: '280', msisdn: '+15555557654' }
  const direct = communicationLineDetails(line, {
    sim: { carrier: { home_network: 'Fixture Wireless', plmn: '310-280' } },
    egress: { country: 'us', mode: 'direct', ready: true },
  }, tr, 'en')
  assert.equal(direct.number, '+15555557654')
  assert.equal(direct.networkRoute, 'Explicit direct connection')

  const unavailable = communicationLineDetails({}, {}, tr, 'en')
  assert.equal(unavailable.number, 'Number unavailable')
  assert.equal(unavailable.networkRoute, 'Not connected')
})

test('line number display preserves every dialling character', () => {
  assert.equal(lineNumber('+44**7700#', tr), '+44**7700#')
  assert.equal(lineNumber('', tr), 'Number unavailable')
})

test('clipboard receives the exact displayed number including plus, star and hash', async () => {
  let copied = ''
  const navigatorRef = { clipboard: { writeText: async value => { copied = value } } }
  assert.equal(await copyExactText('+44**7700#', navigatorRef), true)
  assert.equal(copied, '+44**7700#')
})
