import test from 'node:test'
import assert from 'node:assert/strict'
import { acceptDownloadEvent, canDismissDownload, dismissDownload, isDownloadDismissed, shouldAutoDismissDownload } from '../src/esimDownloadFeedback.js'

test('only successful downloads with resolved recovery auto-dismiss', () => {
  for (const lineRecovery of [undefined, 'recovering', 'failed', 'cancelled']) {
    assert.equal(shouldAutoDismissDownload({ operationId: 'fixture-job', done: true, lineRecovery }), false)
  }
  for (const lineRecovery of ['started', 'not_needed']) {
    assert.equal(shouldAutoDismissDownload({ operationId: 'fixture-job', done: true, lineRecovery }), true)
    assert.equal(shouldAutoDismissDownload({ operationId: 'fixture-job', error: 'Failed', lineRecovery }), false)
  }
  assert.equal(canDismissDownload({ operationId: 'fixture-job', done: true, lineRecovery: 'recovering' }), false)
  assert.equal(canDismissDownload({ operationId: 'fixture-job', error: 'Failed', lineRecovery: 'failed' }), true)
  assert.equal(canDismissDownload({ operationId: 'fixture-legacy', error: 'Failed' }), true)
  assert.equal(shouldAutoDismissDownload({ operationId: 'fixture-legacy', error: 'Failed' }), false)
})

test('late progress cannot regress a terminal result or revive dismissed jobs', () => {
  const result = { operationId: 'fixture-settled', done: true, lineRecovery: 'started' }
  assert.equal(acceptDownloadEvent(result, { operation_id: result.operationId, event: 'progress' }), false)
  assert.equal(acceptDownloadEvent(result, { operation_id: 'fixture-new', event: 'started' }), true)
  assert.equal(dismissDownload(result), true)
  assert.equal(isDownloadDismissed(result.operationId), true)
  assert.equal(acceptDownloadEvent(null, { operation_id: result.operationId, event: 'completed' }), false)
})

test('browser storage failures keep in-memory dismissal without storing card data', () => {
  globalThis.localStorage = { getItem() { throw new Error('denied') }, setItem() { throw new Error('denied') } }
  const result = { operationId: 'fixture-private', done: true, lineRecovery: 'not_needed', metadata: { iccid: 'must-not-store' } }
  assert.equal(dismissDownload(result), true)
  assert.equal(isDownloadDismissed(result.operationId), true)
  let saved
  globalThis.localStorage = { getItem: () => 'invalid json', setItem: (_, value) => { saved = JSON.parse(value) } }
  for (let i = 0; i < 70; i++) dismissDownload({ ...result, operationId: `fixture-${i}` })
  assert.equal(saved.length, 64)
  assert.equal(saved.every(value => typeof value === 'string' && value.startsWith('fixture-')), true)
  assert.equal(saved.includes('fixture-0'), false)
  delete globalThis.localStorage
})
