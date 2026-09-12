import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'

/** Загрузка по ядрам картой: строка на ядро, столбец на замер, цвет на процент.
 *
 * Линиями это не читается. На шестнадцатиядерной ноде в одной рамке лежало
 * шестнадцать наложенных кривых — клубок, из которого не вытащить ни одного факта:
 * ни какое ядро горячее, ни сколько их занято. А вопрос к этому графику ровно один
 * и всегда тот же: нагрузка размазана по ядрам или упёрлась в одно.
 *
 * На карте это видно как рисунок: ровная сетка — нагрузка распределена, светлая
 * полоса поперёк — занято одно ядро (однопоточный упор, самый частый случай),
 * светлый столбец — всплеск на всех сразу.
 *
 * Canvas, а не SVG: 16 ядер × несколько сотен замеров — это тысячи прямоугольников,
 * и в DOM они кладут прокрутку страницы.
 */
export function CoresHeatmap({
  ts,
  cores,
  height = 220,
  fmtTime,
}: {
  ts: number[]
  cores: (number | null)[][] // [замер][ядро]
  height?: number
  fmtTime: (ms: number) => string
}) {
  const { t } = useI18n()
  const ref = useRef<HTMLCanvasElement>(null)
  const [hi, setHi] = useState<{ core: number; i: number } | null>(null)
  const n = ts.length
  const nc = cores.reduce((mx, row) => Math.max(mx, row?.length ?? 0), 0)

  useEffect(() => {
    const cv = ref.current
    if (!cv || n === 0 || nc === 0) return
    const css = (name: string) =>
      getComputedStyle(document.documentElement).getPropertyValue(name).trim()
    const hex = (v: string): [number, number, number] => {
      const h = v.replace('#', '')
      const f = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
      return [0, 2, 4].map((i) => parseInt(f.slice(i, i + 2), 16)) as [number, number, number]
    }
    const draw = () => {
      const dpr = window.devicePixelRatio || 1
      const w = cv.clientWidth
      cv.width = Math.max(1, Math.round(w * dpr))
      cv.height = Math.round(height * dpr)
      const g = cv.getContext('2d')
      if (!g) return
      g.setTransform(dpr, 0, 0, dpr, 0, 0)
      g.clearRect(0, 0, w, height)

      const padL = 34
      const padB = 18
      const cw = (w - padL) / n
      const rh = (height - padB) / nc
      // Холодный цвет — обычная работа, тёплый — ядро под упором. Шкала обрезана на
      // 80%: выше этого разница «88 или 96» ничего не меняет, а внизу важна каждая
      // ступенька, иначе спокойная нода выглядит равномерно пустой.
      const cool = hex(css('--accent') || '#4b74ff')
      const warm: [number, number, number] = [230, 165, 69]
      for (let c = 0; c < nc; c++) {
        for (let i = 0; i < n; i++) {
          const raw = cores[i]?.[c]
          if (raw == null) continue // нет данных — оставляем полотно пустым
          const v = Math.min(raw, 80) / 80
          const base = v < 0.5 ? cool : warm
          const a = v < 0.5 ? 0.08 + v * 1.3 : Math.min(0.92, 0.35 + v * 0.6)
          g.fillStyle = `rgba(${base[0]},${base[1]},${base[2]},${a.toFixed(3)})`
          g.fillRect(padL + i * cw, c * rh, Math.max(1, Math.ceil(cw)), Math.max(1, rh - 1))
        }
      }
      g.font = `10px ${css('--font-mono') || 'ui-monospace, monospace'}`
      g.fillStyle = css('--text-muted') || '#8b93a7'
      g.textAlign = 'right'
      for (let c = 0; c < nc; c += nc > 8 ? 2 : 1) {
        g.fillText(`#${c}`, padL - 6, c * rh + Math.min(rh, 11))
      }
      g.textAlign = 'center'
      const step = Math.max(1, Math.ceil(n / 6))
      for (let i = 0; i < n; i += step) {
        g.fillText(fmtTime(ts[i]), padL + i * cw, height - 5)
      }
    }
    draw()
    const ro = new ResizeObserver(draw)
    ro.observe(cv)
    // тема переключается на лету — цвета берём из переменных, поэтому перерисовываем
    const mo = new MutationObserver(draw)
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => {
      ro.disconnect()
      mo.disconnect()
    }
  }, [ts, cores, n, nc, height, fmtTime])

  if (n === 0 || nc === 0) return <div className="chart-empty">—</div>

  const onMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const cv = ref.current
    if (!cv) return
    const r = cv.getBoundingClientRect()
    const padL = 34
    const cw = (r.width - padL) / n
    const rh = (height - 18) / nc
    const i = Math.max(0, Math.min(n - 1, Math.floor((e.clientX - r.left - padL) / cw)))
    const core = Math.max(0, Math.min(nc - 1, Math.floor((e.clientY - r.top) / rh)))
    setHi({ core, i })
  }

  const val = hi ? cores[hi.i]?.[hi.core] : null
  return (
    <div className="cores-map">
      <canvas
        ref={ref}
        style={{ height }}
        onMouseMove={onMove}
        onMouseLeave={() => setHi(null)}
      />
      <div className="cores-map-read muted small mono">
        {hi && val != null
          ? `${t('ядро')} #${hi.core} · ${val.toFixed(1)}% · ${fmtTime(ts[hi.i])}`
          : t('ядро под курсором покажет свою загрузку')}
      </div>
    </div>
  )
}
