import { useCallback, useEffect, useState } from 'react'
import { getSoftware, type Server, type SoftwareItem, type SoftwareOut } from './api'
import { useI18n } from './i18n'

// Версии ПО (app/software.py): что стоит в контейнерах и на хосте и когда у этого кончается
// поддержка по снимку endoflife.date: все найденное по продукту и циклу (без поддержки сверху),
// образы без версии в теге и образы, которые больше года не пересобирались. Строки sw_advice
// здесь не повторяются: они на сводке и на главной, а здесь то же самое видно по списку.

type TFn = (k: string, p?: Record<string, string | number>) => string
const WHERE_MAX = 6

function ddmmyyyy(iso: string): string {
  const [y, m, d] = iso.split('-')
  return `${d}.${m}.${y}`
}

function term(g: SoftwareItem, t: TFn): string {
  if (g.state === 'eol') return typeof g.eol === 'string' ? t('кончилась {d}', { d: ddmmyyyy(g.eol) }) : t('кончилась')
  if (typeof g.eol !== 'string') return t('срок не объявлен')
  return g.state === 'soon'
    ? t('до {d}, через {n} дн.', { d: ddmmyyyy(g.eol), n: g.left ?? 0 })
    : t('до {d}', { d: ddmmyyyy(g.eol) })
}

function whereText(g: SoftwareItem, t: TFn): string {
  const cont = [...new Set(g.where.filter((w) => w.kind === 'container').map((w) => w.name))].sort()
  const host = g.where.filter((w) => w.kind === 'host').map((w) => w.name)
  const parts: string[] = []
  if (cont.length)
    parts.push(
      cont.slice(0, WHERE_MAX).join(', ') + (cont.length > WHERE_MAX ? ` ${t('и еще {n}', { n: cont.length - WHERE_MAX })}` : ''),
    )
  if (host.length) parts.push(t('хост: {list}', { list: host.join(', ') }))
  return parts.join(' · ')
}

function age(days: number, t: TFn): string {
  const y = Math.floor(days / 365)
  return y >= 2 ? t('{n} г. назад', { n: y }) : t('{n} дн. назад', { n: days })
}

export function SoftwareCard({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const [data, setData] = useState<SoftwareOut | null>(null)
  const [oldOpen, setOldOpen] = useState(false)
  const load = useCallback(() => {
    getSoftware(s.id).then(setData).catch(() => {})
  }, [s.id])
  useEffect(() => {
    load()
    const h = window.setInterval(load, 10 * 60_000)
    return () => window.clearInterval(h)
  }, [load])
  if (!data || (!data.items.length && !data.unknown.length && !data.old.length)) return null
  return (
    <div className="net-cards">
      <div className="chart-card net-card net-card-wide" id="mcard-software">
        <div className="net-head">
          <div className="chart-cap">{t('Версии ПО')}</div>
          {data.built && (
            <span className="muted small">{t('сроки поддержки по endoflife.date на {d}', { d: ddmmyyyy(data.built) })}</span>
          )}
        </div>
        {data.items.length > 0 && (
          <div className="sw-list">
            {data.items.map((g) => (
              <div key={g.key} className="sw-row">
                <span className={`lk-dot ${g.state === 'eol' ? 'lk-dot-bad' : g.state === 'soon' ? 'lk-dot-warn' : 'lk-dot-ok'}`} />
                <span className="sw-name">
                  {g.name} <span className="mono">{g.cycle}</span>
                </span>
                <span className="sw-where muted small" title={g.where.map((w) => `${w.name} ${w.ver}`).join('\n')}>
                  {whereText(g, t)}
                </span>
                <span className={`sw-term small ${g.state === 'eol' ? 't-down' : g.state === 'soon' ? 't-degraded' : 'muted'}`}>
                  {term(g, t)}
                </span>
              </div>
            ))}
          </div>
        )}
        {data.unknown.length > 0 && (
          <div className="net-foot">
            <div>
              {t('Версия не видна, в теге образа ее нет: {list}.', {
                list: data.unknown.map((u) => `${u.name} (${u.pname})`).join(', '),
              })}{' '}
              {data.agent_ok
                ? t('В окружении образа версии тоже нет.')
                : t('Агент 2.35 и новее берет версию из окружения образа: обновите агент.')}
            </div>
          </div>
        )}
        {data.old.length > 0 && (
          <div className="sw-old">
            <button className="ghost small" onClick={() => setOldOpen(!oldOpen)}>
              {oldOpen ? '▾' : '▸'} {t('Образы старше года: {n}', { n: data.old.length })}
            </button>
            {oldOpen && (
              <div className="sw-list">
                {data.old.map((o) => (
                  <div key={o.name} className="sw-row">
                    <span className="lk-dot lk-dot-warn" />
                    <span className="sw-name mono">{o.name}</span>
                    <span className="sw-where muted small mono">{o.image}</span>
                    <span className="sw-term small muted">
                      {t('собран {d}', { d: new Date(o.built * 1000).toLocaleDateString() })}, {age(o.age_days, t)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        <div className="net-foot">
          <div>
            {t(
              'Версии берутся из инвентаря баз, окружения и тега образа, на хосте - из пакетов сторонних репозиториев (pgdg, sury, nodesource, nginx.org, MariaDB, MySQL, Redis). Пакеты дистрибутива дистрибутив чинит, пока жива ОС, их срок - поддержка ОС.',
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
