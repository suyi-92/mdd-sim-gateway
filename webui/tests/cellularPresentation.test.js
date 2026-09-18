import test from 'node:test'
import assert from 'node:assert/strict'
import { cellularRegistrationDetail, cellularNetworkRejectDetail, networkName, networkLabel, currentCellularNetwork,
  cellularOperationOutcome, cellularOperationProgress, networkAvailability } from '../src/cellularPresentation.js'

const t = (value, args = {}) => value.replace('{signal}', String(args.signal ?? ''))

test('network feedback distinguishes restoring from initial registration and shows remaining budget', () => {
  const translate = (value, args = {}) => value.replace('{seconds}', args.seconds)
  const feedback = cellularOperationProgress({ phase: 'restoring', remaining_seconds: 21 }, translate)
  assert.match(feedback, /restoring the previous selection/)
  assert.match(feedback, /21s remaining/)
})

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

test('EPS reject cause 7 is not displayed as an ordinary search timeout', () => {
  const detail = cellularNetworkRejectDetail({ cellular: { network_reject: {
    observed_at: 100, cause_code: 7, cause: 'ps-services-not-allowed',
    rat: 'lte', service_domain: 'ps', operator_id: '',
  } } }, translated)
  assert.match(detail, /Cellular service rejected by the network/)
  assert.match(detail, /EPS services not allowed \(cause 7\)/)
  assert.match(detail, /Unknown network/)
  assert.doesNotMatch(detail, /timeout/i)
})

const translated = (value, args = {}) => value.replace(/\{([^}]+)\}/g, (_, key) => String(args[key]))
const mobile = { operator_id: '46000', name: 'China Mobile', name_zh: '中国移动' }
const unicom = { operator_id: '46001', name: 'China Unicom', name_zh: '中国联通' }
const live = (network = mobile) => ({ ...network, connected: true, observed_at: 200 })

test('Chinese labels and English labels use the same network identity', () => {
  assert.equal(networkName(mobile, 'zh'), '中国移动')
  assert.equal(networkName(mobile, 'en'), 'China Mobile')
  assert.equal(networkLabel(mobile, 'zh'), '中国移动 (46000)')
  assert.equal(networkName({ name: 'Unmapped operator', operator_id: '00101' }, 'zh'), 'Unmapped operator')
})

test('old operator properties cannot make a searching modem appear connected', () => {
  assert.equal(currentCellularNetwork({ cellular: {
    registration: 'searching', operator_code: '46000', operator: 'China Mobile',
  } }).connected, false)
})

test('a past successful selection does not remain a current connection after disconnect', () => {
  const operation = { action: 'apply', state: 'success', finished_at: 100,
    result: { registration: { operator_id: '46000' } } }
  const result = cellularOperationOutcome(operation, { ...live(), connected: false }, [mobile], 'zh', translated)
  assert.equal(result.tone, 'warning')
  assert.match(result.text, /Currently not registered/)
  const changed = cellularOperationOutcome(operation, live(unicom), [mobile, unicom], 'zh', translated)
  assert.match(changed.text, /Current network: 中国联通 \(46001\)/)
})

test('only a new matching registration resolves a historical scan recovery warning', () => {
  const operation = { action: 'scan', state: 'partial', finished_at: 100,
    error: { recovery: { state: 'failed', mode: 'manual', operator_id: '46000' } } }
  const recovered = cellularOperationOutcome(operation, live(), [mobile, unicom], 'zh', translated)
  assert.equal(recovered.tone, 'info')
  assert.match(recovered.text, /Registration has now recovered on 中国移动 \(46000\)/)
  const old = cellularOperationOutcome(operation, { ...live(), observed_at: 99 }, [mobile], 'zh', translated)
  assert.equal(old.tone, 'info')
  assert.match(old.text, /Updating current registration/)
  const other = cellularOperationOutcome(operation, live(unicom), [mobile], 'zh', translated)
  assert.equal(other.tone, 'warning')
  assert.match(other.text, /original selection was not restored/)
})

test('a last-scan current badge follows the live PLMN after changing networks', () => {
  assert.equal(networkAvailability({ ...unicom, status: 'current' }, live(mobile)), 'available')
  assert.equal(networkAvailability({ ...mobile, status: 'available' }, live(mobile)), 'current')
  assert.equal(networkAvailability({ ...mobile, status: 'current' }, { ...live(), connected: false }), 'available')
})

test('a newer registration supersedes timeout as current truth without rewriting history', () => {
  const automatic = { action: 'apply', state: 'failed', finished_at: 100,
    selection: { mode: 'automatic', operator_id: '' },
    error: { code: 'operation_timeout', recovery: { state: 'failed' } } }
  const recovered = cellularOperationOutcome(
    automatic, live(unicom), [mobile, unicom], 'zh', translated)
  assert.equal(recovered.tone, 'info')
  assert.equal(recovered.resolved, true)
  assert.match(recovered.text, /newer modem sample confirms registration on 中国联通 \(46001\)/)
  assert.match(recovered.text, /selection mode was not confirmed/)

  assert.equal(cellularOperationOutcome(
    automatic, { ...live(unicom), observed_at: 100 }, [unicom], 'zh', translated), null)
  assert.equal(cellularOperationOutcome(
    { ...automatic, finished_at: 0 }, live(unicom), [unicom], 'zh', translated), null)
  assert.equal(cellularOperationOutcome(
    automatic, { ...live(unicom), connected: false }, [unicom], 'zh', translated), null)
})

test('late registration on another PLMN does not confirm a manual target', () => {
  const manual = { action: 'apply', state: 'failed', finished_at: 100,
    selection: { mode: 'manual', operator_id: '46000' },
    error: { code: 'network_timeout' } }
  const other = cellularOperationOutcome(
    manual, live(unicom), [mobile, unicom], 'zh', translated)
  assert.equal(other.tone, 'warning')
  assert.equal(other.resolved, true)
  assert.match(other.text, /Service has recovered on 中国联通 \(46001\)/)
  assert.match(other.text, /requested network 中国移动 \(46000\) was not confirmed/)

  const matching = cellularOperationOutcome(manual, live(mobile), [mobile], 'zh', translated)
  assert.equal(matching.tone, 'info')
  assert.match(matching.text, /selection mode was not confirmed/)
  assert.equal(cellularOperationOutcome(
    { ...manual, error: { code: 'network_rejected' } }, live(mobile), [mobile], 'zh', translated), null)
})
