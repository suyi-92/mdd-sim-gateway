import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { Softphone } from '../src/softphone.js'
import JsSIP from 'jssip'

const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
const trackStream = () => {
  const track = { stops: 0, stop() { this.stops += 1 } }
  return { stream: { getTracks: () => [track] }, track }
}

test('merged phone preserves cancellation, direct SDP and relay audio paths', async t => {
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  t.after(() => {
    if (originalNavigator) Object.defineProperty(globalThis, 'navigator', originalNavigator)
    else delete globalThis.navigator
  })

  await t.test('registration prefers same-origin SIP proxy and reports invalid provisioning', () => {
    const originals = { UA: JsSIP.UA, WebSocketInterface: JsSIP.WebSocketInterface, location: globalThis.location }
    const urls = [], events = []
    JsSIP.WebSocketInterface = class { constructor(url) { urls.push(url) } }
    JsSIP.UA = class extends EventEmitter { start() {}; stop() {} }
    globalThis.location = { protocol: 'https:', host: 'fixture.invalid:8443' }
    try {
      const phone = new Softphone((...event) => events.push(event))
      assert.equal(phone.start({ username: 'fixture', password: 'fixture', ws_path: '/api/instances/line/softphone/ws', ws_port: 8109 }, 'old.invalid'), true)
      assert.equal(urls[0], 'wss://fixture.invalid:8443/api/instances/line/softphone/ws')
      assert.equal(phone.start({ username: 'fixture', password: 'fixture', ws_port: 8109 }, 'old.invalid'), true)
      assert.equal(urls[1], 'wss://old.invalid:8109/ws')
      assert.equal(phone._dead, false, 'restarting the same wrapper must permit new calls')
      assert.equal(phone.start({ ws_port: -1 }, 'old.invalid'), false)
      assert.ok(events.some(([event, reason]) => event === 'regfail' && reason === 'invalid provisioning'))
    } finally {
      JsSIP.UA = originals.UA; JsSIP.WebSocketInterface = originals.WebSocketInterface
      if (originals.location === undefined) delete globalThis.location
      else globalThis.location = originals.location
    }
  })

  await t.test('hangup during microphone permission never places the delayed call', async () => {
    const permission = deferred(), media = trackStream(), events = [], dialled = []
    Object.defineProperty(globalThis, 'navigator', { configurable: true,
      value: { mediaDevices: { getUserMedia: () => permission.promise } } })
    const phone = new Softphone((...event) => events.push(event))
    phone.ua = { configuration: { uri: { host: 'fixture.invalid' } }, call: (...args) => dialled.push(args) }
    const dial = phone.call('10086')
    await Promise.resolve()
    phone.hangup()
    permission.resolve(media.stream)
    await dial
    assert.equal(dialled.length, 0)
    assert.ok(media.track.stops > 0)
    assert.equal(phone._local, null)
    assert.equal(events.some(([event]) => event === 'mediafallback'), false)
  })

  await t.test('an obsolete permission result cannot replace a newer call stream', async () => {
    const oldPermission = deferred(), newPermission = deferred()
    const oldMedia = trackStream(), newMedia = trackStream()
    let requests = 0
    Object.defineProperty(globalThis, 'navigator', { configurable: true,
      value: { mediaDevices: { getUserMedia: () => (++requests === 1 ? oldPermission : newPermission).promise } } })
    const phone = new Softphone(() => {})
    const oldAcquire = phone._acquireLocal()
    phone.hangup()
    const newAcquire = phone._acquireLocal()
    newPermission.resolve(newMedia.stream)
    await newAcquire
    oldPermission.resolve(oldMedia.stream)
    await oldAcquire
    assert.equal(phone._local.stream, newMedia.stream)
    assert.equal(newMedia.track.stops, 0)
    assert.ok(oldMedia.track.stops > 0)
    phone.hangup()
    assert.equal(newMedia.track.stops, 1)
  })

  await t.test('decline during answer permission never answers a terminated session', async () => {
    const permission = deferred(), media = trackStream()
    Object.defineProperty(globalThis, 'navigator', { configurable: true,
      value: { mediaDevices: { getUserMedia: () => permission.promise } } })
    const phone = new Softphone(() => {})
    let answers = 0, rejected = 0
    phone.session = { direction: 'incoming', isEstablished: () => false,
      answer() { answers += 1 }, terminate(options) { assert.equal(options.status_code, 603); rejected += 1 } }
    const answer = phone.answer()
    await Promise.resolve()
    phone.reject()
    permission.resolve(media.stream)
    await answer
    assert.equal(answers, 0)
    assert.equal(rejected, 1)
    assert.ok(media.track.stops > 0)
  })

  await t.test('direct calls rewrite Fake-IP while relay calls preserve gathered SDP', () => {
    const original = 'v=0\r\nm=audio 6000 UDP/TLS/RTP/SAVPF 0\r\nc=IN IP4 198.18.0.1\r\na=candidate:1 1 udp 2122260223 198.18.0.1 6000 typ host\r\na=candidate:2 1 udp 2122260222 192.0.2.20 6001 typ host\r\n'
    for (const mode of ['direct', 'relay']) {
      const phone = new Softphone(() => {})
      phone.prov = { media_mode: mode }
      phone.mediaHost = '192.0.2.20'
      const session = new EventEmitter()
      session.direction = 'outgoing'
      phone.handleSession({ session })
      const event = { originator: 'local', sdp: original }
      session.emit('sdp', event)
      if (mode === 'relay') assert.equal(event.sdp, original)
      else {
        assert.equal(event.sdp.includes('198.18.0.1'), false)
        assert.ok(event.sdp.includes('c=IN IP4 192.0.2.20'))
      }
      let translationStops = 0
      phone._translation = { stop() { translationStops += 1 } }
      const media = trackStream()
      phone._local = { stream: media.stream }
      session.emit('ended', { cause: 'Normal' })
      assert.equal(translationStops, 1)
      assert.equal(media.track.stops, 1)
      assert.equal(phone.session, null)
    }
  })
})
