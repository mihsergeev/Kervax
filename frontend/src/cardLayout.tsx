import { Fragment, useEffect, useRef } from 'react'
import { useI18n } from './i18n'
import { sinceShort } from './serverUtils'

// Общая раскладка карточек (сервер, кластер, дальше Docker-нода, сервисы, бэкапы и монитор): меню
// разделов слева по группам, на экране один раздел. На сводке строки проблем, плитки и карта по
// областям. Стили - srv-* и ov-* в App.css

// group - подпись группы в меню (у сводки ее нет)
export type SecDef = { id: string; label: string; group: string }

// Состояние раздела для меню и сводки: level - самое серьезное замечание (0 - все в порядке),
// n - число замечаний у пункта меню, line - строка на карте: первое замечание или коротко, что
// там; idle - данных нет, точка серая; fresh - с какого момента видна самая новая проблема
// раздела, если она появилась меньше суток назад
export type SecInfo = { level: 0 | 1 | 2; n: number; line: string; idle?: boolean; fresh?: string }

// "новое" - проблема появилась меньше суток назад: так одинаково у всех, без "с прошлого захода"
const FRESH_MS = 24 * 3600_000

// count - сколько объектов за одним замечанием ("падают поды: a, b и еще 3"), для числа в меню
export type SecItem = { level: 1 | 2; text: string; since?: string | null; count?: number }

// Замечания раздела в его состояние: уровень самого серьезного, их число, первое в строку карты
// (или ok, если замечаний нет) и самое свежее, если оно моложе суток
export function secInfoOf(
  xs: SecItem[],
  ok: string,
  t: (k: string, p?: Record<string, string | number>) => string,
): SecInfo {
  const fresh = xs
    .map((x) => x.since ?? '')
    .filter((x) => x && Date.now() - Date.parse(x) < FRESH_MS)
    .sort()
    .pop()
  return {
    level: xs.reduce<0 | 1 | 2>((a, x) => (x.level > a ? x.level : a), 0),
    n: xs.reduce((a, x) => a + (x.count ?? 1), 0),
    line: xs.length ? xs[0].text + (xs.length > 1 ? ` ${t('и еще {n}', { n: xs.length - 1 })}` : '') : ok,
    fresh,
  }
}

// Заглушенные проблемы разделов: без них раздел зеленый "все в порядке", хотя рядом в нем "истёк" или
// "с проблемой 1". Серым и с пометкой видно, что проблема есть, а алерт по ней заглушен. Другие
// замечания и желтые строки раздела важнее, их не трогаем
export function markMuted(
  info: Record<string, SecInfo>,
  muted: { sec: string; text: string; dev?: boolean }[],
  t: (k: string, p?: Record<string, string | number>) => string,
): void {
  const seen = new Set<string>()
  for (const { sec, text, dev } of muted) {
    const cur = info[sec]
    if (!cur || cur.level || seen.has(sec)) continue
    seen.add(sec)
    // dev - сервер разработки, поведение приложения: не будит, но видно (backend app/dev_mode.py)
    info[sec] = { ...cur, idle: true, line: dev ? t('разработка: {text}', { text }) : t('заглушено: {text}', { text }) }
  }
}

// "новое · 2 ч": проблема раздела появилась меньше суток назад
export function FreshChip({ since }: { since: string }) {
  const { t } = useI18n()
  const ago = sinceShort(since, t)
  return <span className="srv-new" title={new Date(since).toLocaleString()}>{ago ? `${t('новое')} · ${ago}` : t('новое')}</span>
}

