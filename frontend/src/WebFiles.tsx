import { useState } from 'react'
import { acceptWebfiles, type Server, type WebRoot } from './api'
import { useAuth } from './auth'
import { useI18n } from './i18n'

// Раздел "Файлы сайтов" в "Сервисах" ноды (backend app/webfiles.py, helper webfiles-setup):
// веб-корни, которые helper нашел сам, последняя выкладка и признаки - новые php и правки кода
// вне выкладки. Корень с доменом своего монитора показывает признаки и в карточке сайта.

const fmtDateTime = (ts: string) =>
  new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })

type T = (k: string, p?: Record<string, string | number>) => string

function fileList(files: { p: string }[], t: T): string {
  const names = files.map((f) => f.p)
  return names.slice(0, 5).join(', ') + (names.length > 5 ? ' ' + t('и еще {n}', { n: names.length - 5 }) : '')
}

export function webfilesBad(s: Server): number {
  return (s.webfiles?.roots ?? []).filter((r) => r.php.length > 0 || r.edit.length > 0).length
}

export function WebFiles({ s, onChanged }: { s: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const roots = s.webfiles?.roots ?? []
  const act = async (root: string, key = '') => {
    setBusy(root + key)
    setErr('')
    try {
      await acceptWebfiles(s.id, root, key)
      onChanged()
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }
  const sign = (r: WebRoot, key: 'php' | 'edit') => {
    const files = r[key]
    if (!files.length) return null
    const since = files.map((f) => f.since).sort()[0]
    return (
      <div className="tamper-row">
        <span className={`lk-dot ${key === 'php' ? 'lk-dot-bad' : 'lk-dot-warn'}`} />
        <span className="tamper-ico">{key === 'php' ? '🐘' : '✏️'}</span>
        <span className="tamper-txt">
          {key === 'php' ? t('Новые php') : t('Код изменен вне выкладки')}
          <span className="muted small">{fileList(files, t)}</span>
        </span>
        {since && <span className="muted small tamper-when">{t('с {t}', { t: fmtDateTime(since) })}</span>}
        {!isViewer && (
          <button className="ghost small" disabled={!!busy} onClick={() => act(r.key, key)}>{t('это наше')}</button>
        )}
      </div>
    )
  }
  return (
    <div className="tamper">
      {roots.map((r) => (
        <div key={r.key} className="webfiles-root">
          <div className="webfiles-head">
            <span className="mono">{r.path}</span>
            <span className="muted small">
              {[
                r.where && r.where !== 'host' ? r.where : t('на хосте'),
                (r.pods ?? 0) > 1 ? t('подов: {n}', { n: r.pods ?? 0 }) : '',
                r.domains.length ? r.domains.slice(0, 3).join(', ') : '',
                r.layer
                  ? t('записано в контейнере файлов кода: {n}', { n: r.files })
                  : r.big ? t('файлов кода больше {n}, список неполный', { n: r.files }) : t('файлов кода: {n}', { n: r.files }),
                r.deploy ? t('выкладка {t}: {n} файлов', { t: fmtDateTime(r.deploy.at), n: r.deploy.n }) : '',
                r.site && (r.php.length || r.edit.length) ? t('признаки и в карточке сайта') : '',
              ].filter(Boolean).join(' · ')}
            </span>
          </div>
          {sign(r, 'php')}
          {sign(r, 'edit')}
        </div>
      ))}
      {roots.length === 0 && <div className="muted small">{t('Веб-корней не нашлось.')}</div>}
      {err && <div className="form-error">{err}</div>}
    </div>
  )
}
