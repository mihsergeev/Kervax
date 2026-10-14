import { useEffect, useState } from 'react'
import { ApiError, getServerPackages, type Server, type ServerPackages } from './api'
import { useI18n } from './i18n'

// Установленные пакеты (backend app/packages.py, агент 2.28). Только справка: сколько и
// фильтр по имени или версии, весь список - по кнопке. Ни алертов, ни пунктов "требует действий".

type Props = { server: Server; onUnauthorized: () => void }

// пришли из поиска (Ctrl+K) по пакету - сразу с его именем в фильтре (кладет SearchModal)
function fromSearch(id: number): string {
  try {
    const v = JSON.parse(sessionStorage.getItem('kervax_pkgs_q') || 'null')
    return v && v.id === id && Date.now() - v.ts < 30_000 ? String(v.q) : ''
  } catch {
    return ''
  }
}

export function PackagesCard({ server: s, onUnauthorized }: Props) {
  const { t } = useI18n()
  const sum = s.last_report?.pkgs
  const [q, setQ] = useState(() => fromSearch(s.id))
  const [all, setAll] = useState(false)
  const [res, setRes] = useState<ServerPackages | null>(null)
  const [err, setErr] = useState('')
  const query = q.trim()
  const want = all || query.length >= 2
  const hash = sum?.h ?? ''

  useEffect(() => {
    if (!want) return
    let alive = true
    const tm = setTimeout(() => {
      getServerPackages(s.id, query)
        .then((r) => {
          if (!alive) return
          setRes(r)
          setErr('')
        })
        .catch((e) => {
          if (e instanceof ApiError && e.status === 401) return onUnauthorized()
          if (alive) setErr(String((e as Error).message || e))
        })
    }, 200)
    return () => {
      alive = false
      clearTimeout(tm)
    }
  }, [s.id, query, want, hash, onUnauthorized])

  if (!sum?.n) return null
  const shown = want ? res : null
  return (
    <div className="detail-inc pkgs" id="mcard-pkgs">
      <div className="chart-cap">{t('Пакеты')}</div>
      <div className="pkgs-bar">
        <span className="muted small">
          {t('установлено: {n}', { n: sum.n })}
          {sum.mgr ? ` (${sum.mgr})` : ''}
        </span>
        <input
          className="pkgs-search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t('имя или версия')}
        />
        <button className="ghost small row-act" onClick={() => setAll(!all)}>
          {all ? t('свернуть') : t('весь список')}
        </button>
      </div>
      {err && <p className="form-error">{err}</p>}
      {want && !shown && !err && <div className="muted small">{t('загрузка…')}</div>}
      {shown && shown.n === 0 && (
        <div className="muted small">{t('Список еще не пришел: агент пришлет его со следующим отчетом.')}</div>
      )}
      {shown && shown.n > 0 && shown.total === 0 && <div className="muted small">{t('Ничего не найдено.')}</div>}
      {shown && shown.items.length > 0 && (
        <div className="pkgs-list small">
          {shown.items.map(([name, ver]) => (
            <div key={name + ' ' + ver} className="pkgs-row">
              <span className="pkgs-name">{name}</span>
              <span className="muted pkgs-ver">{ver}</span>
            </div>
          ))}
        </div>
      )}
      {shown && shown.total > shown.items.length && (
        <div className="muted small">
          {t('показаны первые {k} из {n}, уточните фильтр', { k: shown.items.length, n: shown.total })}
        </div>
      )}
    </div>
  )
}
