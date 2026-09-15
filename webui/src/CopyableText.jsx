import React from 'react'
import { copyExactText } from './copyText.js'
import { useI18n } from './i18n.jsx'

export default function CopyableText({ value, showToast, className = '', children }) {
  const { t } = useI18n()
  const text = String(value ?? '')
  const copy = async event => {
    event.stopPropagation()
    const copied = await copyExactText(text)
    showToast?.(t(copied ? 'Copied' : 'Copy failed'))
  }

  return <button type="button" className={`u-copyable-text ${className}`.trim()}
    aria-label={t('Copy phone number')} title={t('Click to copy phone number')} onClick={copy}>
    {children ?? text}
  </button>
}
