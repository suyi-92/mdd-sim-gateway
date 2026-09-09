import test from 'node:test'
import assert from 'node:assert/strict'
import { boundedRead } from '../src/pollRequest.js'
import { api, setCsrf } from '../src/api.js'

test('a read that ignores abort cannot permanently hold the refresh batch', async () => {
  let signal
  const request = boundedRead(value => { signal = value; return new Promise(() => {}) }, 10)
  await assert.rejects(request, { code: 'timeout' })
  assert.equal(signal.aborted, true)
  assert.equal(await boundedRead(() => Promise.resolve('recovered'), 10), 'recovered')
})

test('deadline covers a stalled response body and successful reads do not abort later', async () => {
  let signal
  const request = boundedRead(async value => {
    signal = value
    const response = { text: () => new Promise(() => {}) }
    return response.text()
  }, 10)
  await assert.rejects(request, { code: 'timeout' })
  assert.equal(signal.aborted, true)
  await boundedRead(value => { signal = value; return 42 }, 5)
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(signal.aborted, false)
})

test('failed device reads preserve failure and upload only closed diagnostics after recovery', async t => {
  const original = globalThis.fetch
  t.after(() => { globalThis.fetch = original; setCsrf('') })
  const uploaded = []
  let failing = true
  globalThis.fetch = async (path, options) => {
    if (options.method === 'POST') {
      uploaded.push(JSON.parse(options.body))
      return new Response('{}', { status: 200 })
    }
    return failing ? new Response('unavailable', { status: 503 })
      : new Response('{"devices":[]}', { status: 200 })
  }
  setCsrf('fixture-csrf')
  await assert.rejects(api.devices(), { status: 503 })
  failing = false
  assert.deepEqual(await api.devices(), { devices: [] })
  await new Promise(resolve => setTimeout(resolve, 0))
  const events = uploaded.flatMap(batch => batch.events)
  assert.deepEqual(events.map(item => item.outcome), ['http', 'recovered'])
  for (const item of events) {
    assert.deepEqual(Object.keys(item).sort(),
      ['client_epoch', 'elapsed_ms', 'outcome', 'scope', 'sequence', 'status_code'])
  }
  assert.equal(JSON.stringify(uploaded).includes('fixture-csrf'), false)
  globalThis.fetch = async () => new Response('<html>proxy error</html>', { status: 200 })
  await assert.rejects(api.devices(), { code: 'invalid_response' })
})
