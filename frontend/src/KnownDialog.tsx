import { useState } from 'react'
import type { KnownIssue } from './api'
import { useI18n } from './i18n'
import { SRV_ALERT_KINDS } from './mutes'
import { knownMeta, knownText } from './serverUtils'

const KNOWN_DAYS: [number, string][] = [
  [0, 'пока не починится'],
  [1, '1 день'],
  [3, '3 дня'],
  [7, 'неделя'],
  [30, 'месяц'],
]

// Отметить проблему известной: что про нее знают и на сколько. Алерт молчит, пока не станет
// хуже; ушла сама - отметка закроется; вышел срок, а проблема осталась - алерт придет.
export function KnownDialog({
  title,
  onSave,
  onClose,
}: {
  title: string
  onSave: (text: string, days: number) => Promise<void>
  onClose: () => void
}) {
  const { t } = useI18n()
  const [text, setText] = useState('')
  const [days, setDays] = useState(0)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const save = async () => {
    setBusy(true)
    setErr('')
    try {
      await onSave(text.trim(), days)
    } catch (e) {
      setErr(String((e as Error).message || e))
      setBusy(false)
    }
  }
  return (
    <div className="modal-backdrop">
      <div className="card known-dialog" onClick={(e) => e.stopPropagation()}>
        <h3>📌 {t('Известная проблема')}</h3>
        <div className="muted small">{title}</div>
        <textarea
          rows={3}
          maxLength={500}
          autoFocus
          value={text}
          placeholder={t('Что известно: кто чинит, когда, где тикет')}
          onChange={(e) => setText(e.target.value)}
        />
        <label className="field">
          <span>{t('Молчать')}</span>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {KNOWN_DAYS.map(([v, label]) => (
              <option key={v} value={v}>
                {t(label)}
              </option>
            ))}
          </select>
        </label>
        <div className="muted small">
          {t('Алерт молчит, пока не станет хуже. Проблема ушла сама - отметка закроется, вышел срок - алерт придет как обычно. Раз в неделю бот напомнит, что она все еще висит.')}
        </div>
        <div className="note-bar-actions">
          <button disabled={busy || !text.trim()} onClick={save}>
            {t('Отметить')}
          </button>
          <button className="ghost" disabled={busy} onClick={onClose}>
            {t('Отмена')}
          </button>
        </div>
        {err && <div className="muted small">{err}</div>}
      </div>
    </div>
  )
}

// Известные проблемы в карточке сервера: вид, что написали, кто и до когда, снять
export function KnownServerList({
  items,
  canEdit,
  onDrop,
}: {
  items: Record<string, KnownIssue>
  canEdit: boolean
  onDrop: (kind: string) => Promise<void>
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const drop = async (kind: string) => {
    setBusy(true)
    try {
      await onDrop(kind)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="note-bar known-bar">
      <span className="note-bar-ic">📌</span>
      <div className="note-bar-body">
        <b>{t('Известные проблемы')}</b>
        {Object.entries(items).map(([kind, k]) => (
          <div key={kind} className="known-row">
            <span>
              {t(SRV_ALERT_KINDS.find((x) => x.k === kind)?.label ?? kind)}: {k.text}
              {knownMeta(k, t) && <span className="muted small"> ({knownMeta(k, t)})</span>}
              {k.worse && <span className="t-down small"> · {t('стало хуже отмеченного, алерт ушел')}</span>}
            </span>
            {canEdit && (
              <button
                className="ghost small"
                disabled={busy}
                title={t('Снять отметку: если проблема осталась, придет алерт')}
                onClick={() => drop(kind)}
              >
                {t('снять')}
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

// Известная проблема в карточке монитора
export function KnownCheckBar({
  known,
  canEdit,
  onDrop,
}: {
  known: KnownIssue
  canEdit: boolean
  onDrop: () => Promise<void>
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  return (
    <div className="note-bar known-bar">
      <span className="note-bar-ic">📌</span>
      <div className="note-bar-body">
        <div>
          <b>{t('Известная проблема')}:</b> {knownText(known, t)}
        </div>
        <div className="muted small">
          {[knownMeta(known, t), t('алерт о сбое молчит, отметка закроется, когда монитор поднимется')]
            .filter(Boolean)
            .join(' · ')}
        </div>
      </div>
      {canEdit && (
        <button
          className="ghost small"
          disabled={busy}
          title={t('Снять отметку: если монитор все еще лежит, придет алерт')}
          onClick={async () => {
            setBusy(true)
            try {
              await onDrop()
            } finally {
              setBusy(false)
            }
          }}
        >
          {t('снять')}
        </button>
      )}
    </div>
  )
}
