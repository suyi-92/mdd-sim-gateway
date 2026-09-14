import React, { useEffect } from 'react'
import { deviceTitle } from '../deviceNames.js'
import { useI18n } from '../i18n.jsx'

// Per-page SIM/line picker for multi-SIM setups. Labels each line with the physical reader
// it currently occupies (from the detected-cards state) so it's clear which reader's engine
// (docker container) will handle calls/SMS/logs. Calls and Messages provide independent
// selected-line state so switching a messaging line cannot tear down an active phone session.
//
// Only lines whose physical reader is currently PRESENT are listed — a provisioned line
// whose reader/card is unplugged is dropped from the dropdown (its config stays under SIM
// Config and it reappears when the reader returns).
export default function SimSelector({ instances = [], cards = [], devices = [], selected, setSelected, label = 'Active SIM / line' }) {
  const { t } = useI18n()
  // A modem can expose its physical SIM through ModemManager while its optional VoWiFi
  // PC/SC bridge has no card. Treat either source as live so 4G-only calls/SMS history
  // remains selectable.
  const readerFor = (i) => cards.find((c) => c.present &&
    (String(c.matched) === String(i.id) || (c.iccid && c.iccid === i.iccid)))
  const deviceFor = (i) => devices.find((d) => d.present &&
    String(d.instance_id || '') === String(i.id))
  const sourceFor = (i) => readerFor(i) || deviceFor(i)
  const live = instances.filter((i) => sourceFor(i))
  const lineName = (i) => i.carrier || i.name || [i.mcc, i.mnc].filter(Boolean).join('-') || t('Unknown SIM')
  const numberTail = (i) => String(i.msisdn || '').replace(/\D/g, '').slice(-4)

  // Calls/Messages own their useful default: choose the first live line here instead of in
  // App, where a global default could leak an unrelated line into a device's SIM tab.
  const id = selected?.id
  useEffect(() => {
    if (!id || !live.some((i) => i.id === id)) setSelected(live[0]?.id || null)
  }, [id, live.map((i) => i.id).join(',')])  // eslint-disable-line react-hooks/exhaustive-deps

  if (!live.length) return null
  return (
    <div className="card u-line-selector">
      <span className="u-line-selector-label">{t(label)}</span>
      <select value={id || ''} onChange={(e) => setSelected(e.target.value)}>
        {!id && <option value="">{t('— select —')}</option>}
        {live.map((i) => {
          const c = sourceFor(i)
          const physical = deviceFor(i) || c
          const tail = numberTail(i)
          const statusLabel = i.status?.presentation?.label || i.status?.label
          const st = statusLabel ? ` — ${t(statusLabel)}` : ''
          return <option key={i.id} value={i.id}>{deviceTitle(physical, 0, t)} · {lineName(i)}{tail ? ` · ••••${tail}` : ''}{st}</option>
        })}
      </select>
      {live.length === 1 && <span className="u-line-selector-tail">{t('only line')}</span>}
    </div>
  )
}
