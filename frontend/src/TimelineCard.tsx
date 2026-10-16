import { useEffect, useMemo, useState } from 'react'
import { ApiError, getServerTimeline, type TimelineEvent, type TimelineResult } from './api'
import { actLabel } from './auditActs'
import { useI18n } from './i18n'

// Хронология сервера (backend app/timeline.py): деплои и контейнеры, система, порты, доступ,
// алерты и действия в панели одной лентой. Первый вопрос при разборе сбоя - что поменялось.

const PERIODS = [1, 7, 30, 90]
// фильтры: группа -> какие категории событий в нее входят
const GROUPS: { key: string; cats: string[] }[] = [
  { key: 'deploy', cats: ['deploy', 'crash', 'state'] },
  { key: 'system', cats: ['system'] },
  { key: 'ports', cats: ['ports'] },
  { key: 'access', cats: ['access', 'config'] },
  { key: 'alert', cats: ['alert'] },
  { key: 'panel', cats: ['panel'] },
]
const ICON: Record<string, string> = {
  deploy: '📦',
  crash: '💥',
  state: '⏹',
  system: '⚙',
  ports: '🔌',
  access: '🔑',
  config: '🗓',
  alert: '🔥',
  panel: '👤',
}
// те же значки - в блоке "За сутки" на сводке карточки
export const TL_ICON = ICON

type Props = { serverId: number; onUnauthorized: () => void }

const groupOf = (cat: string) => GROUPS.find((g) => g.cats.includes(cat))?.key ?? cat

