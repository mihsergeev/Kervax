import { useCallback, useEffect, useState } from 'react'
import { getUsage, type DiskWriter, type NetUser, type Server, type ServerReport, type UsageName, type UsageOut } from './api'
import { useI18n } from './i18n'
import { fmtDec } from './NetLinks'
import { fmtBytes } from './units'

// Кто качает трафик и кто пишет на диск (app/usage.py): строка на контейнер, под или юнит - сейчас
// в час из отчета агента, за сутки и за неделю из истории панели, столбики по дням недели.

type TFn = (k: string, p?: Record<string, string | number>) => string
type Row<L> = { name: string; kind: string; live?: L; hist?: UsageName }
const SHOW = 8
const REFRESH_MS = 10 * 60_000

function useUsage(id: number, metric: 'net' | 'disk'): UsageOut | null {
  const [data, setData] = useState<UsageOut | null>(null)
  const load = useCallback(() => {
    getUsage(id, metric).then(setData).catch(() => {})
  }, [id, metric])
  useEffect(() => {
    load()
    const h = window.setInterval(load, REFRESH_MS)
    return () => window.clearInterval(h)
  }, [load])
  return data
}

function ddmm(iso: string): string {
  const [, m, d] = iso.split('-')
  return `${d}.${m}`
}

function kindLabel(kind: string, name: string, t: TFn): string {
  if (name === 'user.slice') return t('сессии и cron')
  return kind === 'container' ? t('контейнер') : kind === 'pod' ? t('под') : kind === 'unit' ? t('юнит') : ''
}

// сейчас (топ агента за час) и история панели - одной строкой на имя
function merge<L extends { name: string; kind: string }>(live: L[], hist: UsageName[] | undefined): Row<L>[] {
  const m = new Map<string, Row<L>>()
  for (const h of hist ?? []) m.set(h.name, { name: h.name, kind: h.kind, hist: h })
  for (const l of live) {
    const r = m.get(l.name)
    if (r) {
      r.live = l
      r.kind = l.kind
    } else m.set(l.name, { name: l.name, kind: l.kind, live: l })
  }
  return [...m.values()]
}

// Столбики по дням недели, у строки свой масштаб: видно, в какие дни он качал или писал, а
// сравнивать строки - по цифрам справа. Сегодня бледнее: сутки еще идут
function WeekBars({ vals, days, fmt, cls }: { vals: number[]; days: string[]; fmt: (n: number) => string; cls: string }) {
  const max = Math.max(...vals, 1)
  return (
    <div className="ub-days">
      {vals.map((v, i) => (
        <i
          key={days[i]}
          className={v > 0 ? `${cls}${i === vals.length - 1 ? ' ub-today' : ''}` : 'ub-zero'}
          style={v > 0 ? { height: `${Math.max(10, (v / max) * 100)}%` } : undefined}
          title={`${ddmm(days[i])}: ${v > 0 ? fmt(v) : '0'}`}
        />
      ))}
    </div>
  )
}

function SinceNote({ hist, t }: { hist: UsageOut | null; t: TFn }) {
  if (!hist?.since || !hist.days.length) return null
  const since = new Date(hist.since)
  const first = new Date(`${hist.days[0]}T23:59:59`)
  if (since <= first) return null
  return (
    <div>
      {t('История по дням копится с {d}.', { d: since.toLocaleDateString([], { day: '2-digit', month: '2-digit' }) })}
    </div>
  )
}

function ShowAll({ n, all, setAll, t }: { n: number; all: boolean; setAll: (v: boolean) => void; t: TFn }) {
  if (n <= SHOW) return null
  return (
    <button className="ghost small ub-more" onClick={() => setAll(!all)}>
      {all ? t('свернуть') : `${t('показать все')} (${n})`}
    </button>
  )
}

const perH = (v: number, s: number) => (v * 3600) / s

