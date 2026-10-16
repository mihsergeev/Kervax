import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError, getReport, type Report } from './api'
import { useBranding } from './BrandLogo'
import { CardNav, type SecDef } from './cardLayout'
import { useI18n } from './i18n'
import { NetTile } from './NetLinks'
import { downloadReportHtml } from './reportHtml'
import { fmtDur } from './units'

// Отчет за период (backend app/api/report.py) как карточка сервера: сводка с плитками и
// графиками по дням, у сайтов, серверов, шума, бэкапов и окон обслуживания свои разделы.
// Смотрят раз в неделю здесь, в панели, - это не алерт и в Telegram не уходит.

const PERIODS = [1, 7, 30]

type T = (k: string, p?: Record<string, string | number>) => string
type Props = {
  onClose: () => void
  onUnauthorized: () => void
  onOpen: (kind: 'server' | 'check', id: number) => void
}

const LEAD: Record<string, string> = {
  over: 'Что ломалось за период, сколько лежало и что шумело. Это не алерт: смотрят здесь, раз в неделю.',
  sites: 'Мониторы, которые лежали или отвечали не всегда. Аптайм - по проверкам, простой - по инцидентам, пока монитор лежал.',
  servers: 'Серверы, которые пропадали или по которым приходили алерты. Простой - по пропускам в отчетах агента: заглушенный алерт его не отменяет.',
  noise: 'Срабатывания алертов без отбоев. Если одно и то же приходит каждый день - стоит поправить порог или заглушить.',
  backups: 'Бэкапы, по которым приходили алерты: не сделался, устарел, не сходится ротация.',
  maint: 'Плановые работы за период: пока окно шло, алерты по его серверам и мониторам молчали.',
}

// изменение к прошлому периоду: "лучше" - зеленым, "хуже" - красным
function Delta({ cur, prev, unit, lowerBetter }: { cur: number; prev: number; unit: string; lowerBetter?: boolean }) {
  const { t } = useI18n()
  const d = cur - prev
  if (Math.abs(d) < (unit === 'pp' ? 0.005 : 0.5)) return <div className="muted small">{t('как в прошлый период')}</div>
  const good = lowerBetter ? d < 0 : d > 0
  const txt =
    unit === 'pp'
      ? t('{d} п.п.', { d: `${d > 0 ? '+' : ''}${d.toFixed(2)}` })
      : unit === 's'
        ? `${d > 0 ? '+' : '-'}${fmtDur(Math.abs(Math.round(d)))}`
        : prev > 0
          ? `${d > 0 ? '+' : ''}${Math.round((d / prev) * 100)}%`
          : `+${Math.round(d)}`
  return (
    <div className={`small rep-delta ${good ? 'rep-good' : 'rep-bad'}`}>
      {txt} {t('к прошлому периоду')}
    </div>
  )
}

type Series = { key: string; label: string; cls: string; values: number[] }

