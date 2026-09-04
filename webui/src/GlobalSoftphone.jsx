import React, { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from './api.js'
import { Softphone as Phone } from './softphone.js'
import CallSurface from './CallSurface.jsx'
import { useI18n } from './i18n.jsx'

// Keep incoming-call registration independent of the page the administrator happens to be
// viewing. The Calls page owns its selected line (it needs the same Phone for outbound calls),
// while this hub owns every other enabled line. That gives every line exactly one browser
// Contact and makes a call that was held by the engine appear immediately after sign-in.
export default function GlobalSoftphone({
  instances,
  excludedId,
  showToast,
  embedded = false,
  onIncoming,
  onCallChange,
  onOpenCalls,
}) {
  const { t } = useI18n()
  const phones = useRef(new Map())
  const wanted = useRef(new Set())
  const callRef = useRef(null)
  const clearTimer = useRef(null)
  const [call, setCallState] = useState(null)
  const [muted, setMuted] = useState(false)
  const [keypad, setKeypad] = useState(false)
  const [dtmfSeq, setDtmfSeq] = useState('')
  const [duration, setDuration] = useState(0)

  const setCall = (next) => {
    callRef.current = typeof next === 'function' ? next(callRef.current) : next
    setCallState(callRef.current)
  }
  const finishCall = (id, endCause) => {
    setCall((current) => current?.id === id
      ? { ...current, state: 'ended', endCause: endCause || current.endCause } : current)
    setMuted(false)
    setKeypad(false)
    setDtmfSeq('')
    // JsSIP normally emits ended after a local reject/hangup, but the UI must also recover
    // when that terminal event is lost during a websocket transition.
    clearTimeout(clearTimer.current)
    clearTimer.current = setTimeout(() => {
      setCall((current) => current?.id === id && current.state === 'ended' ? null : current)
    }, 1800)
  }

  // Only identity and display name matter here. Periodic status refreshes replace the instance
  // objects, but must not tear every SIP registration down and build it again.
  const lineKey = useMemo(() => instances.map((line) => `${line.id}:${line.name || ''}`).sort().join('|'), [instances])

  useEffect(() => {
    const desired = new Set(instances
      .map((line) => String(line.id))
      .filter((id) => excludedId === null || excludedId === undefined || id !== String(excludedId)))
    wanted.current = desired

    for (const [id, phone] of phones.current.entries()) {
      if (!desired.has(id) && callRef.current?.id !== id) {
        phone.stop()
        phones.current.delete(id)
      }
    }

    for (const line of instances) {
      const id = String(line.id)
      if (!desired.has(id) || phones.current.has(id)) continue
      api.softphone(id).then((prov) => {
        if (!prov?.enabled || !wanted.current.has(id) || phones.current.has(id)) return
        let phone
        const onEvent = (type, data) => {
          if (type === 'incoming') {
            // Different lines can ring at the same instant. Once one call owns the browser's
            // microphone, reject a second one as busy instead of replacing the visible call.
            if (callRef.current && callRef.current.id !== id && callRef.current.state !== 'ended') {
              phone.rejectBusy()
              return
            }
            clearTimeout(clearTimer.current)
            setMuted(false)
            setKeypad(false)
            setDtmfSeq('')
            setCall({ id, line: line.name || id, number: data?.from || t('Unknown'), state: 'incoming' })
            onIncoming?.(id)
          } else if (type === 'active') {
            setKeypad(true)
            setDtmfSeq('')
            setCall((current) => current?.id === id
              ? { ...current, state: 'active', startedAt: current.startedAt || Date.now() } : current)
          } else if (type === 'ended' || type === 'failed') {
            if (callRef.current?.id !== id) return
            finishCall(id, data?.cause)
          } else if (type === 'audioblocked') {
            showToast?.(t('Browser blocked call audio. Click the page once and try again.'))
          }
        }
        phone = new Phone(onEvent, null)
        phones.current.set(id, phone)
        phone.start(prov, prov.host || location.hostname)
      }).catch(() => {})
    }
  }, [lineKey, excludedId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => {
    clearTimeout(clearTimer.current)
    for (const phone of phones.current.values()) phone.stop()
    phones.current.clear()
  }, [])

  useEffect(() => { onCallChange?.(call) }, [call, onCallChange])

  // Once a global call handed its line to the persistent Calls page, retain its Phone only
  // for the lifetime of that call. Afterwards the page resumes sole ownership of the line.
  useEffect(() => {
    if (call) return
    for (const [id, phone] of phones.current.entries()) {
      if (!wanted.current.has(id)) {
        phone.stop()
        phones.current.delete(id)
      }
    }
  }, [call, excludedId, lineKey])

  useEffect(() => {
    if (call?.state !== 'active' || !call.startedAt) { setDuration(0); return }
    const timer = setInterval(() => setDuration(Math.floor((Date.now() - call.startedAt) / 1000)), 500)
    return () => clearInterval(timer)
  }, [call?.state, call?.startedAt])

  useEffect(() => {
    if (!(keypad && call?.state === 'active')) return undefined
    const onKey = (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey || !/^[0-9*#]$/.test(event.key)) return
      event.preventDefault()
      const phone = phones.current.get(call.id)
      phone?.sendDTMF(event.key)
      setDtmfSeq((value) => (value + event.key).slice(-32))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [keypad, call?.state, call?.id])

  if (!call) return null
  const phone = phones.current.get(call.id)
  const answer = () => { phone?.unlockAudio(); phone?.answer() }
  const decline = () => {
    phone?.reject(); finishCall(call.id, 'Rejected')
  }
  const hangup = () => { phone?.hangup(); finishCall(call.id) }
  const toggleMute = () => {
    const next = !muted
    setMuted(next)
    phone?.setMuted(next)
  }
  const clock = `${String(Math.floor(duration / 60)).padStart(2, '0')}:${String(duration % 60).padStart(2, '0')}`

  const pressTone = (tone) => {
    phone?.sendDTMF(tone)
    setDtmfSeq((value) => (value + tone).slice(-32))
  }
  const surface = <CallSurface call={call} line={call.line} duration={clock} muted={muted}
    keypad={keypad} dtmfSeq={dtmfSeq} embedded={embedded}
    onAnswer={answer} onDecline={decline} onHangup={hangup} onToggleMute={toggleMute}
    onToggleKeypad={() => setKeypad((value) => !value)} onTone={pressTone}
    onOpenCalls={onOpenCalls} t={t} />
  const slot = embedded ? document.getElementById('u-global-call-slot') : null
  return slot ? createPortal(surface, slot) : <div className="u-call-dock-shell">{surface}</div>
}