export function NetUsersCard({ server: s, r }: { server: Server; r: ServerReport }) {
  const { t } = useI18n()
  const hist = useUsage(s.id, 'net')
  const [all, setAll] = useState(false)
  const live = (r.net_users ?? []).filter((u) => u.s > 0 && (u.rx > 0 || u.tx > 0))
  const rows = merge<NetUser>(live, hist?.names)
  if (!rows.length) return null
  // как считает хостер: только исходящий или весь
  const out = (s.traffic_count ?? 'out') !== 'both'
  const counted = (rx: number, tx: number) => (out ? tx : rx + tx)
  const dayOf = (x: Row<NetUser>) =>
    x.hist ? counted(x.hist.d24[0], x.hist.d24[1]) : x.live ? counted(perH(x.live.rx, x.live.s), perH(x.live.tx, x.live.s)) * 24 : 0
  rows.sort((a, b) => dayOf(b) - dayOf(a))
  const days = hist?.days ?? []
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-netusers">
      <div className="net-head">
        <div className="chart-cap">{t('Кто качает трафик')}</div>
        <span className="muted small">{out ? t('по исходящему, как считает хостер') : t('по всему трафику, как считает хостер')}</span>
      </div>
      <div className="ub-list">
        {(all ? rows : rows.slice(0, SHOW)).map((x) => (
          <div key={x.name} className="ub-row">
            <div className="ub-main">
              <div className="ub-name mono">{x.name}</div>
              <div className="muted small">{[kindLabel(x.kind, x.name, t), x.live ? '' : t('сейчас тихо')].filter(Boolean).join(' · ')}</div>
            </div>
            {x.hist && days.length ? (
              <WeekBars vals={x.hist.days.map(([rx, tx]) => counted(rx, tx))} days={days} fmt={fmtDec} cls="ub-net" />
            ) : (
              <span />
            )}
            <div className="ub-nums mono small">
              {x.live && (
                <div>{t('↑ {tx} ↓ {rx} в час', { tx: fmtDec(perH(x.live.tx, x.live.s)), rx: fmtDec(perH(x.live.rx, x.live.s)) })}</div>
              )}
              <div className="muted">
                {x.hist
                  ? t('за сутки ↑ {tx} ↓ {rx}', { tx: fmtDec(x.hist.d24[1]), rx: fmtDec(x.hist.d24[0]) })
                  : x.live
                    ? t('около ↑ {tx} ↓ {rx} в сутки', { tx: fmtDec(perH(x.live.tx, x.live.s) * 24), rx: fmtDec(perH(x.live.rx, x.live.s) * 24) })
                    : ''}
              </div>
            </div>
          </div>
        ))}
      </div>
      <ShowAll n={rows.length} all={all} setAll={setAll} t={t} />
      <div className="net-foot">
        <SinceNote hist={hist} t={t} />
        <div>
          {t(
            'Здесь все, что прошло через интерфейсы контейнера, вместе с обменом с другими контейнерами этого сервера: база, которая отдает приложению гигабайты, тариф не тратит. В тариф идет только то, что ушло через сетевую карту. Контейнеры с сетью хоста по отдельности не видны.',
          )}
        </div>
      </div>
    </div>
  )
}

export function DiskWritersBlock({ server: s, r }: { server: Server; r: ServerReport }) {
  const { t } = useI18n()
  const hist = useUsage(s.id, 'disk')
  const [all, setAll] = useState(false)
  const live = (r.disk_writers ?? []).filter((w) => w.s > 0 && (w.w > 0 || w.r > 0))
  const rows = merge<DiskWriter>(live, hist?.names)
  if (!rows.length) return null
  const dayOf = (x: Row<DiskWriter>) => (x.hist ? x.hist.d24[0] : x.live ? perH(x.live.w, x.live.s) * 24 : 0)
  rows.sort((a, b) => dayOf(b) - dayOf(a))
  const days = hist?.days ?? []
  const win = live.length ? Math.max(...live.map((w) => w.s)) : 0
  return (
    <div className="chart-card net-card net-card-wide" id="mcard-diskwrite">
      <div className="net-head">
        <div className="chart-cap">{t('Кто пишет на диск')}</div>
        <span className="muted small">
          {win && win < 3000
            ? t('за {n} мин, в пересчете на час', { n: Math.max(1, Math.round(win / 60)) })
            : t('в час, за сутки и за неделю')}
        </span>
      </div>
      <div className="ub-list">
        {(all ? rows : rows.slice(0, SHOW)).map((x) => (
          <div key={x.name} className="ub-row">
            <div className="ub-main">
              <div className="ub-name mono">{x.name === 'user.slice' ? kindLabel(x.kind, x.name, t) : x.name}</div>
              <div className="muted small">
                {[
                  x.name === 'user.slice' ? 'user.slice' : kindLabel(x.kind, x.name, t),
                  x.live && x.live.r > 0 ? t('чтение {s} в час', { s: fmtBytes(perH(x.live.r, x.live.s)) }) : '',
                  x.live ? '' : t('сейчас не пишет'),
                ]
                  .filter(Boolean)
                  .join(' · ')}
              </div>
            </div>
            {x.hist && days.length ? (
              <WeekBars vals={x.hist.days.map(([w]) => w)} days={days} fmt={fmtBytes} cls="ub-disk" />
            ) : (
              <span />
            )}
            <div className="ub-nums mono small">
              {x.live && <div>{t('запись {h} в час', { h: fmtBytes(perH(x.live.w, x.live.s)) })}</div>}
              <div className="muted">
                {x.hist
                  ? t('за сутки {d}, за неделю {w}', { d: fmtBytes(x.hist.d24[0]), w: fmtBytes(x.hist.week[0]) })
                  : x.live
                    ? t('около {d} в сутки', { d: fmtBytes(perH(x.live.w, x.live.s) * 24) })
                    : ''}
              </div>
            </div>
          </div>
        ))}
      </div>
      <ShowAll n={rows.length} all={all} setAll={setAll} t={t} />
      <div className="net-foot">
        <SinceNote hist={hist} t={t} />
        {r.disk_writers_v1 && (
          <div>{t('cgroup v1: в эти цифры попадает только прямая запись, отложенная через кэш страниц не видна')}</div>
        )}
      </div>
    </div>
  )
}