// Меню разделов: группы подписями, у пункта с замечаниями число цветом самого серьезного.
// На телефоне - лентой над разделом, текущий пункт подкручивается в видимую часть
// side - короткое значение справа у пункта (размер, срок), когда числа замечаний там нет
export function CardNav({ secs, active, info, side, onGo }: {
  secs: SecDef[]
  active: string
  info: Record<string, SecInfo>
  side?: Record<string, string>
  onGo: (id: string) => void
}) {
  const { t } = useI18n()
  const ref = useRef<HTMLElement>(null)
  useEffect(() => {
    const nav = ref.current
    const el = nav?.querySelector<HTMLElement>('.srv-nav-item.active')
    if (!nav || !el || nav.scrollWidth <= nav.clientWidth) return
    nav.scrollLeft = el.offsetLeft - nav.clientWidth / 2 + el.offsetWidth / 2
  }, [active])
  return (
    <nav className="srv-nav" ref={ref} aria-label={t('Разделы')}>
      {secs.map((s, i) => {
        const x = info[s.id]
        return (
          <Fragment key={s.id}>
            {s.group && s.group !== secs[i - 1]?.group && <div className="srv-nav-grp">{t(s.group)}</div>}
            <button className={`srv-nav-item${active === s.id ? ' active' : ''}`} onClick={() => onGo(s.id)}>
              <span className="srv-nav-label">{t(s.label)}</span>
              {x?.fresh && <span className="srv-new">{t('новое')}</span>}
              {x && x.n > 0 && x.level > 0 ? (
                <span className={`srv-nav-n srv-nav-n-${x.level >= 2 ? 'bad' : 'warn'}`}>{x.n}</span>
              ) : side?.[s.id] ? (
                <span className="srv-nav-side">{side[s.id]}</span>
              ) : null}
            </button>
          </Fragment>
        )
      })}
    </nav>
  )
}

// Строки проблем на сводке: точка, текст, сколько длится и кнопка туда, где это разбирается
export type OvRow = {
  tone: 't-down' | 't-degraded'
  text: string
  since?: string | null
  act?: { label: string; onClick: () => void }
  dev?: boolean // сервер разработки, вид приложения: серым с пометкой (backend app/dev_mode.py)
  fix?: () => void // починили: закрыть алерт сейчас (FixedDialog.tsx)
}
export function OvRows({ rows }: { rows: OvRow[] }) {
  const { t } = useI18n()
  if (!rows.length) return null
  return (
    <div className="ov-issues">
      {rows.map((i, n) => (
        <div key={n} className="ov-issue">
          <span className={`lk-dot ${i.dev ? 'lk-dot-idle' : i.tone === 't-down' ? 'lk-dot-bad' : 'lk-dot-warn'}`} />
          <span className={`ov-issue-text${i.dev ? ' muted' : ''}`}>{i.dev ? t('разработка: {text}', { text: i.text }) : i.text}</span>
          {sinceShort(i.since, t) && <span className="muted small">{sinceShort(i.since, t)}</span>}
          {i.fix && (
            <button className="ghost small row-act" title={t('Починили: убрать алерт сейчас и считать заново')}
              onClick={i.fix}>
              ✅ {t('Починили')}
            </button>
          )}
          {i.act && (
            <button className="ghost small row-act" onClick={i.act.onClick}>
              {i.act.label} ›
            </button>
          )}
        </div>
      ))}
    </div>
  )
}

// Карта по областям: у каждой области точка самого серьезного и строка, клик открывает раздел
export function OvAreas({ cap, items, onGo }: {
  cap: string
  items: { id: string; label: string; info: SecInfo; go?: string }[]
  onGo: (id: string) => void
}) {
  const { t } = useI18n()
  if (!items.length) return null
  return (
    <>
      <div className="ov-cap">{cap}</div>
      <div className="ov-areas">
        {items.map(({ id, label, info: x, go }) => {
          const lv = x.level >= 2 ? 'bad' : x.level ? 'warn' : ''
          return (
            <button key={id} className={`ov-area${lv ? ` ov-area-${lv}` : ''}`} onClick={() => onGo(go ?? id)}>
              <span className="ov-area-h">
                <span>{t(label)}</span>
                {x.fresh && <FreshChip since={x.fresh} />}
                <span className={`lk-dot${lv ? ` lk-dot-${lv}` : x.line && !x.idle ? ' lk-dot-ok' : ''}`} />
              </span>
              <span className="ov-area-s">{x.line || '—'}</span>
            </button>
          )
        })}
      </div>
    </>
  )
}
