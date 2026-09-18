import assert from 'node:assert/strict'
import test from 'node:test'
import { createRefreshCoordinator } from '../src/refreshCoordinator.js'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const tick = () => new Promise(resolve => setImmediate(resolve))

test('ready snapshots publish before a slow reader and a failed source does not hide them', async () => {
  const cards = deferred()
  const results = []
  let completed = false
  const refresh = createRefreshCoordinator({
    devices: () => ({ devices: ['current-device'] }),
    instances: () => { throw new Error('temporarily unavailable') },
    cards: () => cards.promise,
  }, (key, result) => results.push([key, result]), () => { completed = true })
  const pending = refresh()
  await tick()
  assert.deepEqual(results.map(([key]) => key).sort(), ['devices', 'instances'])
  assert.equal(results.find(([key]) => key === 'devices')[1].value.devices[0], 'current-device')
  assert.equal(results.find(([key]) => key === 'instances')[1].status, 'rejected')
  assert.equal(completed, false)
  cards.resolve({ cards: [] })
  await pending
  assert.equal(results.length, 3)
  assert.equal(completed, true)
})

test('events during a batch merge into one trailing refresh without overlapping reads', async () => {
  const reads = []
  const received = []
  const refresh = createRefreshCoordinator({ devices: () => {
    const read = deferred()
    reads.push(read)
    return read.promise
  } }, (_key, result) => received.push(result.value))
  const first = refresh()
  await tick()
  const trailing = refresh()
  assert.notEqual(trailing, first)
  assert.equal(refresh(), trailing)
  assert.equal(reads.length, 1)
  reads[0].resolve('first')
  await first
  await tick()
  assert.equal(reads.length, 2)
  reads[1].resolve('after-event')
  await trailing
  assert.deepEqual(received, ['first', 'after-event'])
  assert.equal(reads.length, 2)
})

test('an event during the trailing batch is also observed and future refreshes still work', async () => {
  const reads = []
  const refresh = createRefreshCoordinator({ devices: () => {
    const read = deferred()
    reads.push(read)
    return read.promise
  } }, () => {})
  const first = refresh()
  await tick()
  const second = refresh()
  reads[0].resolve({})
  await first
  await tick()
  const third = refresh()
  reads[1].reject(new Error('one transient failure'))
  await second
  await tick()
  assert.equal(reads.length, 3)
  reads[2].resolve({})
  await third
  const later = refresh()
  await tick()
  assert.notEqual(later, first)
  reads[3].resolve({})
  await later
})
