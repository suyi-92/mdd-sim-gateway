import test from 'node:test'
import assert from 'node:assert/strict'
import { appendTranscript, LiveTranslation, OPENAI_TRANSLATION_CALLS_URL,
  translationEvent } from '../src/live-translation.js'

test('translation events expose only source and Chinese transcript deltas', () => {
  assert.deepEqual(translationEvent({ type: 'session.input_transcript.delta', delta: 'Hello' }),
    { type: 'source', delta: 'Hello' })
  assert.deepEqual(translationEvent({ type: 'session.output_transcript.delta', delta: '你好' }),
    { type: 'target', delta: '你好' })
  assert.deepEqual(translationEvent({ type: 'error', error: { message: 'provider secret' } }),
    { type: 'error', code: 'live_translation.provider_error' })
  assert.equal(translationEvent({ type: 'session.output_audio.delta', delta: 'ignored' }), null)
})

test('rolling subtitles stay bounded', () => {
  const result = appendTranscript('first sentence. ', 'second sentence', 20)
  assert.equal(result, 'second sentence')
  assert.equal(appendTranscript('hello', ' world', 20), 'hello world')
})

test('the sidecar clones only its supplied remote track and uses the ephemeral key', async t => {
  const originalMediaStream = globalThis.MediaStream
  globalThis.MediaStream = class { constructor(tracks) { this.tracks = tracks } }
  t.after(() => { globalThis.MediaStream = originalMediaStream })

  let originalStopped = false, cloneStopped = false, request
  const remoteTrack = {
    kind: 'audio', readyState: 'live',
    clone: () => ({ kind: 'audio', readyState: 'live', stop: () => { cloneStopped = true } }),
    stop: () => { originalStopped = true },
  }
  class FakeChannel {
    constructor() { this.readyState = 'open'; this.sent = [] }
    send(value) { this.sent.push(value) }
    close() { this.readyState = 'closed' }
  }
  class FakePeerConnection {
    constructor() { this.iceGatheringState = 'complete'; this.connectionState = 'new'; this.channel = new FakeChannel() }
    addTrack(track, stream) { this.added = { track, stream } }
    createDataChannel() { return this.channel }
    async createOffer() { return { type: 'offer', sdp: 'v=0\r\no=test' } }
    async setLocalDescription(value) { this.localDescription = value }
    async setRemoteDescription(value) { this.remoteDescription = value }
    close() { this.connectionState = 'closed' }
  }
  const fetch = async (url, options) => {
    request = { url, options }
    return { ok: true, text: async () => 'v=0\r\no=answer' }
  }
  const events = []
  const session = new LiveTranslation(event => events.push(event), {
    RTCPeerConnection: FakePeerConnection, fetch,
  })
  await session.start(remoteTrack, 'ek-browser-ephemeral')
  assert.equal(request.url, OPENAI_TRANSLATION_CALLS_URL)
  assert.equal(request.options.headers.Authorization, 'Bearer ek-browser-ephemeral')
  assert.notEqual(session.pc.added.track, remoteTrack)
  session.channel.onmessage({ data: JSON.stringify({
    type: 'session.input_transcript.delta', delta: 'source',
  }) })
  assert.deepEqual(events.at(-1), { type: 'source', delta: 'source' })
  const channel = session.channel
  session.stop()
  assert.equal(originalStopped, false)
  assert.equal(cloneStopped, true)
  channel.onmessage({ data: JSON.stringify({ type: 'session.closed' }) })
  assert.equal(session.pc, null)
})
