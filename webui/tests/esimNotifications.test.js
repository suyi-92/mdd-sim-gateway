import test from 'node:test'
import assert from 'node:assert/strict'
import { mergeNotificationSnapshot, newerNotificationStatus, notificationReaderFailureRecovered } from '../src/esimNotifications.js'

const failed = { state: 'failed', reason_code: 'reader_unavailable', attempts: 0, elapsed_ms: 0, updated_at: 101 }
const profile = { iccid: 'fixture-card', profileState: 'enabled', notification_status: failed,
  recovery_status: { id: 'task', device_id: 'modem', state: 'failed', finished_at: 100 } }
const card = { present: true, iccid: 'fixture-card', hardware_id: 'modem' }
const device = { id: 'modem', present: true, esim_recovery: { id: 'task' },
  logical_channels: { status: 'ready', capacity: 3, allocated: 3 },
  sim: { present: true }, cellular_recovery: { state: 'ready' },
  cellular: { registration: 'roaming', operator_code: '00101', observed_at: 120 } }

test('only an unattempted reader error superseded by same-card recovery loses its row alert', () => {
  assert.equal(notificationReaderFailureRecovered(profile, device, card), true)
  for (const status of [{ ...failed, attempts: 1 }, { ...failed, elapsed_ms: 5 },
    { ...failed, reason_code: 'network_transport' }, { ...failed, updated_at: 121 }]) {
    assert.equal(notificationReaderFailureRecovered({ ...profile, notification_status: status }, device, card), false)
  }
  assert.equal(notificationReaderFailureRecovered(profile, device, { ...card, iccid: 'replacement' }), false)
  assert.equal(notificationReaderFailureRecovered(profile, { ...device, id: 'other' }, card), false)
  assert.equal(notificationReaderFailureRecovered({ ...profile, profileState: 'disabled' }, device, card), false)
  assert.equal(profile.notification_status.state, 'failed', 'audit remains unchanged')
})

test('passive cache refresh repairs missed completion without replacing current profile or notification data', () => {
  const ses = [{ id: 'one', eid: 'fixture-euicc', profiles: [profile], notifications: [{ seq: 1 }] }]
  const snapshot = { cached: true, ts: 130, ses: [{ id: 'one', eid: 'fixture-euicc',
    profiles: [{ iccid: 'fixture-card', profileState: 'disabled' }] }] }
  const merged = mergeNotificationSnapshot(ses, snapshot)
  assert.equal(merged[0].profiles[0].notification_status.state, 'empty')
  assert.equal(merged[0].profiles[0].profileState, 'enabled')
  assert.deepEqual(merged[0].notifications, [{ seq: 1 }])
  assert.equal(newerNotificationStatus(merged[0].profiles[0].notification_status, failed).state, 'empty')
  for (const other of [
    { ...snapshot, cached: false }, { ...snapshot, ts: 90 },
    { ...snapshot, ses: [{ ...snapshot.ses[0], eid: 'other-euicc' }] },
    { ...snapshot, ses: [{ ...snapshot.ses[0], id: 'two' }] },
  ]) assert.equal(mergeNotificationSnapshot(ses, other)[0].profiles[0].notification_status, failed)
})
