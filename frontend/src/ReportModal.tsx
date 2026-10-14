import { useEffect, useState } from 'react'
import { ApiError, getReport, type Report } from './api'
import { useI18n } from './i18n'
import { fmtDur } from './units'

// Отчет за период (backend app/api/report.py): что ломалось, сколько лежало, что шумело.
// Смотрят раз в неделю здесь, в панели, - это не алерт и в Telegram не уходит.

const PERIODS = [1, 7, 30]

type Props = { onClose: () => void; onUnauthorized: () => void }

// изменение к прошлому периоду: "лучше" - зеленым, "хуже" - красным
function Delta({ cur, prev, unit, lowerBetter }: { cur: number; prev: number; unit: string; lowerBetter?: boolean }) {
  const { t } = useI18n()
  const d = cur - prev
  if (Math.abs(d) < (unit === 'pp' ? 0.005 : 0.5)) return <span className="muted small">{t('как в прошлый период')}</span>
  const good = lowerBetter ? d < 0 : d > 0
  const txt =
    unit === 'pp'
      ? t('{d} п.п.', { d: `${d > 0 ? '+' : ''}${d.toFixed(2)}` })
      : unit === 's'
        ? `${d > 0 ? '+' : '-'}${fmtDur(Math.abs(Math.round(d)))}`
        : prev > 0
          ? `${d > 0 ? '+' : ''}${Math.round((d / prev) * 100)}%`
          : `+${Math.round(d)}`
  return <span className={`small rep-delta ${good ? 'rep-good' : 'rep-bad'}`}>{txt} {t('к прошлому периоду')}</span>
}

