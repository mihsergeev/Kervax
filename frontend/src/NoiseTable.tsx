import { useState, type ReactNode } from 'react'
import type { ReportNoise } from './api'
import { useI18n } from './i18n'
import { noiseGroups, noiseWhen, times } from './reportNoise'

// Что шумело таблицей (окно отчета и история алертов): объект строкой-заголовком, под ним что
// именно, почему и когда. Без полосок и счетчиков в лоб: важно, что и где, а число раз - мелко
// рядом с датой. flat - строки одного объекта без заголовка (раздел объекта), onPick - строка
// ссылкой (к сообщениям про это)
export function NoiseTable({
  items,
  name,
  perTarget = 6,
  flat,
  onPick,
}: {
  items: ReportNoise[]
  name: (x: ReportNoise) => ReactNode
  perTarget?: number
  flat?: boolean
  onPick?: (x: ReportNoise) => void
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState<Set<string>>(new Set())
  return (
    <table className={`sec-tbl rp-noise${flat ? ' rp-noise-flat' : ''}`}>
      <tbody>
        {noiseGroups(items).map((g) => {
          const k = `${g[0].section}|${g[0].target}`
          const all = flat || open.has(k)
          const shown = all ? g : g.slice(0, perTarget)
          return [
            ...(flat
              ? []
              : [
                  <tr key={k} className="rp-noise-grp">
                    <td colSpan={2}>{name(g[0])}</td>
                  </tr>,
                ]),
            ...shown.map((x) => (
              <tr key={`${k}|${x.kind}|${x.what}`}>
                <td title={x.label}>
                  <div>
                    {onPick ? (
                      <button className="linklike rp-noise-pick" onClick={() => onPick(x)}>
                        {x.what}
                      </button>
                    ) : (
                      x.what
                    )}
                  </div>
                  {x.note && <div className="muted small">{x.note}</div>}
                </td>
                <td className="num small">
                  <div>{noiseWhen(x)}</div>
                  {x.n > 1 && <div className="muted">{times(x.n, t)}</div>}
                </td>
              </tr>
            )),
            ...(g.length > shown.length
              ? [
                  <tr key={`${k}|more`}>
                    <td colSpan={2}>
                      <button className="linklike small" onClick={() => setOpen(new Set([...open, k]))}>
                        {t('и еще {n}', { n: g.length - shown.length })}
                      </button>
                    </td>
                  </tr>,
                ]
              : []),
          ]
        })}
      </tbody>
    </table>
  )
}
