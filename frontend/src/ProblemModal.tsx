import { useEffect, useState } from 'react'
import { ApiError, getHealth, sendProblemReport } from './api'
import { recentClientLog, uiPage, uiSection } from './errorLog'
import { useI18n } from './i18n'

// подписи разделов, как в шапке
const SECTION: Record<string, string> = {
  home: 'Главная',
  sites: 'Сайты',
  servers: 'Серверы',
  docker: 'Докер',
  kuber: 'Кубер',
  services: 'Сервисы',
  backups: 'Бэкапы',
}

// "Сообщить о проблеме": человек пишет пару слов, а адрес страницы, раздел, версия панели и его
// ошибки за последние минуты (что записал бэкенд и неудачные запросы этой вкладки) прикладываются
// сами. Скриншот не нужен. Админ видит сообщение в окне "Ошибки панели".
export function ProblemModal({ onClose, onUnauthorized }: { onClose: () => void; onUnauthorized: () => void }) {
  const { t } = useI18n()
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [sent, setSent] = useState<{ attached: number; client_log: number } | null>(null)
  const [version, setVersion] = useState('')
  // что было на экране в момент открытия окна: адрес не меняется, пока оно открыто
  const [page] = useState(() => uiPage())
  const [section] = useState(() => uiSection())
  const [log] = useState(() => recentClientLog())

  useEffect(() => {
    getHealth().then((h) => setVersion(h.version)).catch(() => setVersion(''))
  }, [])

  const send = async () => {
    if (!text.trim()) return
    setBusy(true)
    setErr(null)
    try {
      const r = await sendProblemReport({ text: text.trim(), page, section, client_log: log })
      setSent({ attached: r.attached, client_log: r.client_log })
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal pe-problem" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Сообщить о проблеме')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        {sent ? (
          <>
            <p className="pe-sent">
              {t('Отправлено, спасибо. Сообщение у админа в окне "Ошибки панели".')}
            </p>
            <p className="muted small">
              {sent.attached + sent.client_log > 0
                ? t('Приложено ошибок: {n}, неудачных запросов: {m}.', { n: sent.attached, m: sent.client_log })
                : t('Ошибок за последние 15 минут не было: админ увидит только ваш текст и адрес страницы.')}
            </p>
            <div className="pe-acts">
              <button onClick={onClose}>{t('Закрыть')}</button>
            </div>
          </>
        ) : (
          <>
            <p className="muted small">
              {t('Пара слов: что делали и что пошло не так. Адрес страницы, раздел, версия панели и ваши ошибки за последние 15 минут приложатся сами, скриншот не нужен.')}
            </p>
            <textarea
              className="pe-text"
              rows={5}
              maxLength={2000}
              autoFocus
              value={text}
              placeholder={t('например: в карточке бэкапа нажал "включить дамп", а под строкой красная ошибка без причины')}
              onChange={(e) => setText(e.target.value)}
            />
            <div className="pe-attach">
              <div className="srv-nav-grp rt-cap">{t('Приложится')}</div>
              <div className="pe-kv">
                <span className="muted">{t('Страница')}</span>
                <span className="mono">{page}</span>
                <span className="muted">{t('Раздел')}</span>
                <span>{section ? t(SECTION[section] ?? section) : '-'}</span>
                <span className="muted">{t('Версия')}</span>
                <span>{version || '-'}</span>
                <span className="muted">{t('Неудачные запросы')}</span>
                <span>
                  {log.length === 0
                    ? t('за 15 минут не было')
                    : log.slice(-5).map((x, i) => (
                        <span key={i} className="pe-log">
                          <span className="mono">
                            {x.method} {x.path} {x.status || t('нет связи')}
                          </span>
                          {x.detail && <span className="muted"> {x.detail}</span>}
                        </span>
                      ))}
                  {log.length > 5 && <span className="muted"> {t('и еще {n}', { n: log.length - 5 })}</span>}
                </span>
              </div>
              <p className="muted small">{t('Ошибки, которые панель записала сама, добавятся при отправке.')}</p>
            </div>
            {err && <p className="form-error">{err}</p>}
            <div className="pe-acts">
              <button className="ghost" onClick={onClose}>
                {t('Отмена')}
              </button>
              <button disabled={busy || !text.trim()} onClick={send}>
                {busy ? '...' : t('Отправить')}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
