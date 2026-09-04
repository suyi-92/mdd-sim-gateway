import React from 'react'

export const CALL_KEYS = [
  ['1', ''], ['2', 'ABC'], ['3', 'DEF'],
  ['4', 'GHI'], ['5', 'JKL'], ['6', 'MNO'],
  ['7', 'PQRS'], ['8', 'TUV'], ['9', 'WXYZ'],
  ['*', ''], ['0', ''], ['#', ''],
]

function ActionButton({ icon, label, tone = 'neutral', onClick, active = false, pulse = false }) {
  return (
    <div className="u-call-action">
      <button type="button" className={`u-call-action-button is-${tone}${active ? ' is-active' : ''}${pulse ? ' is-pulsing' : ''}`}
        aria-label={label} aria-pressed={active || undefined} onClick={onClick}>
        {icon}
      </button>
      <span>{label}</span>
    </div>
  )
}

function DtmfKeypad({ value, onTone, t }) {
  return (
    <div className="u-call-dtmf">
      <div className="u-call-dtmf-display mono" aria-live="polite" aria-label={t('Entered tones')}>
        {value || t('Type or tap keys')}
      </div>
      <div className="u-call-dtmf-grid" aria-label={t('Keypad')}>
        {CALL_KEYS.map(([key, letters]) => (
          <button type="button" key={key} onClick={() => onTone?.(key)} aria-label={key}>
            <b>{key}</b>
            <small>{letters || '\u00a0'}</small>
          </button>
        ))}
      </div>
    </div>
  )
}

export default function CallSurface({
  call,
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
        <div className="u-call-number mono">{call.number || t('Unknown')}</div>
        <div className="u-call-line">{line || t('VoWiFi line')}</div>
        {state === 'active' && <div className="u-call-duration mono">{duration}</div>}
      </div>

      {state === 'active' && canDtmf && keypad && (
        <DtmfKeypad value={dtmfSeq} onTone={onTone} t={t} />
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
            <ActionButton icon={muted ? '🔇' : '🎙'} label={t(muted ? 'Unmute' : 'Mute')}
              tone="primary" active={muted} onClick={onToggleMute} />
            {canDtmf && <ActionButton icon="⌨" label={t('Keypad')} tone="violet"
              active={keypad} onClick={onToggleKeypad} />}
            <ActionButton icon="✕" label={t('Hangup')} tone="danger" onClick={onHangup} />
          </>
        )}
      </div>

      {state === 'ended' && <div className="u-call-ended">{t('Call ended')}</div>}
    </section>
  )
}
