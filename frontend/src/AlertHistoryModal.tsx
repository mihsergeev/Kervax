import { useEffect, useState } from 'react'
import { ApiError, alertHistory, type AlertEventDto, type AlertHistory, type AlertHistoryQuery } from './api'
import { useI18n } from './i18n'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
  initialTarget?: string // открыть сразу с фильтром по серверу или монитору (из карточки сервера)
}

const PERIODS = [1, 7, 30, 90, 180]

// История отправленных алертов. Нужна на два вопроса: приходил ли алерт про эту беду (и
// когда), и что шумит больше всего - сводка считает срабатывания за весь период под
// фильтром, клик по строке сводки сужает список до этого вида или объекта.
export function AlertHistoryModal({ onClose, onUnauthorized, initialTarget }: Props) {
  const { t } = useI18n()
  const [days, setDays] = useState(7)
  const [only, setOnly] = useState<'all' | 'fires' | 'recoveries'>('all')
  const [target, setTarget] = useState(initialTarget ?? '')
  const [kind, setKind] = useState<{ kind: string; label: string } | null>(null)
  const [q, setQ] = useState('')
  const [qDebounced, setQDebounced] = useState('')
  const [data, setData] = useState<AlertHistory | null>(null)
  const [events, setEvents] = useState<AlertEventDto[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    const id = window.setTimeout(() => setQDebounced(q.trim()), 400)
    return () => window.clearTimeout(id)
  }, [q])

  const query: AlertHistoryQuery = { days, only, target, kind: kind?.kind ?? '', q: qDebounced }
  const key = JSON.stringify(query)

  useEffect(() => {
    let alive = true
    setBusy(true)
    setErr(null)
    alertHistory(JSON.parse(key) as AlertHistoryQuery)
      .then((d) => {
        if (!alive) return
        setData(d)
        setEvents(d.events)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
        else if (alive) setErr(e instanceof Error ? e.message : String(e))
      })
      .finally(() => alive && setBusy(false))
    return () => {
      alive = false
    }
  }, [key, onUnauthorized])

  const more = async () => {
    const last = events[events.length - 1]
    if (!last) return
    setBusy(true)
    try {
      const d = await alertHistory({ ...query, before_id: last.id })
      setEvents((prev) => [...prev, ...d.events])
      setData((prev) => (prev ? { ...prev, more: d.more } : d))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const s = data?.summary
  const fmt = (ts: string) => new Date(ts).toLocaleString()
  return (
    <div className="modal-backdrop">
      <div className="card modal docker-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('История алертов')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="modal-toolbar">
          <select className="modal-sort" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {PERIODS.map((d) => (
              <option key={d} value={d}>{t('за {n} дн.', { n: d })}</option>
            ))}
          </select>
          <select className="modal-sort" value={only} onChange={(e) => setOnly(e.target.value as typeof only)}>
            <option value="all">{t('все')}</option>
            <option value="fires">{t('только срабатывания')}</option>
            <option value="recoveries">{t('только отбои')}</option>
          </select>
          <input className="checks-search modal-search" placeholder={t('Поиск по тексту')}
            value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        {(target || kind) && (
          <div className="ah-filters">
            {target && (
              <button className="type-chip ah-chip" onClick={() => setTarget('')} title={t('Снять фильтр')}>
                {target} ✕
              </button>
            )}
            {kind && (
              <button className="type-chip ah-chip" onClick={() => setKind(null)} title={t('Снять фильтр')}>
                {kind.label} ✕
              </button>
            )}
          </div>
        )}
        {s && (
          <>
            <div className="muted small">
              {t('Срабатываний: {f}, отбоев: {r}', { f: s.fires, r: s.recoveries })}
            </div>
            {(s.kinds.length > 0 || s.targets.length > 0) && (
              <div className="ah-summary">
                <div>
                  <div className="ah-summary-title">{t('Чаще всего: виды')}</div>
                  {s.kinds.slice(0, 8).map((k) => (
                    <button key={k.kind} className="ah-bar" onClick={() => setKind({ kind: k.kind, label: k.label })}>
                      <span className="ah-bar-fill" style={{ width: `${(k.n / s.kinds[0].n) * 100}%` }} />
                      <span className="ah-bar-label">{k.label}</span>
                      <span className="ah-bar-n">{k.n}</span>
                    </button>
                  ))}
                </div>
                <div>
                  <div className="ah-summary-title">{t('Чаще всего: серверы и мониторы')}</div>
                  {s.targets.slice(0, 8).map((x) => (
                    <button key={x.target} className="ah-bar" onClick={() => setTarget(x.target)}>
                      <span className="ah-bar-fill" style={{ width: `${(x.n / s.targets[0].n) * 100}%` }} />
                      <span className="ah-bar-label mono">{x.target}</span>
                      <span className="ah-bar-n">{x.n}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
        {err && <p className="form-error">{err}</p>}
        <div className="loc-results docker-clist docker-clist-scroll">
          {data && events.length === 0 && !busy && (
            <div className="muted small">{t('За этот период алертов не было.')}</div>
          )}
          {events.map((e) => (
            <div key={e.id} className={`ah-row ${e.recovery ? 'ah-rec' : ''}`}>
              <div className="ah-meta muted small">
                <span>{fmt(e.ts)}</span>
                {e.target && (
                  <button className="ghost ah-link" onClick={() => setTarget(e.target)}>{e.target}</button>
                )}
                {e.kind && (
                  <button className="ghost ah-link" onClick={() => setKind({ kind: e.kind, label: e.label })}>{e.label}</button>
                )}
              </div>
              <div className="ah-text">{e.text}</div>
            </div>
          ))}
        </div>
        {data?.more && (
          <div className="modal-actions">
            <button className="ghost" disabled={busy} onClick={more}>
              {busy ? '...' : t('Показать раньше')}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
