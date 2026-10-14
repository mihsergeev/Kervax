import type { Check, CheckSample } from './api'
import type { Series } from './charts/StackedAreaChart'
import { useI18n } from './i18n'
import { sinceShort } from './serverUtils'

// Где ломается сайт (бэкенд checks._Phases, app/inside_probe.py): из чего сложилось время
// ответа последней проверки, на какой фазе оборвался запрос и что видно изнутри сервера сайта.

type TFn = (k: string, p?: Record<string, string | number>) => string
type PhaseKey = 'dns' | 'connect' | 'tls' | 'wait' | 'body'

const PHASES: { k: PhaseKey; label: string; color: string }[] = [
  { k: 'dns', label: 'DNS', color: '#8b5cf6' },
  { k: 'connect', label: 'подключение', color: '#06b6d4' },
  { k: 'tls', label: 'TLS', color: '#f59e0b' },
  { k: 'wait', label: 'ожидание ответа', color: '#4b74ff' },
  { k: 'body', label: 'загрузка', color: '#94a3b8' },
]

function fmtMs(ms: number, t: TFn): string {
  return ms < 1000 ? `${Math.round(ms)} ${t('мс')}` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} ${t('с')}`
}

// Серии графика "время ответа" по фазам. null - у точек фаз нет (старые данные, монитор
// изнутри сервера, локация), тогда график прежний, одной линией.
export function phaseSeries(points: CheckSample[] | null, t: TFn): Series[] | null {
  const pts = points ?? []
  const has = (p: CheckSample) => p.ph_connect != null || p.ph_wait != null || p.ph_dns != null || p.ph_tls != null
  if (!pts.some(has)) return null
  const v = (p: CheckSample, x: number | null | undefined) => (has(p) ? x ?? 0 : null)
  const out: Series[] = [
    { name: 'DNS', color: PHASES[0].color, values: pts.map((p) => v(p, p.ph_dns)) },
    { name: t('подключение'), color: PHASES[1].color, values: pts.map((p) => v(p, p.ph_connect)) },
    { name: 'TLS', color: PHASES[2].color, values: pts.map((p) => v(p, p.ph_tls)) },
    { name: t('ожидание ответа'), color: PHASES[3].color, values: pts.map((p) => v(p, p.ph_wait)) },
    {
      name: t('загрузка'),
      color: PHASES[4].color,
      values: pts.map((p) =>
        has(p) && p.latency_ms != null
          ? Math.max(0, p.latency_ms - (p.ph_connect ?? 0) - (p.ph_tls ?? 0) - (p.ph_wait ?? 0))
          : null,
      ),
    },
    // точки до разбивки (или проверки без нее) - общим временем, чтобы график не проваливался
    { name: t('без разбивки'), color: '#64748b', values: pts.map((p) => (has(p) ? null : p.latency_ms)) },
  ]
  return out.filter((s) => s.values.some((x) => x != null && x > 0))
}

export function CheckWhere({ check }: { check: Check }) {
  const { t } = useI18n()
  const ph = check.last_phases
  // сайт давно поднялся - про прошлый сбой изнутри уже не рассказываем
  const liAt = Date.parse(check.last_inside?.ts || check.last_inside?.asked || '')
  const li = check.last_status === 'up' && !(Date.now() - liAt < 86400_000) ? null : check.last_inside
  if (check.type !== 'http' || check.probe_local || (!ph && !li)) return null
  const parts = PHASES.filter((p) => (ph?.[p.k] ?? 0) > 0 || ph?.fail === p.k)
  const total = parts.reduce((a, p) => a + (ph?.[p.k] ?? 0), 0) || 1
  const failed = !!ph?.fail
  const srv = li?.server || t('сервер')
  const asked = li?.asked ? (Date.now() - Date.parse(li.asked)) / 1000 : Infinity
  return (
    <div className="detail-inc" id="check-where">
      <div className="chart-cap">{failed ? t('Где оборвался запрос') : t('Из чего сложилось время ответа')}</div>
      {ph && parts.length > 0 && (
        <>
          <div className="ph-bar">
            {parts.map((p) => (
              <span
                key={p.k}
                className={`ph-seg${ph.fail === p.k ? ' ph-fail' : ''}`}
                style={{ flexGrow: Math.max(ph[p.k] ?? 0, total * 0.03), background: p.color }}
                title={`${t(p.label)}: ${fmtMs(ph[p.k] ?? 0, t)}`}
              />
            ))}
          </div>
          <div className="ph-legend small">
            {parts.map((p) => (
              <span key={p.k} className={ph.fail === p.k ? 't-down' : undefined}>
                <i style={{ background: p.color }} />
                {t(p.label)} <b>{fmtMs(ph[p.k] ?? 0, t)}</b>
                {ph.fail === p.k && ` - ${t('оборвалось здесь')}`}
              </span>
            ))}
          </div>
        </>
      )}
      {li?.ts && li.status ? (
        <div className="small ph-inside">
          {li.status === 'up' || li.status === 'degraded' ? (
            <>
              <span className="t-up">{t('Изнутри {s} при сбое открывался', { s: srv })}</span> ({li.message})
              <span className="muted"> - {t('приложение живо, снаружи мешает сеть, блокировка или белый список')}</span>
            </>
          ) : (
            <>
              <span className="t-down">{t('Изнутри {s} при сбое тоже не открывался', { s: srv })}</span>: {li.message}
              <span className="muted"> - {t('дело в самом сервере или приложении')}</span>
            </>
          )}
          {sinceShort(li.ts, t) && <span className="muted"> · {t('{t} назад', { t: sinceShort(li.ts, t) })}</span>}
        </div>
      ) : li?.asked ? (
        <div className="small muted ph-inside">
          {asked < 180
            ? t('Спросили {s}, открывается ли сайт изнутри: ждем ответ агента', { s: srv })
            : t('{s} не ответил на проверку изнутри', { s: srv })}
        </div>
      ) : null}
    </div>
  )
}
