import React from 'react'
import { LiveSubtitlePanel, subtitleButtonLabel } from './LiveSubtitles.jsx'
import { formatPhoneNumberDisplay } from './phoneNumberDisplay.js'

export const CALL_KEYS = [
  ['1', ''], ['2', 'ABC'], ['3', 'DEF'],
  ['4', 'GHI'], ['5', 'JKL'], ['6', 'MNO'],
  ['7', 'PQRS'], ['8', 'TUV'], ['9', 'WXYZ'],
  ['*', ''], ['0', ''], ['#', ''],
]

function ActionButton({ icon, label, tone = 'neutral', onClick, active = false, pulse = false,
  disabled = false }) {
  return (
    <div className="u-call-action">
      <button type="button" className={`u-call-action-button is-${tone}${active ? ' is-active' : ''}${pulse ? ' is-pulsing' : ''}`}
        aria-label={label} aria-pressed={active || undefined} disabled={disabled} onClick={onClick}>
        {icon}
      </button>
      <span>{label}</span>
    </div>
  )
}

export function DialKeypad({ onKey, t }) {
  return (
    <div className="u-call-dtmf-grid" aria-label={t('Keypad')}>
      {CALL_KEYS.map(([key, letters]) => (
        <button type="button" key={key} onClick={() => onKey?.(key)} aria-label={key}>
          <b>{key}</b>
          <small aria-hidden="true">{letters || '\u00a0'}</small>
        </button>
      ))}
    </div>
  )
}

export function DtmfKeypad({ value, onTone, t }) {
  return (
    <div className="u-call-dtmf">
      <div className="u-call-dtmf-display mono" aria-live="polite" aria-label={t('Entered tones')}>
        {value}
      </div>
      <DialKeypad onKey={onTone} t={t} />
    </div>
  )
}

export default function CallSurface({
  call,
  callerName = '',
  line,
  duration = '00:00',
  muted = false,
  keypad = false,
  dtmfSeq = '',
  canDtmf = true,
  embedded = false,
  onAnswer,
  onDecline,
  onHangup,
  onToggleMute,
  onToggleKeypad,
  subtitles,
  onToggleSubtitles,
  onTone,
  onOpenCalls,
  t = (value) => value,
}) {
  if (!call) return null
  const state = call.state || 'incoming'
  const status = state === 'incoming' ? t('Incoming call')
    : state === 'active' ? t('Connected')
      : state === 'ringing' ? t('Ringing')
        : state === 'calling' ? t('Dialing') : t('Call ended')

  return (
    <section className={`u-call-surface ${embedded ? 'is-embedded' : 'is-floating'} is-${state}`}
      aria-live="polite" aria-label={t('Call controls')}>
      <div className="u-call-surface-head">
        <div className="u-call-state"><i />{status}</div>
        {!embedded && onOpenCalls && (
          <button type="button" className="u-call-open" onClick={onOpenCalls}>{t('Open Calls')}</button>
        )}
      </div>

      <div className="u-call-identity">
        <div className="u-call-avatar" aria-hidden="true">☎</div>
        <div className={`u-call-number${callerName ? '' : ' mono'}`}>{callerName || (call.number ? formatPhoneNumberDisplay(call.number) : t('Unknown'))}</div>
        {callerName && <div className="mono">{formatPhoneNumberDisplay(call.number)}</div>}
        <div className="u-call-line">{line || t('VoWiFi line')}</div>
        {state === 'active' && <div className="u-call-duration mono">{duration}</div>}
        {call.listenOnly && state !== 'ended' && <div role="status" style={{ fontSize: 12, color: '#f59e0b', marginTop: 6 }}>{t('Listen only · the other side cannot hear you')}</div>}
      </div>

      {state === 'active' && canDtmf && keypad && (
        <DtmfKeypad value={dtmfSeq} onTone={onTone} t={t} />
      )}

      {state === 'active' && subtitles?.phase !== 'idle' && (
        <LiveSubtitlePanel subtitles={subtitles} t={t} compact={!embedded} />
      )}

      <div className="u-call-actions">
        {state === 'incoming' && (
          <>
            <ActionButton icon="✕" label={t('Decline')} tone="danger" onClick={onDecline} />
            <ActionButton icon="☎" label={t('Answer')} tone="success" onClick={onAnswer} pulse />
          </>
        )}
        {(state === 'calling' || state === 'ringing') && (
          <ActionButton icon="✕" label={t('Hangup')} tone="danger" onClick={onHangup} />
        )}
        {state === 'active' && (
          <>
            <ActionButton icon={call.listenOnly ? '🚫' : muted ? '🔇' : '🎙'} label={t(call.listenOnly ? 'No mic' : muted ? 'Unmute' : 'Mute')}
              tone="primary" active={muted} disabled={Boolean(call.listenOnly)} onClick={onToggleMute} />
            {canDtmf && <ActionButton icon="⌨" label={t('Keypad')} tone="violet"
              active={keypad} onClick={onToggleKeypad} />}
            {subtitles?.available && <ActionButton icon="文" label={subtitleButtonLabel(subtitles, t)}
              tone="primary" active={['connecting', 'active', 'reconnecting'].includes(subtitles.phase)}
              onClick={onToggleSubtitles} />}
            <ActionButton icon="✕" label={t('Hangup')} tone="danger" onClick={onHangup} />
          </>
        )}
      </div>

      {state === 'ended' && <div className="u-call-ended">{t('Call ended')}</div>}
    </section>
  )
}
