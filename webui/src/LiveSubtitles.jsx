import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { appendTranscript } from './live-translation.js'

const EMPTY = { phase: 'idle', source: '', target: '', error: '' }

export function useLiveSubtitles(getPhone, active) {
  const [capability, setCapability] = useState(null)
  const [view, setView] = useState(EMPTY)
  const operation = useRef(0)

  const refreshCapability = useCallback(() => {
    let cancelled = false
    api.liveTranslationStatus()
      .then((value) => { if (!cancelled) setCapability(value) })
      .catch(() => { if (!cancelled) setCapability((current) => current || {
        enabled: false, configured: false, unavailable: true,
      }) })
    return () => { cancelled = true }
  }, [])

  useEffect(refreshCapability, [refreshCapability])
  useEffect(() => {
    if (!active) return undefined
    return refreshCapability()
  }, [active, refreshCapability])

  const onTranslationEvent = useCallback((event) => {
    if (event.type === 'source') {
      setView((current) => ({ ...current, source: appendTranscript(current.source, event.delta) }))
    } else if (event.type === 'target') {
      setView((current) => ({ ...current, target: appendTranscript(current.target, event.delta) }))
    } else if (event.type === 'status') {
      setView((current) => ({ ...current, phase: event.status === 'reconnecting' ? 'reconnecting' : 'active', error: '' }))
    } else if (event.type === 'error') {
      setView((current) => ({ ...current, phase: 'error', error: event.code || 'live_translation.connection_failed' }))
    }
  }, [])

  const stop = useCallback((clear = false) => {
    operation.current += 1
    try { getPhone()?.stopLiveTranslation() } catch {}
    setView((current) => clear ? EMPTY : { ...current, phase: 'stopped', error: '' })
  }, [getPhone])

  const toggle = useCallback(async () => {
    if (['connecting', 'active', 'reconnecting'].includes(view.phase)) {
      stop(false)
      return
    }
    const phone = getPhone()
    if (!phone) {
      setView({ phase: 'error', source: '', target: '', error: 'live_translation.remote_audio_unavailable' })
      return
    }
    const currentOperation = ++operation.current
    setView({ phase: 'connecting', source: '', target: '', error: '' })
    try {
      const credential = await api.createLiveTranslationSession()
      if (operation.current !== currentOperation) return
      await phone.startLiveTranslation(credential.value, (event) => {
        if (operation.current === currentOperation) onTranslationEvent(event)
      })
      if (operation.current !== currentOperation) phone.stopLiveTranslation()
    } catch (error) {
      if (operation.current !== currentOperation) return
      const code = String(error?.message || '')
      setView({ phase: 'error', source: '', target: '',
        error: code.startsWith('live_translation.') ? code : 'live_translation.connection_failed' })
    }
  }, [getPhone, onTranslationEvent, stop, view.phase])

  useEffect(() => {
    if (active) return undefined
    operation.current += 1
    try { getPhone()?.stopLiveTranslation() } catch {}
    setView(EMPTY)
    return undefined
  }, [active, getPhone])

  useEffect(() => () => {
    operation.current += 1
    try { getPhone()?.stopLiveTranslation() } catch {}
  }, [getPhone])

  return {
    ...view,
    available: Boolean(capability?.enabled && capability?.configured),
    capabilityLoaded: capability !== null,
    toggle,
  }
}

export function LiveSubtitlePanel({ subtitles, t = (value) => value, compact = false }) {
  const sourceRef = useRef(null)
  const targetRef = useRef(null)
  useEffect(() => {
    if (sourceRef.current) sourceRef.current.scrollTop = sourceRef.current.scrollHeight
  }, [subtitles?.source])
  useEffect(() => {
    if (targetRef.current) targetRef.current.scrollTop = targetRef.current.scrollHeight
  }, [subtitles?.target])
  if (!subtitles || subtitles.phase === 'idle') return null
  const waiting = subtitles.phase === 'connecting' ? 'Starting live subtitles…'
    : subtitles.phase === 'reconnecting' ? 'Live subtitles are reconnecting…'
      : subtitles.phase === 'stopped' ? 'Live subtitles stopped'
        : 'Waiting for the other person to speak…'
  return <div className={`u-live-subtitles${compact ? ' is-compact' : ''}`}>
    <div className="u-live-subtitle-head"><b>{t('AI live subtitles')}</b><span>{t(subtitles.phase === 'active' ? 'Listening'
      : subtitles.phase === 'error' ? 'Unavailable' : subtitles.phase === 'stopped' ? 'Stopped'
        : subtitles.phase === 'reconnecting' ? 'Reconnecting…' : 'Starting…')}</span></div>
    <div className="u-live-subtitle-row"><span>{t('Original')}</span><p ref={sourceRef}>{subtitles.source || t(waiting)}</p></div>
    <div className="u-live-subtitle-row is-target" aria-live="polite" aria-atomic="false"><span>{t('Chinese')}</span><p ref={targetRef}>{subtitles.target || '—'}</p></div>
    {subtitles.error && <p className="u-live-subtitle-error" role="alert">{t(subtitles.error)}</p>}
  </div>
}

export function subtitleButtonLabel(subtitles, t = (value) => value) {
  if (['connecting', 'active', 'reconnecting'].includes(subtitles?.phase)) return t('Stop subtitles')
  if (subtitles?.phase === 'error') return t('Retry subtitles')
  return t('Subtitles')
}
