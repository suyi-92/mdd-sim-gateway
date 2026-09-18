import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useI18n } from '../i18n'

export function BackupRecord({ item, details, disabled, restoring, onRestore }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState('')
  const [failed, setFailed] = useState(false)
  const exporting = useRef(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const download = async () => {
    if (exporting.current) return
    exporting.current = true
    setBusy(true); setFailed(false); setFeedback('Preparing download…')
    try {
      const blob = await api.exportBackup(item.name)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = item.name.replace(/\.tar\.gz$/, '') + '.mddbackup'
      document.body.appendChild(link); link.click(); link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 60000)
      if (mounted.current) setFeedback('Download started')
    } catch (error) { if (mounted.current) { setFailed(true); setFeedback(error.message) } }
    finally { exporting.current = false; if (mounted.current) setBusy(false) }
  }
  return <div className="u-backup-row">
    <div className="u-backup-copy"><b className="mono" title={item.name}>{item.name}</b><span>{details}</span></div>
    <div className="u-backup-actions">
      <span role="status" className={`u-backup-feedback ${failed ? 'u-error' : 'u-muted'}`} title={t(feedback)}>{t(feedback)}</span>
      <button className="btn btn-ghost u-backup-export" disabled={disabled || busy} onClick={download}>{t('Export')}</button>
      <button className="btn btn-ghost u-backup-restore" disabled={disabled || busy} onClick={onRestore}>{t(restoring ? 'Restoring…' : 'Restore')}</button>
    </div>
  </div>
}

export function BackupImport({ disabled, onImported }) {
  const { t } = useI18n()
  const [file, setFile] = useState(null)
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState('')
  const [failed, setFailed] = useState(false)
  const uploading = useRef(false)
  const input = useRef(null)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const upload = async () => {
    if (!file || uploading.current) return
    if (file.size > 1024 ** 3) { setFailed(true); setFeedback('backup.transfer.too_large'); return }
    uploading.current = true
    setBusy(true); setFailed(false); setFeedback('Uploading and checking…')
    try {
      await api.importBackup(file)
      if (mounted.current) {
        setFeedback('Imported. Select Restore below to apply it.')
        setFile(null)
        if (input.current) input.current.value = ''
      }
      await onImported()
    } catch (error) { if (mounted.current) { setFailed(true); setFeedback(error.message) } }
    finally { uploading.current = false; if (mounted.current) setBusy(false) }
  }
  return <div className="u-backup-import">
    <label>{t('Migration package')}<input ref={input} type="file" accept=".mddbackup" disabled={disabled || busy}
      onChange={event => { setFile(event.target.files?.[0] || null); setFeedback(''); setFailed(false) }} /></label>
    <span role="status" className={`u-backup-feedback ${failed ? 'u-error' : 'u-muted'}`} title={t(feedback)}>{t(feedback)}</span>
    <button className="btn btn-ghost" disabled={disabled || busy || !file} onClick={upload}>{t('Import backup')}</button>
  </div>
}