export function TimelineCard({ serverId, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [days, setDays] = useState(7)
  const [data, setData] = useState<TimelineResult | null>(null)
  const [loaded, setLoaded] = useState<string | null>(null)
  const [err, setErr] = useState('')
  const [off, setOff] = useState<Set<string>>(() => new Set())
  const [limit, setLimit] = useState(100)
  const [opened, setOpened] = useState<Set<string>>(() => new Set())
  const key = `${serverId}|${days}`
  const busy = loaded !== key

  useEffect(() => {
    let alive = true
    getServerTimeline(serverId, days)
      .then((r) => {
        if (!alive) return
        setData(r)
        setErr('')
        setLoaded(`${serverId}|${days}`)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        if (!alive) return
        setErr(String((e as Error).message || e))
        setLoaded(`${serverId}|${days}`)
      })
    return () => {
      alive = false
    }
  }, [serverId, days, onUnauthorized])

  const groupLabel = (k: string) =>
    ({
      deploy: t('Контейнеры'),
      system: t('Система'),
      ports: t('Порты'),
      access: t('Доступ и кроны'),
      alert: t('Алерты'),
      panel: t('Панель'),
    })[k] ?? k


  const counts = data?.cats ?? {}
  const events = useMemo(() => (data?.events ?? []).filter((e) => !off.has(groupOf(e.cat))), [data, off])

  const toggle = (g: string) =>
    setOff((prev) => {
      const n = new Set(prev)
      if (n.has(g)) n.delete(g)
      else n.add(g)
      return n
    })

  const fmtDay = (ts: string) => new Date(ts).toLocaleDateString([], { day: 'numeric', month: 'long', weekday: 'short' })
  const fmtTime = (ts: string) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

  const text = (e: TimelineEvent) => {
    if (e.cat === 'panel') {
      if (e.kind === 'maint') return `${t('окно обслуживания')}: ${e.text}${e.end ? ` (${t('до')} ${fmtTime(e.end)})` : ''}`
      if (e.kind === 'note') return `${t('заметка')} (${e.user}): ${e.text}`
      return `${e.user}: ${actLabel(e.kind, t, e.text)}${e.text ? ` · ${e.text}` : ''}`
    }
    return e.text
  }

  const shown = events.slice(0, limit)
  const openKey = (e: TimelineEvent) => `${e.kind}|${e.ts}`
  const flip = (k: string) =>
    setOpened((prev) => {
      const n = new Set(prev)
      if (n.has(k)) n.delete(k)
      else n.add(k)
      return n
    })
  // строка прогона пакетов: обновлен - старая и новая версия, поставлен и удален - с плюсом и минусом
  const pkgLine = (x: { a: string; p: string; o: string; n: string }) =>
    x.a === 'upgrade' ? `${x.p} ${x.o} -> ${x.n}` : x.a === 'install' ? `+ ${x.p} ${x.n}` : `- ${x.p} ${x.o}`
  return (
    <div className="chart-card net-card net-card-wide tl-card" id="mcard-timeline">
      <div className="net-head">
        <div className="win-switch">
          {PERIODS.map((d) => (
            <button key={d} className={`win-btn${d === days ? ' win-btn-active' : ''}`} onClick={() => setDays(d)}>
              {d === 1 ? t('сутки') : t('{n} дней', { n: d })}
            </button>
          ))}
        </div>
        {busy && <span className="muted small">{t('загрузка…')}</span>}
      </div>
      {/* плитки - счетчики групп и они же фильтр: нажатая группа скрывается из ленты */}
      <div className="net-tiles tl-tiles">
        {GROUPS.map((g) => {
          const n = g.cats.reduce((a, c) => a + (counts[c] ?? 0), 0)
          if (!n) return null
          const hidden = off.has(g.key)
          return (
            <button
              key={g.key}
              className={`net-tile tl-tile${hidden ? ' tl-tile-off' : ''}`}
              onClick={() => toggle(g.key)}
              title={hidden ? t('показать') : t('скрыть')}
              aria-pressed={!hidden}
            >
              <div className="net-tile-label">
                {ICON[g.key]} {groupLabel(g.key)}
              </div>
              <div className="net-tile-value">{n}</div>
            </button>
          )
        })}
      </div>
      {err && <p className="form-error">{err}</p>}
      {data && events.length === 0 && (
        <div className="muted small">{t('За этот период ничего не происходило.')}</div>
      )}
      {shown.length > 0 && (
        <div className="tl-list">
          {shown.map((e, i) => {
            const day = fmtDay(e.ts)
            const head = i === 0 || fmtDay(shown[i - 1].ts) !== day
            const more = e.kind === 'pkgs' && (e.data?.length ?? 0) > 0
            const isOpen = more && opened.has(openKey(e))
            return (
              <div key={i}>
                {head && <div className="tl-day">{day}</div>}
                <div className={`tl-row tl-${e.cat}${e.cat === 'alert' && !e.recovery ? ' tl-fire' : ''}${e.alert ? ' tl-warn' : ''}`}>
                  <span className="tl-time">{fmtTime(e.ts)}</span>
                  <span className="tl-icon" aria-hidden="true">
                    {e.cat === 'alert' && e.recovery ? '✅' : (ICON[e.cat] ?? '•')}
                  </span>
                  <span className="tl-text">
                    {text(e)}
                    {more && (
                      <button className="ghost small tl-more" onClick={() => flip(openKey(e))}>
                        {isOpen ? t('свернуть') : t('подробно')}
                      </button>
                    )}
                  </span>
                </div>
                {isOpen && (
                  <div className="tl-pkgs small">
                    {e.data!.map((x, j) => (
                      <div key={j} className={`tl-pkg tl-pkg-${x.a}`}>
                        {pkgLine(x)}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
      {events.length > limit && (
        <button className="ghost small row-act exp-more" onClick={() => setLimit(limit + 200)}>
          {t('показать еще ({n})', { n: events.length - limit })}
        </button>
      )}
      <div className="net-foot">
        <div>{t('Плитки сверху - фильтр: нажмите, чтобы скрыть группу из ленты.')}</div>
        <div>
          {t(
            'Контейнеры, система и порты записываются с 1.4.168, пакеты по журналу установок - с агента 2.28. Доступ и кроны - из снимков раз в 10 минут.',
          )}
        </div>
      </div>
    </div>
  )
}
