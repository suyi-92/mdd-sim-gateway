import test from 'node:test'
import assert from 'node:assert/strict'
import { esimErrorMessage, profileOperationFeedback } from '../src/esimErrors.js'
import { mergeNotificationSnapshot } from '../src/esimNotifications.js'

const t = (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? `{${key}}`)
const detail = { code: 'esim_operation_failed', message: 'The card rejected this operation.',
  diagnostic: { step: 'es10c_enable_profile', lpac_code: -1, card_result: 3,
    reason: 'policy_disallowed', raw: 'private-token' } }

test('card result and helper code remain distinct without rendering raw fields', () => {
  const text = esimErrorMessage({ data: { detail } }, t)
  assert.match(text, /Step: es10c_enable_profile/)
  assert.match(text, /Helper code: -1/)
  assert.match(text, /Card result: 3/)
  assert.doesNotMatch(text, /private-token/)
  assert.doesNotMatch(esimErrorMessage({ data: { detail: { ...detail,
    diagnostic: { step: 'private-step', lpac_code: 'private', card_result: -1, status_word: 'private' } } } }, t), /private/)
})

test('last failure survives cache polls and clears after a later successful command', () => {
  const failed = { state: 'failed', error: detail, updated_at: 10 }
  const initial = [{ id: 'one', eid: 'fixture-euicc', profiles: [{ iccid: 'fixture-card', operation_status: failed }] }]
  const snapshot = status => ({ cached: true, ses: [{ id: 'one', eid: 'fixture-euicc', profiles: [{
    iccid: 'fixture-card', operation_status: status }] }] })
  let merged = mergeNotificationSnapshot(initial, snapshot(undefined))
  assert.match(profileOperationFeedback(merged[0].profiles[0].operation_status, t), /Last operation failed/)
  merged = mergeNotificationSnapshot(merged, snapshot({ state: 'success', updated_at: 11 }))
  assert.equal(profileOperationFeedback(merged[0].profiles[0].operation_status, t), '')
  merged = mergeNotificationSnapshot(merged, snapshot(failed))
  assert.equal(merged[0].profiles[0].operation_status.state, 'success')
  const foreign = snapshot(failed); foreign.ses[0].eid = 'other-euicc'
  assert.deepEqual(mergeNotificationSnapshot(merged, foreign), merged)
})
