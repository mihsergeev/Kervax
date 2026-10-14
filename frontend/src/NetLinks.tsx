import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import {
  ApiError,
  addLink,
  deleteLink,
  getLinkHistory,
  getLinks,
  getTraffic,
  updateServer,
  type LinkPoint,
  type NetLink,
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
// настоящим сетевым картам с лимитом тарифа (app/traffic.py). Вид - как stat-панели Grafana:
// плитки с крупным числом, строки связей с точкой состояния и мини-графиком за час.

type TFn = (k: string, p?: Record<string, string | number>) => string
export type Tone = 'ok' | 'warn' | 'bad' | 'muted'
const PERIODS = [1, 6, 24, 168]
const REFRESH_MS = 30_000

// Десятичные единицы, как в тарифах хостеров: 20 ТБ = 20 * 10^12 байт
function decParts(n: number): [string, string] {
  const u = byteUnits()
  let v = n
  let i = 0
  while (v >= 1000 && i < u.length - 1) {
    v /= 1000
    i++
  }
  return [v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1).replace(/\.0$/, ''), u[i]]
}
function fmtDec(n: number): string {
  return decParts(n).join(' ')
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

// Плитка с крупным числом: метрика сети, трафик. tone красит число, bad - еще и рамку.
export function NetTile({
  label,
  value,
  unit,
  sub,
  tone,
  title,
  children,
}: {
  label: ReactNode
  value: string
  unit?: string
  sub?: string
  tone?: Tone
  title?: string
  children?: ReactNode
}) {
  return (
    <div className={`net-tile${tone ? ` net-tile-${tone}` : ''}`} title={title}>
      <div className="net-tile-label">{label}</div>
      <div className="net-tile-value">
        {value}
        {unit && <span className="net-tile-unit"> {unit}</span>}
      </div>
      {sub && <div className="net-tile-sub">{sub}</div>}
      {children}
    </div>
  )
}

// Мини-график времени подключения за час: линия по 2-минутным точкам, красные метки внизу -
// где были неудачные попытки
function Spark({ points }: { points: NetLink['spark'] }) {
  const pts = points ?? []
  const W = 120
  const H = 28
  const vals = pts.map((p) => (p ? p[0] : null)).filter((v): v is number => v != null)
  if (!pts.length || (!vals.length && !pts.some((p) => p && p[1] > 0))) return <span className="lk-spark" />
  const lo = Math.min(...vals)
  const hi = Math.max(...vals)
  const span = hi - lo
  const step = W / pts.length
  // ровная линия - посередине, а не прижатой к низу
  const y = (v: number) => (span ? H - 6 - ((v - lo) / span) * (H - 10) : (H - 4) / 2)
  const segs: string[] = []
  let cur = ''
  pts.forEach((p, i) => {
    const v = p ? p[0] : null
    if (v == null) {
      if (cur) segs.push(cur)
      cur = ''
      return
    }
    const x = (i + 0.5) * step
    cur += `${cur ? 'L' : 'M'}${x.toFixed(1)},${y(v).toFixed(1)}`
  })
  if (cur) segs.push(cur)
  return (
    <svg className="lk-spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
      {pts.map((p, i) =>
        p && p[1] > 0 ? (
          <rect key={i} x={i * step + 0.5} y={H - 3} width={Math.max(step - 1, 1)} height={3} className="lk-spark-loss" />
        ) : null,
      )}
      {segs.map((d, i) => (
        <path key={i} d={d} className="lk-spark-line" />
      ))}
    </svg>
  )
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
        <div className="lk-chart-grid">
          <div>
            <div className="net-tile-label">{t('Время подключения')}</div>
            <MultiLineChart
              height={130}
              fmtX={fmtX}
              fmtY={(v) => ms(v, t)}
              yMin={0}
              series={[
                { name: t('медиана'), color: '#5a8fc7', points: p.map((x) => ({ t: x.t, y: x.rtt })) },
                { name: t('худшее'), color: '#d9a441', points: p.map((x) => ({ t: x.t, y: x.rtt_max })) },
              ]}
            />
          </div>
          <div>
            <div className="net-tile-label">{t('Неудачные попытки, %')}</div>
            <MultiLineChart
              height={130}
              fmtX={fmtX}
              yMin={0}
              yMax={100}
              unit="%"
              series={[{ name: t('неудачи'), color: '#d65a5a', points: p.map((x) => ({ t: x.t, y: x.loss })) }]}
            />
          </div>
        </div>
      )}
    </div>
  )
}