// Столбики по дням, плотно и с частой сеткой. Несколько рядов - стопкой.
function DayBars({ labels, series, fmt, t }: { labels: string[]; series: Series[]; fmt: (n: number) => string; t: T }) {
  const sums = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] ?? 0), 0))
  const max = Math.max(...sums, 0)
  const every = labels.length > 16 ? 3 : labels.length > 9 ? 2 : 1
  const day = (iso: string) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`
  return (
    <div className="rp-chart-box">
      <div className="rp-chart">
        <span className="rp-max">{max ? fmt(max) : ''}</span>
        {labels.map((d, i) => (
          <div
            key={d}
            className="rp-col"
            title={`${day(d)}: ${series.map((s) => `${s.label} ${fmt(s.values[i] ?? 0)}`).join(', ')}`}
          >
            {series.map((s) =>
              s.values[i] ? (
                <span key={s.key} className={`rp-seg ${s.cls}`} style={{ height: `${(100 * s.values[i]) / (max || 1)}%` }} />
              ) : null,
            )}
          </div>
        ))}
      </div>
      <div className="rp-x">
        {labels.map((d, i) => (
          <span key={d}>{i % every === 0 ? day(d) : ''}</span>
        ))}
      </div>
      <div className="rt-legend small">
        {series.map((s) => (
          <span key={s.key}>
            <i className={`rt-sw ${s.cls}`} />
            {s.label}: {fmt(s.values.reduce((a, b) => a + b, 0))}
          </span>
        ))}
        {max === 0 && <span className="muted">{t('за период пусто')}</span>}
      </div>
    </div>
  )
}

export function ReportModal({ onClose, onUnauthorized, onOpen }: Props) {
  const { t } = useI18n()
  const [branding] = useBranding()
  const [days, setDays] = useState(7)
  const [data, setData] = useState<Report | null>(null)
  const [err, setErr] = useState('')
  const [sec, setSec] = useState('over')
  // за какой период данные уже пришли: другой выбран - значит, считаем
  const [loaded, setLoaded] = useState<number | null>(null)
  const busy = loaded !== days
  const mainRef = useRef<HTMLDivElement>(null)

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

  const go = (id: string) => {
    setSec(id)
    mainRef.current?.scrollTo({ top: 0 })
  }
  const fmtDay = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const mins = (s: number) => (s ? fmtDur(s) : '0')
  const sites = data?.sites
  const servers = data?.servers
  const noise = data?.noise
  const labels = data?.day_labels ?? []
  const chart = labels.length >= 7

  const SECS: SecDef[] = [
    { id: 'over', label: 'Сводка', group: '' },
    ...(sites ? [{ id: 'sites', label: 'Сайты', group: 'Что ломалось' }] : []),
    ...(servers ? [{ id: 'servers', label: 'Серверы', group: 'Что ломалось' }] : []),
    { id: 'noise', label: 'Что шумело', group: 'Алерты' },
    ...(noise && noise.backups.length ? [{ id: 'backups', label: 'Бэкапы', group: 'Алерты' }] : []),
    { id: 'maint', label: 'Окна обслуживания', group: 'Работы' },
  ]
  const side: Record<string, string> = {}
  if (data) {
    if (sites) side.sites = String(sites.affected)
    if (servers) side.servers = String(servers.items.length)
    if (noise) side.noise = String(noise.fires)
    if (noise?.backups.length) side.backups = String(noise.backups.length)
    side.maint = String(data.maint.length)
  }
  const cur = SECS.find((s) => s.id === sec) ?? SECS[0]

  const link = (kind: 'server' | 'check', id: number, name: string) => (
    <button className="linklike" onClick={() => onOpen(kind, id)}>
      {name}
    </button>
  )
  const tile = (to: string, node: ReactNode) => (
    <button className="srv-tile-btn" onClick={() => go(to)} title={t('к разделу')}>
      {node}
    </button>
  )
  const downChart = (withSites: boolean, withServers: boolean) =>
    chart && (
      <DayBars
        t={t}
        labels={labels}
        fmt={mins}
        series={[
          ...(withSites && sites ? [{ key: 's', label: t('простой сайтов'), cls: 'rp-c-sites', values: sites.per_day }] : []),
          ...(withServers && servers
            ? [{ key: 'v', label: t('простой серверов'), cls: 'rp-c-servers', values: servers.per_day }]
            : []),
        ]}
      />
    )
  const alertChart =
    chart && noise && <DayBars t={t} labels={labels} fmt={(n) => String(n)} series={[{ key: 'a', label: t('алертов'), cls: 'rp-c-alerts', values: noise.per_day }]} />

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Отчет')}</h3>
          {data && (
            <span className="muted small">
              {fmtDay(data.since)} - {fmtDay(data.until)}
              {busy ? ` · ${t('считаем...')}` : ''}
            </span>
          )}
          <span className="al-head-btns">
            <button
              className="ghost"
              disabled={!data || busy}
              title={t('Одним HTML-файлом: открывается в любом браузере, можно переслать или распечатать')}
              onClick={() => data && downloadReportHtml(data, t, branding?.title || 'Kervax')}
            >
              {t('Скачать отчет')}
            </button>
            <button className="ghost" onClick={onClose}>
              {t('Закрыть')}
            </button>
          </span>
        </div>

        <div className="al-body">
          <CardNav secs={SECS} active={cur.id} info={{}} side={side} onGo={go} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
              <div className="win-switch">
                {PERIODS.map((d) => (
                  <button key={d} className={`win-btn${d === days ? ' win-btn-active' : ''}`} onClick={() => setDays(d)}>
                    {d === 1 ? t('сутки') : t('{n} дней', { n: d })}
                  </button>
                ))}
              </div>
            </div>
            {err && <p className="form-error">{err}</p>}
            {!data ? (
              !err && <p className="muted small">{t('считаем...')}</p>
            ) : (
              <div className={`srv-sec${busy ? ' rp-busy' : ''}`}>
                <p className="muted small al-lead">{t(LEAD[cur.id])}</p>

                {cur.id === 'over' && (
                  <>
                    <div className="net-tiles srv-tiles">
                      {sites &&
                        tile(
                          'sites',
                          <NetTile
                            label={t('Сайты, средний аптайм')}
                            value={sites.uptime != null ? `${sites.uptime.toFixed(2)}%` : '—'}
                            sub={
                              sites.incidents
                                ? t('сбоев: {n}, лежали {d}', { n: sites.incidents, d: fmtDur(sites.downtime_s) })
                                : t('сбоев не было')
                            }
                            tone={sites.incidents ? 'warn' : 'ok'}
                          >
                            {sites.prev && sites.uptime != null && sites.prev.uptime != null && (
                              <Delta cur={sites.uptime} prev={sites.prev.uptime} unit="pp" />
                            )}
                          </NetTile>,
                        )}
                      {servers &&
                        tile(
                          'servers',
                          <NetTile
                            label={t('Серверы, недоступность')}
                            value={mins(servers.offline_s)}
                            sub={t('пропадали: {n} из {m}', { n: servers.with_gaps, m: servers.total })}
                            tone={servers.offline_s ? 'warn' : 'ok'}
                          >
                            {servers.prev_ok && <Delta cur={servers.offline_s} prev={servers.prev_offline_s} unit="s" lowerBetter />}
                          </NetTile>,
                        )}
                      {noise &&
                        tile(
                          'noise',
                          <NetTile label={t('Алертов пришло')} value={String(noise.fires)} sub={t('без отбоев')}>
                            <Delta cur={noise.fires} prev={noise.prev_fires} unit="n" lowerBetter />
                          </NetTile>,
                        )}
                    </div>
                    {servers && servers.panel_s > 0 && (
                      <div className="al-note">
                        <span>
                          {t('Панель не принимала отчеты {d}, раз: {n}. В простой серверов это не входит.', {
                            d: fmtDur(servers.panel_s),
                            n: servers.panel_gaps,
                          })}
                        </span>
                      </div>
                    )}
                    {chart && (sites || servers) && (
                      <>
                        <div className="srv-nav-grp rt-cap">{t('Простой по дням')}</div>
                        {downChart(true, true)}
                      </>
                    )}
                    {chart && noise && (
                      <>
                        <div className="srv-nav-grp rt-cap">{t('Алерты по дням')}</div>
                        {alertChart}
                      </>
                    )}
                    <div className="srv-nav-grp rt-cap">{t('Хуже всего')}</div>
                    {(() => {
                      const rows: ReactNode[] = []
                      for (const c of (sites?.items ?? []).filter((c) => c.downtime_s > 0).slice(0, 3))
                        rows.push(
                          <tr key={`c${c.id}`}>
                            <td>{link('check', c.id, c.name)}</td>
                            <td className="muted small">{t('сайт')}</td>
                            <td className="small">{t('лежал {d}', { d: fmtDur(c.downtime_s) })}</td>
                          </tr>,
                        )
                      for (const s of (servers?.items ?? []).filter((s) => s.offline_s > 0).slice(0, 3))
                        rows.push(
                          <tr key={`s${s.id}`}>
                            <td>{link('server', s.id, s.name)}</td>
                            <td className="muted small">{t('сервер')}</td>
                            <td className="small">{t('недоступен {d}', { d: fmtDur(s.offline_s) })}</td>
                          </tr>,
                        )
                      for (const x of (noise?.top ?? []).slice(0, 3))
                        rows.push(
                          <tr key={`n${x.kind}|${x.target}`}>
                            <td className="mono small">{x.target}</td>
                            <td className="muted small">{x.label}</td>
                            <td className="small">{t('алертов: {n}', { n: x.n })}</td>
                          </tr>,
                        )
                      return rows.length ? (
                        <table className="sec-tbl">
                          <tbody>{rows}</tbody>
                        </table>
                      ) : (
                        <p className="small form-ok">{t('За период ничего не ломалось и не шумело.')}</p>
                      )
                    })()}
                  </>
                )}

                {cur.id === 'sites' && sites && (
                  <>
                    {downChart(true, false)}
                    {sites.items.length === 0 ? (
                      <p className="muted small">{t('Все {n} мониторов отработали без сбоев', { n: sites.total })}</p>
                    ) : (
                      <>
                        <table className="sec-tbl">
                          <thead>
                            <tr>
                              <th>{t('Монитор')}</th>
                              <th className="num">{t('Аптайм')}</th>
                              <th className="num hide-m">{t('Сбоев')}</th>
                              <th className="num">{t('Лежал')}</th>
                              <th className="num hide-m">{t('Самый долгий')}</th>
                              <th className="num hide-m">{t('Последний')}</th>
                            </tr>
                          </thead>
                          <tbody>
                            {sites.items.map((c) => (
                              <tr key={c.id}>
                                <td>
                                  {link('check', c.id, c.name)}
                                  {c.open && <span className="type-chip rep-open">{t('лежит сейчас')}</span>}
                                </td>
                                <td className="num">{c.uptime != null ? `${c.uptime.toFixed(2)}%` : '—'}</td>
                                <td className="num hide-m">{c.incidents || '—'}</td>
                                <td className="num">{fmtDur(c.downtime_s)}</td>
                                <td className="num hide-m">{fmtDur(c.longest_s)}</td>
                                <td className="num hide-m">{c.last_at ? fmtDay(c.last_at) : '—'}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                        <p className="muted small">
                          {t('Остальные {n} без сбоев', { n: sites.total - sites.affected })}
                          {sites.more > 0 && ` · ${t('и еще {n} со сбоями', { n: sites.more })}`}
                        </p>
                      </>
                    )}
                  </>
                )}

                {cur.id === 'servers' && servers && (
                  <>
                    {servers.panel_s > 0 && (
                      <div className="al-note">
                        <span>
                          {t('Панель не принимала отчеты {d}, раз: {n}. В простой серверов это не входит.', {
                            d: fmtDur(servers.panel_s),
                            n: servers.panel_gaps,
                          })}
                        </span>
                      </div>
                    )}
                    {downChart(false, true)}
                    {servers.items.length === 0 ? (
                      <p className="muted small">{t('Серверы не пропадали, алертов по ним не было')}</p>
                    ) : (
                      <table className="sec-tbl">
                        <thead>
                          <tr>
                            <th>{t('Сервер')}</th>
                            <th className="num">{t('Недоступен')}</th>
                            <th className="num hide-m">{t('Раз')}</th>
                            <th className="num hide-m">{t('Самый долгий')}</th>
                            <th className="num">{t('Алертов')}</th>
                            <th className="hide-m">{t('Чаще всего')}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {servers.items.map((s) => (
                            <tr key={s.id}>
                              <td>{link('server', s.id, s.name)}</td>
                              <td className="num">{fmtDur(s.offline_s)}</td>
                              <td className="num hide-m">{s.gaps || '—'}</td>
                              <td className="num hide-m">{fmtDur(s.longest_s)}</td>
                              <td className="num">{s.alerts || '—'}</td>
                              <td className="hide-m">
                                <span className="ans-chips">
                                  {s.kinds.map((k) => (
                                    <span key={k.kind} className="type-chip">
                                      {k.label} {k.n}
                                    </span>
                                  ))}
                                </span>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </>
                )}

                {cur.id === 'noise' && noise && (
                  <>
                    {alertChart}
                    {noise.top.length === 0 ? (
                      <p className="muted small">{t('Алертов за период не было.')}</p>
                    ) : (
                      <>
                        <div className="srv-nav-grp rt-cap">{t('Чаще всего')}</div>
                        {noise.top.map((x) => (
                          <div key={`${x.kind}|${x.target}`} className="ah-bar rep-bar">
                            <span className="ah-bar-fill" style={{ width: `${(x.n / noise.top[0].n) * 100}%` }} />
                            <span className="ah-bar-label">
                              <span className="mono">{x.target}</span> <span className="muted">· {x.label}</span>
                            </span>
                            <span className="ah-bar-n">{x.n}</span>
                          </div>
                        ))}
                      </>
                    )}
                  </>
                )}

                {cur.id === 'backups' && noise && (
                  <table className="sec-tbl">
                    <tbody>
                      {noise.backups.map((b) => (
                        <tr key={b.target}>
                          <td className="mono">{b.target}</td>
                          <td className="small">{b.kinds.join(', ')}</td>
                          <td className="num small">{t('алертов: {n}', { n: b.n })}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}

                {cur.id === 'maint' &&
                  (data.maint.length === 0 ? (
                    <p className="muted small">{t('Окон обслуживания за период не было.')}</p>
                  ) : (
                    <table className="sec-tbl">
                      <tbody>
                        {data.maint.map((m, i) => (
                          <tr key={i}>
                            <td>{m.title}</td>
                            <td className="small">
                              {m.count > 1
                                ? t('{n} раз, последний {t}', { n: m.count, t: `${fmtDay(m.start)} - ${fmtDay(m.end)}` })
                                : `${fmtDay(m.start)} - ${fmtDay(m.end)}`}
                            </td>
                            <td className="muted small hide-m">
                              {[
                                m.servers ? t('серверов {n}', { n: m.servers }) : '',
                                m.checks ? t('мониторов {n}', { n: m.checks }) : '',
                              ]
                                .filter(Boolean)
                                .join(' · ')}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
