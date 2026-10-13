import { useState } from 'react'
import { useI18n } from './i18n'

// Заметка в карточке сервера или монитора: что известно и кто в курсе ("диск под замену,
// тикет у хостера"). Ее же видно в списке, в "Что сломано" и строкой в алертах.
export function NoteBar({
  note,
  by,
  at,
  canEdit,
  onSave,
}: {
  note?: string
  by?: string
  at?: string | null
  canEdit: boolean
  onSave: (text: string) => Promise<void>
}) {
  const { t } = useI18n()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const text = (note || '').trim()

  const save = async (value: string) => {
    setBusy(true)
    setErr('')
    try {
      await onSave(value.trim())
      setEditing(false)
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }

  if (editing)
    return (
      <div className="note-bar note-bar-edit">
        <textarea
          rows={3}
          maxLength={2000}
          value={draft}
          autoFocus
          placeholder={t('Что важно знать дежурному: что сломано, кто в курсе, когда починят')}
          onChange={(e) => setDraft(e.target.value)}
        />
        <div className="note-bar-actions">
          <button disabled={busy} onClick={() => save(draft)}>
            {t('Сохранить')}
          </button>
          <button className="ghost" disabled={busy} onClick={() => setEditing(false)}>
            {t('Отмена')}
          </button>
          {text && (
            <button className="ghost" disabled={busy} onClick={() => save('')}>
              {t('Убрать заметку')}
            </button>
          )}
          <span className="muted small">{t('Заметка приходит строкой в алертах')}</span>
        </div>
        {err && <div className="muted small">{err}</div>}
      </div>
    )

  if (!text) {
    if (!canEdit) return null
    return (
      <button
        className="ghost small note-add"
        onClick={() => {
          setDraft('')
          setEditing(true)
        }}
      >
        📝 {t('добавить заметку')}
      </button>
    )
  }

  return (
    <div className="note-bar">
      <span className="note-bar-ic">📝</span>
      <div className="note-bar-body">
        <div className="note-bar-text">{text}</div>
        {(by || at) && (
          <div className="muted small">
            {[by, at ? new Date(at).toLocaleString() : ''].filter(Boolean).join(', ')}
          </div>
        )}
      </div>
      {canEdit && (
        <button
          className="ghost icon-btn"
          title={t('Изменить заметку')}
          onClick={() => {
            setDraft(text)
            setEditing(true)
          }}
        >
          ✎
        </button>
      )}
    </div>
  )
}