function linkView(l: NetLink, agentOk: boolean, t: TFn): { tone: Tone; value: string; sub?: string } {
  const loss = l.loss ?? 0
  if (!l.fresh) return { tone: 'muted', value: agentOk ? t('ждем первую проверку') : t('нет данных') }
  if (l.held) return { tone: 'muted', value: t('сервер сам молчит') }
  if (loss >= 100) return { tone: 'bad', value: t('нет связи'), sub: l.err || undefined }
  if (loss >= 20) return { tone: 'bad', value: t('теряет {p}%', { p: Math.round(loss) }), sub: l.err || undefined }
  if (loss > 0) return { tone: 'warn', value: ms(l.rtt, t), sub: t('неудач {p}%', { p: Math.round(loss) }) }
  return { tone: 'ok', value: ms(l.rtt, t) }
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
  const canAdd = !isViewer && data.agent_ok && data.links.length < data.max
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
  return (
    <div className="chart-card net-card" id="mcard-links">
      <div className="net-head">
        <div className="chart-cap">{t('Связь с другими серверами')}</div>
        {canAdd && !adding && (
          <button className="ghost small row-act" onClick={() => setAdding(true)}>
            + {t('Добавить связь')}
          </button>
        )}
      </div>
      {prob && <div className="net-prob">{prob.text}</div>}
      {!data.agent_ok && <div className="muted small">{t('Связь проверяет агент 2.31 и новее: обновите агент.')}</div>}
      {data.links.length === 0 && !adding && (
        <div className="muted small">
          {t('Агент будет раз в 30 секунд открывать TCP-соединение с адресом и мерить время: вход VPN с выходом, приложение с базой. Алерт, если связь пропала или теряет попытки.')}
        </div>
      )}
      {data.links.length > 0 && (
        <div className="loc-results">
          {data.links.map((l) => {
            const v = linkView(l, data.agent_ok, t)
            return (
              <div key={l.id} className={`loc-res lk-row${open === l.id ? ' lk-row-open' : ''}`}>
                <div
                  className="lk-main"
                  role="button"
                  tabIndex={0}
                  title={t('График за период')}
                  onClick={() => setOpen(open === l.id ? null : l.id)}
                  onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setOpen(open === l.id ? null : l.id)}
                >
                  <span className={`lk-dot lk-dot-${v.tone}`} />
                  <div className="lk-name">
                    <div className="lk-title">
                      <span className="lk-target">{l.target_name ?? l.host}</span>
                      <span className="muted">:{l.port}</span>
                      {l.note && <span className="lk-note">{l.note}</span>}
                    </div>
                    {l.target_name && <div className="muted mono lk-host">{l.host}</div>}
                  </div>
                  <Spark points={l.spark} />
                  <div className={`lk-val net-tone-${v.tone}`}>
                    <div>{v.value}</div>
                    {v.sub && <div className="lk-val-sub">{v.sub}</div>}
                  </div>
                  {!isViewer && (
                    <button
                      className="lk-del"
                      title={t('Убрать')}
                      onClick={(e) => {
                        e.stopPropagation()
                        remove(l.id, l.name)
                      }}
                    >
                      ×
                    </button>
                  )}
                </div>
                {open === l.id && <LinkChart serverId={s.id} linkId={l.id} />}
              </div>
            )
          })}
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
  // рисуем в настоящую ширину карточки: в растянутом viewBox подписи осей росли вместе с ней
  const box = useRef<HTMLDivElement>(null)
  const [W, setW] = useState(640)
  const has = days.length > 0
  useEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.round(e.contentRect.width))))
    ro.observe(el)
    return () => ro.disconnect()
  }, [has])
  if (!has) return null
  const H = 150
  const L = 52
  const B = 18
  const max = Math.max(...days.map((d) => d.rx + d.tx), 1)
  // круглая верхняя граница сетки: 1, 2, 5 x 10^n
  const pow = 10 ** Math.floor(Math.log10(max))
  const top = [1, 2, 5, 10].map((k) => k * pow).find((v) => v >= max) ?? max
  // меньше двух недель - столбики не растягиваем на всю ширину, иначе один день - плита
  const bw = (W - L - 4) / Math.max(days.length, 14)
  const y = (v: number) => H - B - (v / top) * (H - B - 6)
  const grid = [0, 0.25, 0.5, 0.75, 1].map((k) => k * top)
  const d = hi != null ? days[hi] : null
  return (
    <div className="lk-bars" ref={box}>
      <div className="lk-legend">
        <span className="net-tile-label">{t('По дням')}</span>
        <span className="lk-sw lk-tx" /> {t('исходящий')} <span className="lk-sw lk-rx" /> {t('входящий')}
        <span className="lk-tip">{d ? `${ddmm(d.day)}: ↑ ${fmtDec(d.tx)} · ↓ ${fmtDec(d.rx)}` : ''}</span>
      </div>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="lk-svg" onMouseLeave={() => setHi(null)}>
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
            <rect x={L + i * bw} y={6} width={bw} height={H - B - 6} className={hi === i ? 'lk-hover' : 'lk-hit'} />
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
  const over = !!sum && sum.limit > 0 && (sum.forecast ?? 0) > sum.limit
  const limTone: Tone | undefined = pct >= 100 ? 'bad' : pct >= 90 || over ? 'warn' : undefined
  const what = data.count === 'both' ? t('весь трафик') : t('исходящий')
  const [txV, txU] = decParts(sum?.tx ?? 0)
  const [rxV, rxU] = decParts(sum?.rx ?? 0)
  const fc = sum?.forecast != null ? decParts(sum.forecast) : null
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-traffic">
      <div className="net-head">
        <div className="chart-cap">
          {t('Трафик за месяц')}
          {sum && <span className="net-period">{ddmm(sum.start)}-{ddmm(prevDay(sum.end))}</span>}
        </div>
        {!isViewer && !edit && (
          <button className="ghost small row-act" onClick={startEdit}>
            {data.limit_gb ? t('Изменить лимит тарифа') : t('Задать лимит тарифа')}
          </button>
        )}
      </div>
      {prob && <div className="net-prob">{prob.text}</div>}
      {!data.agent_ok && <div className="muted small">{t('Трафик считает агент 2.31 и новее: обновите агент.')}</div>}
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
      {sum && (
        <div className="net-tiles">
          <NetTile label={t('Исходящий')} value={txV} unit={txU} sub={t('за период')} />
          <NetTile label={t('Входящий')} value={rxV} unit={rxU} sub={t('за период')} />
          {sum.limit > 0 ? (
            <NetTile
              label={t('Лимит тарифа')}
              value={`${pct >= 10 ? Math.round(pct) : pct.toFixed(1)}%`}
              sub={t('{u} из {l}, считается {what}', { u: fmtDec(sum.used), l: fmtDec(sum.limit), what })}
              tone={limTone}
            >
              <div className="lk-meter">
                <span style={{ width: `${Math.min(pct, 100)}%` }} className={pct >= 100 ? 'bad' : pct >= 90 ? 'warn' : ''} />
              </div>
            </NetTile>
          ) : (
            <NetTile label={t('Лимит тарифа')} value={t('нет')} sub={t('только данные, без алертов')} tone="muted" />
          )}
          <NetTile
            label={t('К концу периода')}
            value={fc ? fc[0] : '—'}
            unit={fc ? fc[1] : undefined}
            sub={fc ? t('при нынешнем темпе') : t('прогноз появится через трое суток данных')}
            tone={over ? 'warn' : fc ? undefined : 'muted'}
          />
        </div>
      )}
      <DayBars days={data.days} t={t} />
      <div className="net-foot">
        {sum && !sum.full && data.since && (
          <div>
            {t('Считаем с {d}: раньше агент счетчики не отдавал, начало периода не вошло.', {
              d: new Date(data.since).toLocaleDateString([], { day: '2-digit', month: '2-digit' }),
            })}
          </div>
        )}
        {data.periods.length > 1 && (
          <div>
            {t('Прошлые периоды')}:{' '}
            {data.periods
              .slice(0, -1)
              .reverse()
              .slice(0, 6)
              .map((p) => `${ddmm(p.start)} ↑ ${fmtDec(p.tx)} ↓ ${fmtDec(p.rx)}`)
              .join(' · ')}
          </div>
        )}
        <div>
          {data.ifaces.length
            ? t('Считается по сетевым картам: {list}. Туннели, мосты docker и veth не входят, иначе один пакет считался бы два-три раза.', {
                list: data.ifaces.join(', '),
              })
            : t('Агент не нашел у сервера настоящих сетевых карт.')}
        </div>
      </div>
    </div>
  )
}