export function ReportModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [days, setDays] = useState(7)
  const [data, setData] = useState<Report | null>(null)
  const [err, setErr] = useState('')
  // за какой период данные уже пришли: другой выбран - значит, считаем
  const [loaded, setLoaded] = useState<number | null>(null)
  const busy = loaded !== days

  useEffect(() => {
    let alive = true
    getReport(days)
      .then((r) => {
        if (!alive) return
        setData(r)
        setErr('')
        setLoaded(days)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        if (!alive) return
        setErr(String((e as Error).message || e))
        setLoaded(days)
      })
    return () => {
      alive = false
    }
  }, [days, onUnauthorized])

  const fmtDay = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const sites = data?.sites
  const servers = data?.servers
  const noise = data?.noise
  return (
    <div className="modal-backdrop">
      <div className="card modal docker-modal rep-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Отчет')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="modal-toolbar">
          <div className="win-switch">
            {PERIODS.map((d) => (
              <button key={d} className={`win-btn${d === days ? ' win-btn-active' : ''}`} onClick={() => setDays(d)}>
                {d === 1 ? t('сутки') : t('{n} дней', { n: d })}
              </button>
            ))}
          </div>
          {data && (
            <span className="muted small">
              {fmtDay(data.since)} - {fmtDay(data.until)}
            </span>
          )}
          {busy && <span className="muted small">{t('считаем...')}</span>}
        </div>
        {err && <p className="form-error">{err}</p>}
        {data && (
          <>
            <div className="rep-tiles">
              {sites && (
                <div className="rep-tile">
                  <div className="rep-tile-label">{t('Сайты, средний аптайм')}</div>
                  <div className="rep-tile-val">{sites.uptime != null ? `${sites.uptime.toFixed(2)}%` : '—'}</div>
                  <div className="muted small">
                    {sites.incidents
                      ? t('сбоев: {n}, лежали {d}', { n: sites.incidents, d: fmtDur(sites.downtime_s) })
                      : t('сбоев не было')}
                  </div>
                  {sites.prev && sites.uptime != null && sites.prev.uptime != null && (
                    <Delta cur={sites.uptime} prev={sites.prev.uptime} unit="pp" />
                  )}
                </div>
              )}
              {servers && (
                <div className="rep-tile">
                  <div className="rep-tile-label">{t('Серверы, недоступность')}</div>
                  <div className="rep-tile-val">{servers.offline_s ? fmtDur(servers.offline_s) : '0'}</div>
                  <div className="muted small">
                    {t('пропадали: {n} из {m}', { n: servers.with_gaps, m: servers.total })}
                  </div>
                  {servers.prev_ok && (
                    <Delta cur={servers.offline_s} prev={servers.prev_offline_s} unit="s" lowerBetter />
                  )}
                </div>
              )}
              {noise && (
                <div className="rep-tile">
                  <div className="rep-tile-label">{t('Алертов пришло')}</div>
                  <div className="rep-tile-val">{noise.fires}</div>
                  <div className="muted small">{t('без отбоев')}</div>
                  <Delta cur={noise.fires} prev={noise.prev_fires} unit="n" lowerBetter />
                </div>
              )}
              {servers && servers.panel_s > 0 && (
                <div className="rep-tile">
                  <div className="rep-tile-label">{t('Панель не принимала отчеты')}</div>
                  <div className="rep-tile-val">{fmtDur(servers.panel_s)}</div>
                  <div className="muted small">
                    {t('раз: {n}, в простой серверов не входит', { n: servers.panel_gaps })}
                  </div>
                </div>
              )}
            </div>

            {sites && (
              <section className="rep-sec">
                <h4>{t('Сайты со сбоями')}</h4>
                {sites.items.length === 0 ? (
                  <div className="muted small">{t('Все {n} мониторов отработали без сбоев', { n: sites.total })}</div>
                ) : (
                  <div className="rep-table-wrap">
                    <table className="rep-table">
                      <thead>
                        <tr>
                          <th>{t('Монитор')}</th>
                          <th>{t('Аптайм')}</th>
                          <th>{t('Сбоев')}</th>
                          <th>{t('Лежал')}</th>
                          <th>{t('Самый долгий')}</th>
                          <th>{t('Последний')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sites.items.map((c) => (
                          <tr key={c.id}>
                            <td>
                              <a href={`/?check=${c.id}`}>{c.name}</a>
                              {c.open && <span className="type-chip rep-open">{t('лежит сейчас')}</span>}
                            </td>
                            <td className="num">{c.uptime != null ? `${c.uptime.toFixed(2)}%` : '—'}</td>
                            <td className="num">{c.incidents || '—'}</td>
                            <td className="num">{fmtDur(c.downtime_s)}</td>
                            <td className="num">{fmtDur(c.longest_s)}</td>
                            <td className="num">{c.last_at ? fmtDay(c.last_at) : '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {sites.items.length > 0 && (
                  <div className="muted small">
                    {t('Остальные {n} без сбоев', { n: sites.total - sites.affected })}
                    {sites.more > 0 && ` · ${t('и еще {n} со сбоями', { n: sites.more })}`}
                  </div>
                )}
              </section>
            )}

            {servers && (
              <section className="rep-sec">
                <h4>{t('Серверы')}</h4>
                {servers.items.length === 0 ? (
                  <div className="muted small">{t('Серверы не пропадали, алертов по ним не было')}</div>
                ) : (
                  <div className="rep-table-wrap">
                    <table className="rep-table">
                      <thead>
                        <tr>
                          <th>{t('Сервер')}</th>
                          <th>{t('Недоступен')}</th>
                          <th>{t('Раз')}</th>
                          <th>{t('Самый долгий')}</th>
                          <th>{t('Алертов')}</th>
                          <th>{t('Чаще всего')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {servers.items.map((s) => (
                          <tr key={s.id}>
                            <td>
                              <a href={`/?server=${s.id}`}>{s.name}</a>
                            </td>
                            <td className="num">{fmtDur(s.offline_s)}</td>
                            <td className="num">{s.gaps || '—'}</td>
                            <td className="num">{fmtDur(s.longest_s)}</td>
                            <td className="num">{s.alerts || '—'}</td>
                            <td>
                              {s.kinds.map((k) => (
                                <span key={k.kind} className="type-chip">
                                  {k.label} {k.n}
                                </span>
                              ))}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            )}

            {noise && noise.top.length > 0 && (
              <section className="rep-sec">
                <h4>{t('Что шумело')}</h4>
                {noise.top.map((x) => (
                  <div key={`${x.kind}|${x.target}`} className="ah-bar rep-bar">
                    <span className="ah-bar-fill" style={{ width: `${(x.n / noise.top[0].n) * 100}%` }} />
                    <span className="ah-bar-label">
                      <span className="mono">{x.target}</span> <span className="muted">· {x.label}</span>
                    </span>
                    <span className="ah-bar-n">{x.n}</span>
                  </div>
                ))}
              </section>
            )}

            {noise && noise.backups.length > 0 && (
              <section className="rep-sec">
                <h4>{t('Бэкапы: что ломалось')}</h4>
                {noise.backups.map((b) => (
                  <div key={b.target} className="rep-line">
                    <span className="mono">{b.target}</span>
                    <span className="muted small">
                      {b.kinds.join(', ')} · {t('алертов: {n}', { n: b.n })}
                    </span>
                  </div>
                ))}
              </section>
            )}

            {data.maint.length > 0 && (
              <section className="rep-sec">
                <h4>{t('Окна обслуживания')}</h4>
                {data.maint.map((m, i) => (
                  <div key={i} className="rep-line">
                    <span>🚧 {m.title}</span>
                    <span className="muted small">
                      {m.count > 1
                        ? t('{n} раз, последний {t}', { n: m.count, t: `${fmtDay(m.start)} - ${fmtDay(m.end)}` })
                        : `${fmtDay(m.start)} - ${fmtDay(m.end)}`}
                    </span>
                  </div>
                ))}
              </section>
            )}
          </>
        )}
      </div>
    </div>
  )
}
