import test from 'node:test'
import assert from 'node:assert/strict'
import { cellularRegistrationDetail } from '../src/cellularPresentation.js'

const t = (value, args = {}) => value.replace('{signal}', String(args.signal ?? ''))

test('registered roaming is distinct from a disconnected data bearer', () => {
  const detail = cellularRegistrationDetail({ cellular: {
    registration: 'roaming', access_technology: 'lte', operator: 'Example visited network',
    signal: 78, data_active: false,
  } }, t)
  assert.equal(detail,
    'Roaming registered · LTE · Example visited network · Signal 78% · No data bearer')
})

test('home registration and an active bearer are reported independently', () => {
  const detail = cellularRegistrationDetail({ cellular: {
    registration: 'home', access_technology: 'lte', signal: 55, data_active: true,
  } }, t)
  assert.equal(detail, 'Home network registered · LTE · Signal 55% · Data bearer connected')
})

test('an unregistered modem does not claim base-station attachment', () => {
  assert.equal(cellularRegistrationDetail({ cellular: {
    registration: 'searching', access_technology: 'lte', signal: 40,
  } }, t), '')
})
