import React, { useEffect, useRef } from 'react'
import CopyableText from '../CopyableText.jsx'
import { deviceTitle } from '../deviceNames.js'
import { useI18n } from '../i18n.jsx'
import { formatPhoneNumberDisplay } from '../phoneNumberDisplay.js'
import { communicationLineDetails } from '../simLineDetails.js'

// Per-page SIM/line picker for multi-SIM setups. Labels each line with the physical reader
// it currently occupies (from the detected-cards state) so it's clear which reader's engine
// (docker container) will handle calls/SMS/logs. Calls and Messages provide independent
// selected-line state so switching a messaging line cannot tear down an active phone session.
//
// Only lines whose physical reader is currently PRESENT are listed — a provisioned line
// whose reader/card is unplugged is dropped from the dropdown (its config stays under SIM
// Config and it reappears when the reader returns).
export default function SimSelector({ instances = [], cards = [], devices = [], selected, setSelected, label = 'Active SIM / line', showDetails = false, showToast }) {
  const { t, language } = useI18n()
  const selectedHardware = useRef('')
  // A modem can expose its physical SIM through ModemManager while its optional VoWiFi
  // PC/SC bridge has no card. Treat either source as live so 4G-only calls/SMS history
  // remains selectable.
  const readerFor = (i) => cards.find((c) => {
    if (!c.present) return false
    const owner = devices.find(d => d.id === c.hardware_id)
    if (owner && (!owner.present || String(owner.instance_id || '') !== String(i.id))) return false
    return String(c.matched) === String(i.id) || (c.iccid && c.iccid === i.iccid)
  })
  const deviceFor = (i) => devices.find((d) => d.present &&
    String(d.instance_id || '') === String(i.id))
  const sourceFor = (i) => readerFor(i) || deviceFor(i)
  const live = instances.filter((i) => sourceFor(i))
  // Keep the selector text exactly as it was in 1.9.4-vmware.2. The richer, full
  // identity belongs to the adjacent details and must not widen or unmask this control.
  const lineName = (i) => {
    const carrier = deviceFor(i)?.sim?.carrier
    return (carrier?.brand_source === 'esim_profile' && carrier.name)
      || i.carrier || i.name || [i.mcc, i.mnc].filter(Boolean).join('-') || t('Unknown SIM')
  }
  const numberTail = (i) => String(i.msisdn || '').replace(/\D/g, '').slice(-4)

  // Calls/Messages own their useful default: choose the first live line here instead of in
  // App, where a global default could leak an unrelated line into a device's SIM tab.
  const id = selected?.id
  useEffect(() => {
    if (!id || !live.some((i) => i.id === id)) {
      const replacement = live.find(i => deviceFor(i)?.id === selectedHardware.current)
      setSelected(replacement?.id || live[0]?.id || null)
    } else {
      selectedHardware.current = deviceFor(selected)?.id || readerFor(selected)?.hardware_id || ''
    }
  }, [id, live.map((i) => i.id).join(',')])  // eslint-disable-line react-hooks/exhaustive-deps

  if (!live.length) return null
  const current = live.find(i => String(i.id) === String(id)) || live[0]
  const currentDevice = deviceFor(current) || sourceFor(current)
  const details = showDetails ? communicationLineDetails(current, currentDevice, t, language) : null
  const detailFields = details ? [
    ['Carrier', details.carrier], ['Line name', details.line], ['Number', details.number, true],
    ['Number region', details.country], ['SIM home network', details.homeNetwork], ['Network route', details.networkRoute],
  ] : []
  return (
    <div className="card u-line-selector">
      <span className="u-line-selector-label">{t(label)}</span>
      <select value={id || ''} onChange={(e) => setSelected(e.target.value)}>
        {!id && <option value="">{t('— select —')}</option>}
        {live.map((i) => {
          const c = sourceFor(i)
          const physical = deviceFor(i) || c
          const tail = numberTail(i)
          const statusLabel = i.status?.state === 'STOPPED' && physical.device_type === 'modem'
            && physical.capabilities?.vowifi?.desired === false
            ? ['home', 'roaming', 'registered'].includes(physical.cellular?.registration)
              ? 'Cellular network registered' : 'VoWiFi is off'
            : i.status?.presentation?.label || i.status?.label
          const st = statusLabel ? ` — ${t(statusLabel)}` : ''
          return <option key={i.id} value={i.id}>{deviceTitle(physical, 0, t)} · {lineName(i)}{tail ? ` · ••••${tail}` : ''}{st}</option>
        })}
      </select>
      {live.length === 1 && <span className="u-line-selector-tail">{t('only line')}</span>}
      {details && <dl className="u-line-selector-meta" aria-label={t('Current line details')}>
        {detailFields.map(([name, value, copyable]) => <div key={name}><dt>{t(name)}</dt><dd title={copyable ? undefined : value}>
          {copyable && value !== t('Number unavailable')
            ? <CopyableText value={value} showToast={showToast}>{formatPhoneNumberDisplay(value)}</CopyableText>
            : value}
        </dd></div>)}
      </dl>}
    </div>
  )
}
