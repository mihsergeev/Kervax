import { Fragment, useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  addLink,
  deleteLink,
  getLinkHistory,
  getLinks,
  getTraffic,
  updateServer,
  type LinkPoint,
  type NetLinksOut,
  type Server,
  type TrafficOut,
} from './api'
import { useAuth } from './auth'
import { MultiLineChart } from './charts/MultiLineChart'
import { useI18n } from './i18n'
import { byteUnits } from './units'

// Связь с другими серверами (агент 2.31, app/netlinks.py): раз в 30 секунд агент трижды
// пробует открыть TCP-соединение с адресом и меряет время рукопожатия. И трафик за месяц по
// настоящим сетевым картам с лимитом тарифа (app/traffic.py).

type TFn = (k: string, p?: Record<string, string | number>) => string
const PERIODS = [1, 6, 24, 168]
const REFRESH_MS = 30_000

// Десятичные единицы, как в тарифах хостеров: 20 ТБ = 20 * 10^12 байт
function fmtDec(n: number): string {
  const u = byteUnits()
  let v = n
  let i = 0
  while (v >= 1000 && i < u.length - 1) {
    v /= 1000
    i++
  }
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1).replace(/\.0$/, '')} ${u[i]}`
}

function ddmm(iso: string): string {
  const [, m, d] = iso.split('-')
  return `${d}.${m}`
}

function prevDay(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() - 1)
  return d.toISOString().slice(0, 10)
}

function ms(v: number | null | undefined, t: TFn): string {
  if (v == null) return '—'
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${t('мс')}`
}

function LinkChart({ serverId, linkId }: { serverId: number; linkId: number }) {
  const { t } = useI18n()
  const [hours, setHours] = useState(24)
  // точки помнят, за какой период загружены: сменили период - до ответа показываем загрузку
  const [res, setRes] = useState<{ h: number; pts: LinkPoint[] } | null>(null)
  useEffect(() => {
    let alive = true
    getLinkHistory(serverId, linkId, hours)
      .then((r) => alive && setRes({ h: hours, pts: r.points }))
      .catch(() => alive && setRes({ h: hours, pts: [] }))
    return () => {
      alive = false
    }
  }, [serverId, linkId, hours])
  const pts = res && res.h === hours ? res.pts : null
  const p = (pts ?? []).map((x) => ({ ...x, t: Date.parse(x.ts) }))
  const fmtX = hours > 24
    ? (v: number) => new Date(v).toLocaleDateString([], { day: '2-digit', month: '2-digit' })
    : undefined
  return (
    <div className="lk-chart">
      <div className="win-switch">
        {PERIODS.map((h) => (
          <button key={h} className={`win-btn${h === hours ? ' win-btn-active' : ''}`} onClick={() => setHours(h)}>
            {h < 24 ? t('{n} ч', { n: h }) : t('{n} дн', { n: h / 24 })}
          </button>
        ))}
      </div>
      {pts == null ? (
        <div className="muted small">{t('загрузка…')}</div>
      ) : (
        <>
          <div className="muted small">{t('Время подключения')}</div>
          <MultiLineChart
            height={120}
            fmtX={fmtX}
            fmtY={(v) => ms(v, t)}
            yMin={0}
            series={[
              { name: t('медиана'), color: '#5a8fc7', points: p.map((x) => ({ t: x.t, y: x.rtt })) },
              { name: t('худшее'), color: '#d9a441', points: p.map((x) => ({ t: x.t, y: x.rtt_max })) },
            ]}
          />
          <div className="muted small">{t('Неудачные попытки, %')}</div>
          <MultiLineChart
            height={90}
            fmtX={fmtX}
            yMin={0}
            yMax={100}
            unit="%"
            series={[{ name: t('неудачи'), color: '#d65a5a', points: p.map((x) => ({ t: x.t, y: x.loss })) }]}
          />
        </>
      )}
    </div>
  )
}

