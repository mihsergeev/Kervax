import { useMemo, useState } from 'react'
import { ApiError, importChecks, type CheckForm, type CheckType } from './api'
import { useI18n } from './i18n'

type Props = {
  groups?: string[]
  onClose: () => void
  onImported: () => void
  onUnauthorized: () => void
}

function deriveName(target: string): string {
  let s = target.trim().replace(/^\w+:\/\//, '').replace(/^www\./, '')
  s = s.split('/')[0]
  return s || target.trim()
}

function parseItems(text: string, type: CheckType, group: string): CheckForm[] {
  const out: CheckForm[] = []
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line) continue
    let name: string
    let target: string
    const bar = line.indexOf('|')
    if (bar >= 0) {
      name = line.slice(0, bar).trim()
      target = line.slice(bar + 1).trim()
    } else {
      target = line
      name = deriveName(line)
    }
    if (!target) continue
    const item: CheckForm = { name: name || target, type, target }
    if (group.trim()) item.group_name = group.trim()
    if (type === 'tcp_port') {
      const m = target.match(/^(.*?):(\d+)$/)
      if (m) {
        item.target = m[1]
        item.port = Number(m[2])
      }
    }
    out.push(item)
  }
  return out
}

export function ImportModal({ groups = [], onClose, onImported, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [text, setText] = useState('')
  const [type, setType] = useState<CheckType>('http')
  const [group, setGroup] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const items = useMemo(() => parseItems(text, type, group), [text, type, group])

  async function submit() {
    if (items.length === 0) return
    setBusy(true)
    setErr(null)
    try {
      await importChecks(items)
      onImported()
      onClose()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal import-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Массово добавить сайты')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <p className="muted small">
          {t('По одному на строку. Можно «Имя | адрес» — иначе имя возьмётся из адреса. Протокол можно не писать: проверим по https, а сайт только на http отметим как проблему (для http-сайта впишите http:// явно).')}
        </p>

        <div className="form-row">
          <label className="field">
            <span>{t('Тип')}</span>
            <select value={type} onChange={(e) => setType(e.target.value as CheckType)}>
              <option value="http">{t('HTTP(S) — сайт/эндпоинт')}</option>
              <option value="tcp_port">{t('TCP-порт')}</option>
            </select>
          </label>
          <label className="field">
            <span>{t('Группа')}</span>
            <input
              value={group}
              onChange={(e) => setGroup(e.target.value)}
              placeholder={t('напр. Организация / Прод / VPN')}
              list="kervax-import-groups"
            />
            <datalist id="kervax-import-groups">
              {groups.map((g) => (
                <option key={g} value={g} />
              ))}
            </datalist>
          </label>
        </div>

        <label className="field">
          <span>{t('Список')}</span>
          <textarea
            className="import-textarea"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={8}
            placeholder={
              type === 'http'
                ? 'example.com\nМой блог | blog.example.com\napi.service.com/health\nwikipedia.org\nПрод API | api.myapp.io/status\ngithub.com\nstatus.corp.ru'
                : '8.8.8.8:53\nМоя БД | db.host:5432\nsmtp.mail.ru:587\nredis.internal:6379\ndns.google:853'
            }
          />
        </label>

        {err && <p className="form-error">{err}</p>}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            {t('Отмена')}
          </button>
          <button onClick={submit} disabled={items.length === 0 || busy}>
            {busy ? t('…') : t('Добавить {n}', { n: items.length })}
          </button>
        </div>
      </div>
    </div>
  )
}
