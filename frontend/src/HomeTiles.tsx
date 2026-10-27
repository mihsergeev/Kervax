import type { ReactNode } from 'react'
import type { Section } from './App'
import { useI18n } from './i18n'
import type { SecIssue } from './sectionIssues'

// Плитка сводки на главной. Пока все хорошо - общее число раздела ("12 из 12") и зеленое "все в
// норме". Когда есть беда, крупно она ("2 проблемы", предупреждения чипом), ниже серой строкой общее
// число, а под чертой первые строки "сервер - что" (клик ведет туда, где чинится, как в "Что сломано").
// Шапка плитки целиком - в раздел. В шапках разделов та же плитка - фильтр списка под ней (active -
// нажата), а строки ведут в карточку
export type TileIssue = { key: string; name: string; text: string; down: boolean; onOpen: () => void }
type Tone = 'ok' | 'warn' | 'muted'

// строк под чертой: три влезают целиком, больше - две и "еще N: имена"
const TILE_ROWS = 3

// строки беды (sectionIssues.ts) -> строки плитки: клик ведет туда же, куда строка "Что сломано"
// eslint-disable-next-line react-refresh/only-export-components
export function tileIssues(xs: SecIssue[], open?: (sec: Section, id?: number, srv?: boolean, detailSec?: string) => void): TileIssue[] {
  return xs.map((x) => ({ key: x.key, name: x.name, text: x.text, down: x.down, onOpen: () => open?.(x.section, x.id, x.srv, x.sec) }))
}

// склонение по числу: слова идут через t(), в английском одно или много
// eslint-disable-next-line react-refresh/only-export-components
export function usePlural() {
  const { lang } = useI18n()
  return (n: number, one: string, few: string, many: string) => {
    if (lang === 'en') return n === 1 ? one : many
    const a = n % 10, b = n % 100
    return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 10 || b >= 20) ? few : many
  }
}

export function HomeTile({
  icon, label, value, unit, ok, okTone = 'ok', foot, total, issues = [], onOpen, active, title, wide,
}: {
  icon?: ReactNode
  label: string
  value: string
  unit?: string
  ok?: string // строка под числом, пока проблем нет
  okTone?: Tone
  foot?: string // серая строка: контекст раздела ("хостов: 5")
  total?: string // общее число, когда крупно проблема ("онлайн 12 из 12")
  issues?: TileIssue[]
  onOpen: () => void
  active?: boolean // фильтр списка по плитке стоит
  title?: string
  wide?: boolean // в шапке раздела: с бедой - в две колонки и строк больше, плиток там мало
}) {
  const { t } = useI18n()
  const pl = usePlural()
  const bad = issues.filter((x) => x.down)
  const warn = issues.filter((x) => !x.down)
  const tone = bad.length ? 'bad' : warn.length ? 'warn' : ''
  const list = [...bad, ...warn]
  const rows = wide ? TILE_ROWS + 1 : TILE_ROWS
  const shown = list.length <= rows ? list : list.slice(0, rows - 1)
  const rest = [...new Set(list.slice(shown.length).map((x) => x.name || x.text))]
  const n = bad.length || warn.length
  return (
    <div className={`htile${tone ? ` htile-${tone}` : ''}${active ? ' htile-active' : ''}${wide ? ' htile-wide' : ''}`}>
      <button type="button" className="htile-head" onClick={onOpen} title={title ?? t('Открыть')} aria-pressed={active}>
        <span className="htile-label">{icon}{label}{active === undefined && <span className="htile-go">→</span>}</span>
        {tone ? (
          <span className={`htile-big htile-t-${tone}`}>
            <span className="htile-num">{n}</span>
            <span className="htile-word">
              {bad.length
                ? pl(n, t('проблема'), t('проблемы'), t('проблем'))
                : pl(n, t('предупреждение'), t('предупреждения'), t('предупреждений'))}
            </span>
            {bad.length > 0 && warn.length > 0 && (
              <span className="htile-chip">{t('+{n} предупр.', { n: warn.length })}</span>
            )}
          </span>
        ) : (
          <span className="htile-big">
            <span className="htile-num">{value}</span>
            {unit && <span className="htile-unit">{unit}</span>}
          </span>
        )}
        {!tone && ok && <span className={`htile-ok htile-t-${okTone}`}>{ok}</span>}
        {(tone ? total ?? foot : foot) && <span className="htile-foot">{tone ? total ?? foot : foot}</span>}
      </button>
      {tone && (
        <div className="htile-rows">
          {shown.map((x) => (
            <button key={x.key} type="button" className="htile-row" onClick={x.onOpen}
              title={x.name ? `${x.name} - ${x.text}` : x.text}>
              <span className={`lk-dot ${x.down ? 'lk-dot-bad' : 'lk-dot-warn'}`} />
              <span className="htile-row-txt">
                {x.name && <b>{x.name}</b>} <span className="muted">{x.text}</span>
              </span>
            </button>
          ))}
          {rest.length > 0 && (
            <button type="button" className="htile-row htile-more" onClick={onOpen} title={rest.join(', ')}>
              <span className="htile-row-txt">{t('еще {n}: {list}', { n: list.length - shown.length, list: rest.join(', ') })}</span>
            </button>
          )}
        </div>
      )}
    </div>
  )
}
