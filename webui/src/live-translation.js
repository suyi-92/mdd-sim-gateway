// Browser WebRTC sidecar for OpenAI Realtime Translation.
//
// The phone call keeps its own PeerConnection. This class clones only the remote party's
// decoded audio track into a second connection and emits source/Chinese transcript deltas.
// Closing or failing this sidecar never mutates the call, its sender, or its audio sink.

export const OPENAI_TRANSLATION_CALLS_URL = 'https://api.openai.com/v1/realtime/translations/calls'
export const MAX_TRANSCRIPT_CHARS = 2400

export function appendTranscript(current, delta, limit = MAX_TRANSCRIPT_CHARS) {
  const next = `${String(current || '')}${String(delta || '')}`
  if (next.length <= limit) return next
  const tail = next.slice(-limit)
  const boundary = tail.search(/[\s。！？.!?]/)
  return boundary >= 0 && boundary < 160 ? tail.slice(boundary + 1).trimStart() : tail
}

export function translationEvent(event) {
  if (!event || typeof event !== 'object') return null
  if (event.type === 'session.input_transcript.delta' && typeof event.delta === 'string') {
    return { type: 'source', delta: event.delta }
  }
  if (event.type === 'session.output_transcript.delta' && typeof event.delta === 'string') {
    return { type: 'target', delta: event.delta }
  }
  if (event.type === 'session.closed') return { type: 'closed' }
  if (event.type === 'error') return { type: 'error', code: 'live_translation.provider_error' }
  return null
}

function waitForIceGathering(pc, timeoutMs = 3000) {
  if (pc.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve) => {
    let timer
    const done = () => {
      clearTimeout(timer)
      pc.removeEventListener?.('icegatheringstatechange', changed)
      resolve()
    }
    const changed = () => { if (pc.iceGatheringState === 'complete') done() }
    pc.addEventListener?.('icegatheringstatechange', changed)
    timer = setTimeout(done, timeoutMs)
  })
}

export class LiveTranslation {
  constructor(onEvent, dependencies = {}) {
    this.onEvent = onEvent
    this.RTCPeerConnection = dependencies.RTCPeerConnection || globalThis.RTCPeerConnection
    this.fetch = dependencies.fetch || globalThis.fetch
    this.pc = null
    this.channel = null
    this.sourceTrack = null
    this.closing = false
    this.abort = null
    this.closeTimer = null
    this.connectTimer = null
  }

  emit(event) { try { this.onEvent?.(event) } catch {} }

  async start(remoteTrack, clientSecret) {
    if (this.pc) throw new Error('live_translation.already_running')
    if (!remoteTrack || remoteTrack.kind !== 'audio' || remoteTrack.readyState === 'ended') {
      throw new Error('live_translation.remote_audio_unavailable')
    }
    if (typeof this.RTCPeerConnection !== 'function' || typeof this.fetch !== 'function') {
      throw new Error('live_translation.browser_unsupported')
    }
    const secret = String(clientSecret || '')
    if (!secret) throw new Error('live_translation.not_configured')

    this.closing = false
    const pc = new this.RTCPeerConnection({ iceServers: [] })
    this.pc = pc
    this.sourceTrack = remoteTrack.clone()
    pc.addTrack(this.sourceTrack, new MediaStream([this.sourceTrack]))
    // Translation audio is intentionally not played: this feature is source + Chinese text
    // only. Disabling the received track also prevents an accidental future audio attachment.
    pc.ontrack = (event) => { if (event.track) event.track.enabled = false }
    pc.onconnectionstatechange = () => {
      if (this.closing) return
      if (pc.connectionState === 'failed') {
        this.emit({ type: 'error', code: 'live_translation.connection_failed' })
        this.stop()
      } else if (pc.connectionState === 'disconnected') {
        this.emit({ type: 'status', status: 'reconnecting' })
      } else if (pc.connectionState === 'connected') {
        clearTimeout(this.connectTimer)
        this.emit({ type: 'status', status: 'active' })
      }
    }

    const channel = pc.createDataChannel('oai-events')
    this.channel = channel
    channel.onopen = () => {
      clearTimeout(this.connectTimer)
      if (!this.closing) this.emit({ type: 'status', status: 'active' })
    }
    channel.onmessage = ({ data }) => {
      let parsed
      try { parsed = JSON.parse(data) } catch { return }
      const event = translationEvent(parsed)
      if (!event) return
      if (event.type === 'closed') {
        const expected = this.closing
        this.dispose()
        if (!expected) this.emit({ type: 'error', code: 'live_translation.connection_closed' })
        return
      }
      this.emit(event)
      if (event.type === 'error') this.stop()
    }
    channel.onerror = () => {
      if (!this.closing) {
        this.emit({ type: 'error', code: 'live_translation.connection_failed' })
        this.stop()
      }
    }
    channel.onclose = () => {
      const expected = this.closing
      this.dispose()
      if (!expected) this.emit({ type: 'error', code: 'live_translation.connection_closed' })
    }

    this.abort = new AbortController()
    let timedOut = false
    const requestTimer = setTimeout(() => { timedOut = true; this.abort?.abort() }, 15000)
    try {
      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)
      await waitForIceGathering(pc)
      const response = await this.fetch(OPENAI_TRANSLATION_CALLS_URL, {
        method: 'POST',
        headers: { Authorization: `Bearer ${secret}`, 'Content-Type': 'application/sdp' },
        body: pc.localDescription?.sdp || offer.sdp,
        signal: this.abort.signal,
      })
      if (!response.ok) throw new Error('live_translation.connection_failed')
      const answer = await response.text()
      if (!answer.startsWith('v=')) throw new Error('live_translation.invalid_response')
      await pc.setRemoteDescription({ type: 'answer', sdp: answer })
      if (pc.connectionState !== 'connected' && channel.readyState !== 'open') {
        this.connectTimer = setTimeout(() => {
          if (this.closing || pc.connectionState === 'connected' || channel.readyState === 'open') return
          this.emit({ type: 'error', code: 'live_translation.connection_failed' })
          this.stop()
        }, 12000)
      }
    } catch (error) {
      const code = error?.name === 'AbortError'
        ? (timedOut ? 'live_translation.connection_failed' : 'live_translation.connection_closed')
        : String(error?.message || '').startsWith('live_translation.')
          ? error.message : 'live_translation.connection_failed'
      this.stop()
      throw new Error(code)
    } finally { clearTimeout(requestTimer) }
  }

  stop() {
    if (this.closing) return
    this.closing = true
    try { this.abort?.abort() } catch {}
    try { this.sourceTrack?.stop() } catch {}
    try {
      if (this.channel?.readyState === 'open') {
        this.channel.send(JSON.stringify({ type: 'session.close' }))
        this.closeTimer = setTimeout(() => this.dispose(), 1500)
        return
      }
    } catch {}
    this.dispose()
  }

  dispose() {
    clearTimeout(this.closeTimer)
    clearTimeout(this.connectTimer)
    this.closeTimer = null
    this.connectTimer = null
    const channel = this.channel
    if (channel) channel.onopen = channel.onmessage = channel.onerror = channel.onclose = null
    try { this.sourceTrack?.stop() } catch {}
    try { channel?.close() } catch {}
    try { this.pc?.close() } catch {}
    this.abort = null
    this.sourceTrack = null
    this.channel = null
    this.pc = null
  }
}