export function LinksCard({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [data, setData] = useState<NetLinksOut | null>(null)
  const [open, setOpen] = useState<number | null>(null)
  const [adding, setAdding] = useState(false)
  const [target, setTarget] = useState<string>('')
  const [host, setHost] = useState('')
  const [port, setPort] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const load = useCallback(() => {
    getLinks(s.id).then(setData).catch(() => {})
  }, [s.id])
  useEffect(() => {
    load()
    const h = window.setInterval(load, REFRESH_MS)
    return () => window.clearInterval(h)
  }, [load])
  if (!data || (!data.agent_ok && data.links.length === 0)) return null
  const prob = (s.problems ?? []).find((p) => p.kind === 'link')
  const pickTarget = (v: string) => {
    setTarget(v)
    const tg = data.targets.find((x) => String(x.id) === v)
    if (tg) setHost(tg.ip || tg.local_ip || '')
  }
  const submit = async () => {
    setBusy(true)
    setErr('')
    try {
      await addLink(s.id, {
        target_server_id: target ? Number(target) : null,
        host: host.trim(),
        port: Number(port),
        note: note.trim(),
      })
      setAdding(false)
      setTarget('')
      setHost('')
      setPort('')
      setNote('')
      load()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t('не вышло'))
    } finally {
      setBusy(false)
    }
  }
  const remove = async (id: number, name: string) => {
    if (!window.confirm(t('Убрать связь с {name}? История ее проверок удалится.', { name }))) return
    try {
      await deleteLink(s.id, id)
      if (open === id) setOpen(null)
      load()
    } catch {
      /* кнопка остается, можно повторить */
    }
  }
  const state = (l: NetLinksOut['links'][number]) => {
    if (!l.fresh) return <span className="muted">{data.agent_ok ? t('ждем первую проверку') : t('нет данных')}</span>
    if (l.held) return <span className="muted">{t('сервер сам молчит')}</span>
    if ((l.loss ?? 0) >= 100) return <span className="t-down">{t('нет связи')}{l.err ? `: ${l.err}` : ''}</span>
    if ((l.loss ?? 0) >= 20)
      return (
        <span className="t-down">
          {t('теряет {p}% попыток', { p: Math.round(l.loss ?? 0) })}
          {l.err ? ` (${l.err})` : ''}
        </span>
      )
    if ((l.loss ?? 0) > 0)
      return (
        <span className="t-degraded">
          {ms(l.rtt, t)} · {t('неудач {p}%', { p: Math.round(l.loss ?? 0) })}
        </span>
      )
    return <span className="t-up">{ms(l.rtt, t)}</span>
  }
  return (
    <div className="detail-inc" id="mcard-links">
      <div className="chart-cap">{t('Связь с другими серверами')}</div>
      {prob && <div className="t-down small">{prob.text}</div>}
      {!data.agent_ok && (
        <div className="muted small">{t('Связь проверяет агент 2.31 и новее: обновите агент.')}</div>
      )}
      {data.links.length === 0 ? (
        <div className="muted small">
          {t('Агент будет раз в 30 секунд открывать TCP-соединение с адресом и мерить время: вход VPN с выходом, приложение с базой. Алерт, если связь пропала или теряет попытки.')}
        </div>
      ) : (
        <table className="exp-table psi-table small lk-table">
          <tbody>
            {data.links.map((l) => (
              <Fragment key={l.id}>
                <tr>
                  <td>
                    <button className="linklike" onClick={() => setOpen(open === l.id ? null : l.id)}>
                      {l.target_name ?? l.host}
                      <span className="muted">:{l.port}</span>
                    </button>
                    {l.note && <span className="muted"> {l.note}</span>}
                    {l.target_name && <div className="muted mono lk-host">{l.host}</div>}
                  </td>
                  <td>{state(l)}</td>
                  {!isViewer && (
                    <td className="lk-act">
                      <button className="ghost small row-act" title={t('Убрать')} onClick={() => remove(l.id, l.name)}>
                        ×
                      </button>
                    </td>
                  )}
                </tr>
                {open === l.id && (
                  <tr className="lk-chart-row">
                    <td colSpan={isViewer ? 2 : 3}>
                      <LinkChart serverId={s.id} linkId={l.id} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
      {!isViewer && data.agent_ok && !adding && data.links.length < data.max && (
        <div className="unit-actions small">
          <button className="ghost small row-act" onClick={() => setAdding(true)}>
            {t('Добавить связь')}
          </button>
        </div>
      )}
      {adding && (
        <div className="lk-form small">
          <select value={target} onChange={(e) => pickTarget(e.target.value)}>
            <option value="">{t('другой адрес')}</option>
            {data.targets.map((x) => (
              <option key={x.id} value={x.id}>
                {x.name}
              </option>
            ))}
          </select>
          <input
            className="mono"
            placeholder={t('адрес')}
            value={host}
            onChange={(e) => setHost(e.target.value)}
            maxLength={253}
          />
          <input
            className="mono lk-port"
            placeholder={t('порт')}
            inputMode="numeric"
            value={port}
            onChange={(e) => setPort(e.target.value.replace(/\D/g, '').slice(0, 5))}
          />
          <input placeholder={t('что это (необязательно)')} value={note} onChange={(e) => setNote(e.target.value)} maxLength={64} />
          <button className="small" disabled={busy || !host.trim() || !port} onClick={submit}>
            {t('Добавить')}
          </button>
          <button className="ghost small" onClick={() => setAdding(false)}>
            {t('Отмена')}
          </button>
          {err && <div className="t-down">{err}</div>}
        </div>
      )}
    </div>
  )
}

function DayBars({ days, t }: { days: TrafficOut['days']; t: TFn }) {
  const [hi, setHi] = useState<number | null>(null)
  if (days.length === 0) return null
  const W = 640
  const H = 120
  const L = 52
  const B = 18
  const max = Math.max(...days.map((d) => d.rx + d.tx), 1)
  // круглая верхняя граница сетки: 1, 2, 5 x 10^n
  const pow = 10 ** Math.floor(Math.log10(max))
  const top = [1, 2, 5, 10].map((k) => k * pow).find((v) => v >= max) ?? max
  const bw = (W - L - 4) / days.length
  const y = (v: number) => H - B - (v / top) * (H - B - 6)
  const grid = [0, 0.25, 0.5, 0.75, 1].map((k) => k * top)
  const d = hi != null ? days[hi] : null
  return (
    <div className="lk-bars">
      <svg viewBox={`0 0 ${W} ${H}`} className="lk-svg" onMouseLeave={() => setHi(null)}>
        {grid.map((g) => (
          <g key={g}>
            <line x1={L} x2={W} y1={y(g)} y2={y(g)} className="lk-grid" />
            <text x={L - 4} y={y(g) + 3} className="lk-axis" textAnchor="end">
              {g ? fmtDec(g) : '0'}
            </text>
          </g>
        ))}
        {days.map((x, i) => (
          <g key={x.day} onMouseEnter={() => setHi(i)}>
            <rect x={L + i * bw} y={6} width={bw} height={H - B - 6} fill="transparent" />
            <rect x={L + i * bw + 1} y={y(x.tx)} width={Math.max(bw - 2, 1)} height={H - B - y(x.tx)} className="lk-tx" />
            <rect
              x={L + i * bw + 1}
              y={y(x.tx + x.rx)}
              width={Math.max(bw - 2, 1)}
              height={y(x.tx) - y(x.tx + x.rx)}
              className="lk-rx"
            />
            {(i % Math.ceil(days.length / 8) === 0 || i === days.length - 1) && (
              <text x={L + i * bw + bw / 2} y={H - 4} className="lk-axis" textAnchor="middle">
                {ddmm(x.day)}
              </text>
            )}
          </g>
        ))}
      </svg>
      <div className="muted small lk-legend">
        <span className="lk-sw lk-tx" /> {t('исходящий')} <span className="lk-sw lk-rx" /> {t('входящий')}
        {d && (
          <span className="lk-tip">
            {ddmm(d.day)}: ↑ {fmtDec(d.tx)} · ↓ {fmtDec(d.rx)}
          </span>
        )}
      </div>
    </div>
  )
}

export function TrafficCard({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [data, setData] = useState<TrafficOut | null>(null)
  const [edit, setEdit] = useState(false)
  const [limit, setLimit] = useState('')
  const [unit, setUnit] = useState<'gb' | 'tb'>('tb')
  const [count, setCount] = useState<'out' | 'both'>('out')
  const [cycle, setCycle] = useState('1')
  const [err, setErr] = useState('')
  const load = useCallback(() => {
    getTraffic(s.id).then(setData).catch(() => {})
  }, [s.id])
  useEffect(() => {
    load()
    const h = window.setInterval(load, 10 * 60_000)
    return () => window.clearInterval(h)
  }, [load])
  if (!data || (!data.agent_ok && data.days.length === 0)) return null
  const sum = data.sum
  const prob = (s.problems ?? []).find((p) => p.kind === 'traffic')
  const startEdit = () => {
    const gb = data.limit_gb
    const tb = gb >= 1000 && gb % 100 === 0
    setLimit(gb ? String(tb ? gb / 1000 : gb) : '')
    setUnit(gb && !tb ? 'gb' : 'tb')
    setCount(data.count === 'both' ? 'both' : 'out')
    setCycle(String(data.cycle_day || 1))
    setErr('')
    setEdit(true)
  }
  const save = async () => {
    const v = parseFloat(limit.replace(',', '.')) || 0
    const gb = Math.round(unit === 'tb' ? v * 1000 : v)
    const cd = Math.min(Math.max(parseInt(cycle, 10) || 1, 1), 28)
    try {
      await updateServer(s.id, { traffic_limit_gb: gb, traffic_count: count, traffic_cycle_day: cd })
      setEdit(false)
      load()
      onChanged()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t('не вышло'))
    }
  }
  const pct = sum && sum.limit ? (sum.used / sum.limit) * 100 : 0
  const tone = pct >= 100 ? 't-down' : pct >= 90 || (sum?.forecast ?? 0) > (sum?.limit || Infinity) ? 't-degraded' : ''
  const what = data.count === 'both' ? t('весь трафик') : t('исходящий')
  return (
    <div className="detail-inc" id="mcard-traffic">
      <div className="chart-cap">
        {t('Трафик за месяц')}
        {sum && <span className="muted"> {ddmm(sum.start)}-{ddmm(prevDay(sum.end))}</span>}
      </div>
      {prob && <div className="t-down small">{prob.text}</div>}
      {!data.agent_ok && <div className="muted small">{t('Трафик считает агент 2.31 и новее: обновите агент.')}</div>}
      {sum && (
        <table className="exp-table psi-table small">
          <tbody>
            <tr>
              <td>{t('Исходящий')}</td>
              <td>{fmtDec(sum.tx)}</td>
            </tr>
            <tr>
              <td>{t('Входящий')}</td>
              <td>{fmtDec(sum.rx)}</td>
            </tr>
            {sum.limit > 0 && (
              <tr>
                <td>{t('Лимит тарифа')}</td>
                <td className={tone}>
                  {t('{u} из {l} ({p}%), считается {what}', {
                    u: fmtDec(sum.used),
                    l: fmtDec(sum.limit),
                    p: pct >= 10 ? Math.round(pct) : pct.toFixed(1),
                    what,
                  })}
                  <div className="lk-meter">
                    <span style={{ width: `${Math.min(pct, 100)}%` }} className={pct >= 100 ? 'bad' : pct >= 90 ? 'warn' : ''} />
                  </div>
                </td>
              </tr>
            )}
            <tr>
              <td>{t('К концу периода')}</td>
              <td className={sum.limit && (sum.forecast ?? 0) > sum.limit ? 't-degraded' : ''}>
                {sum.forecast != null
                  ? t('около {v} при нынешнем темпе', { v: fmtDec(sum.forecast) })
                  : t('прогноз появится через трое суток данных')}
              </td>
            </tr>
          </tbody>
        </table>
      )}
      {sum && !sum.full && data.since && (
        <div className="muted small">
          {t('Считаем с {d}: раньше агент счетчики не отдавал, начало периода не вошло.', {
            d: new Date(data.since).toLocaleDateString([], { day: '2-digit', month: '2-digit' }),
          })}
        </div>
      )}
      <DayBars days={data.days} t={t} />
      {data.periods.length > 1 && (
        <div className="muted small lk-periods">
          {t('Прошлые периоды')}:{' '}
          {data.periods
            .slice(0, -1)
            .reverse()
            .slice(0, 6)
            .map((p) => `${ddmm(p.start)} ↑ ${fmtDec(p.tx)} ↓ ${fmtDec(p.rx)}`)
            .join(' · ')}
        </div>
      )}
      <div className="muted small">
        {data.ifaces.length
          ? t('Считается по сетевым картам: {list}. Туннели, мосты docker и veth не входят, иначе один пакет считался бы два-три раза.', {
              list: data.ifaces.join(', '),
            })
          : t('Агент не нашел у сервера настоящих сетевых карт.')}
      </div>
      {!isViewer && !edit && (
        <div className="unit-actions small">
          <button className="ghost small row-act" onClick={startEdit}>
            {data.limit_gb ? t('Изменить лимит тарифа') : t('Задать лимит тарифа')}
          </button>
        </div>
      )}
      {edit && (
        <div className="lk-form small">
          <input
            className="lk-port"
            inputMode="decimal"
            placeholder={t('лимит')}
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
          />
          <select value={unit} onChange={(e) => setUnit(e.target.value as 'gb' | 'tb')}>
            <option value="tb">{byteUnits()[4]}</option>
            <option value="gb">{byteUnits()[3]}</option>
          </select>
          <select value={count} onChange={(e) => setCount(e.target.value as 'out' | 'both')}>
            <option value="out">{t('хостер считает исходящий')}</option>
            <option value="both">{t('хостер считает весь трафик')}</option>
          </select>
          <label>
            {t('период с')}{' '}
            <input
              className="lk-port"
              inputMode="numeric"
              value={cycle}
              onChange={(e) => setCycle(e.target.value.replace(/\D/g, '').slice(0, 2))}
            />{' '}
            {t('числа')}
          </label>
          <button className="small" onClick={save}>
            {t('Сохранить')}
          </button>
          <button className="ghost small" onClick={() => setEdit(false)}>
            {t('Отмена')}
          </button>
          <div className="muted">{t('Пустой или 0 - без лимита: только данные, без алертов.')}</div>
          {err && <div className="t-down">{err}</div>}
        </div>
      )}
    </div>
  )
}
