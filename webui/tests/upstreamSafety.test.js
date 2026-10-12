import test from 'node:test'
import assert from 'node:assert/strict'
import { displayedMessageWatermark } from '../src/messageReadWatermark.js'
import { ordinaryCallEndLabel } from '../src/call-status.js'
import { api, setCsrf } from '../src/api.js'

const row = (id, top, bottom) => ({ dataset: { messageId: id },
  getBoundingClientRect: () => ({ width: 100, height: bottom - top, top, bottom }) })
const viewport = (rows, visible = true) => ({ getClientRects: () => visible ? [{}] : [],
  getBoundingClientRect: () => ({ width: 100, height: 100, top: 0, bottom: 100 }),
  querySelectorAll: () => rows })

test('read watermark comes only from visible painted message rows', () => {
  const rows = [row('10', 0, 30), row('11', 40, 70), row('12', 110, 140), row('bad', 0, 10)]
  assert.equal(displayedMessageWatermark(viewport(rows)), 11)
  assert.equal(displayedMessageWatermark(viewport(rows, false)), 0)
  assert.equal(displayedMessageWatermark(null), 0)
})

test('microphone failure is named without overriding a final backend call result', () => {
  assert.equal(ordinaryCallEndLabel('', 'User Denied Media Access'), 'Microphone unavailable')
  assert.equal(ordinaryCallEndLabel('failed', 'Rejected', true), 'Failed')
  assert.equal(ordinaryCallEndLabel('', 'Rejected', true), 'Call ended')
})

test('a delayed multipart 401 cannot clear a newer authenticated session', async () => {
  const oldFetch = globalThis.fetch, oldWindow = globalThis.window
  let complete, expired = 0
  globalThis.window = { dispatchEvent: () => expired++ }
  globalThis.fetch = () => new Promise(resolve => { complete = resolve })
  try {
    setCsrf('old-fixture')
    const pending = api.stageMmsAttachment('1', new File(['a'], 'fixture.txt'))
    setCsrf('new-fixture')
    complete({ status: 401, ok: false, text: async () => JSON.stringify({ detail: 'expired' }) })
    await assert.rejects(pending, /expired/)
    assert.equal(expired, 0)
    let sent
    globalThis.fetch = async (_url, options) => { sent = options.headers; return { ok: true, status: 200, text: async () => '{}' } }
    await api.markThreadRead('1', { peer: 'INFO', message_id: 11 })
    assert.equal(sent['X-MDD-CSRF-Token'], 'new-fixture')
  } finally { globalThis.fetch = oldFetch; globalThis.window = oldWindow; setCsrf('') }
})
