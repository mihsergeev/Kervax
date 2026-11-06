import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ApiError, previewServerDelete, type ServerDeletePreview } from './api'
import { useI18n } from './i18n'
import { useEscape } from './useEscape'

export function ServerDeleteDialog({ ids, message, onClose, onDelete, onUnauthorized }: {
  ids: number[]
  message: string
  onClose: () => void
  onDelete: (archive: ServerDeletePreview) => Promise<void>
  onUnauthorized: () => void
}) {
  const { t } = useI18n()
  const titleId = useId()
  const dialog = useRef<HTMLDialogElement>(null)
  const [preview, setPreview] = useState<ServerDeletePreview | null>(null)
  const [archive, setArchive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const repos = Object.values(preview ?? {}).flat()
  const close = () => { if (!busy) onClose() }
  useEscape(close)

  useEffect(() => {
    const element = dialog.current!
    element.showModal()
    return () => element.close()
  }, [])
  useEffect(() => {
    let active = true
    previewServerDelete(ids).then((data) => {
      if (active) setPreview(data)
    }).catch((e) => {
      if (!active) return
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setError(e instanceof Error ? e.message : t('Ошибка'))
    })
    return () => { active = false }
  }, [ids, onUnauthorized, t])

  async function confirm() {
    if (!preview || busy) return
    setBusy(true)
    setError('')
    try {
      await onDelete(archive ? preview : {})
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      if (e instanceof ApiError && e.status === 409) setPreview(null)
      setError(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  return createPortal(
    <dialog
      ref={dialog}
      className="card modal server-delete-dialog"
      aria-labelledby={titleId}
      onCancel={(e) => { e.preventDefault(); close() }}
    >
      <h3 id={titleId}>{message}</h3>
      {!preview && !error && <div className="muted" role="status">{t('Ищем связанные репозитории…')}</div>}
      {repos.length > 0 && (
        <>
          <label className="checkbox">
            <input type="checkbox" checked={archive} disabled={busy} onChange={(e) => setArchive(e.target.checked)} />
            {t('Перенести связанные репозитории в архив')}
          </label>
          <ul className="server-delete-repos">
            {repos.map((r) => (
              <li key={`${r.backup_server_id}:${r.repo}`}><strong>{r.repo}</strong> · {r.backup_server_name}</li>
            ))}
          </ul>
          <div className="muted">{t('Снапшоты останутся на бэкап-сервере. Алерты об отсутствии агента и просрочке для архива отключены.')}</div>
        </>
      )}
      {error && <div className="err" role="alert">{error}</div>}
      <div className="modal-actions">
        <button type="button" className="ghost" disabled={busy} onClick={close} autoFocus>{t('Отмена')}</button>
        <button type="button" className="ghost danger-btn" disabled={busy || !preview} onClick={confirm}>
          {archive ? t('Удалить и архивировать') : t('Удалить')}
        </button>
      </div>
    </dialog>,
    document.body,
  )
}
