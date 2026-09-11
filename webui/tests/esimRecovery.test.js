import test from 'node:test'
import assert from 'node:assert/strict'
import { waitForEsimLine } from '../src/esimRecovery.js'

test('a created container with NO_CARD is not a recovered line', async () => {
  let reads = 0
  const api = { status: async id => {
    assert.equal(id, 'fixture')
    return { state: ++reads === 1 ? 'NO_CARD' : 'OK' }
  } }
  assert.equal(await waitForEsimLine(api, 'fixture', 3000), true)
  assert.equal(reads, 2)
})

test('a persistent PIN/registration failure has a deadline', async () => {
  assert.equal(await waitForEsimLine({ status: async () => ({ state: 'NO_CARD' }) }, 'fixture', 10), false)
})

test('an unresponsive status request is aborted at the deadline', async () => {
  let signal
  const api = { status: (_id, current) => { signal = current; return new Promise(() => {}) } }
  assert.equal(await waitForEsimLine(api, 'fixture', 10), false)
  assert.equal(signal.aborted, true)
})
