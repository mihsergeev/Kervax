import { useRef, useState } from 'react'
import { timeTicks } from './timeAxis'

// Стек-график с заливкой (площади складываются снизу вверх) ИЛИ зеркальный (одна
// серия вверх, вторая вниз от нуля) ИЛИ overlay (каждая серия — своя площадь от нуля,
// накладываются, НЕ складываются — для независимых величин типа дисков-маунтов).
// Легенда + тултип при наведении. Свой стиль: тонкие линии, мягкие градиенты.

export type Series = { name: string; color: string; values: (number | null)[] }

type Props = {
  ts: number[] // общие метки времени
  series: Series[]
  mode?: 'stack' | 'mirror' | 'overlay'
  yMax?: number // фикс. верх (напр. 100 для %); авто если не задан
  fmtY?: (v: number) => string
  fmtV?: (v: number) => string // формат значения в тултипе
  fmtTime?: (ms: number) => string // подпись времени по оси/в тултипе
  height?: number
  onZoom?: (fromMs: number, toMs: number) => void // выделение мышью → зум по времени
  // клик по полотну (нажал-отпустил на месте) → раскрыть график. Отдельно от onZoom:
  // протяжка остаётся выделением диапазона, кликом её не подменяем
  onExpand?: () => void
}

const W = 640
const PAD_L = 52
const PAD_R = 12
const PAD_T = 8
const PAD_B = 20

export function StackedAreaChart({
  ts,
  series,
  mode = 'stack',
  yMax: yMaxFix,
  fmtY,
  fmtV,
  fmtTime: fmtTimeProp,
  height = 150,
  onZoom,
  onExpand,
}: Props) {
  const H = height
  const ref = useRef<SVGSVGElement>(null)
  const [hi, setHi] = useState<number | null>(null)
  const [drag, setDrag] = useState<{ a: number; b: number } | null>(null)
  const pressRef = useRef<{ x: number; y: number } | null>(null)
  const n = ts.length
  if (n === 0) return <div className="chart-empty">—</div>

  const tMin = ts[0]
  const tMax = ts[n - 1]
  const plotW = W - PAD_L - PAD_R
  const plotH = H - PAD_T - PAD_B
  const sx = (i: number) =>
    n === 1 ? PAD_L + plotW / 2 : PAD_L + (i / (n - 1)) * plotW

  // верхняя граница
  let top: number
  if (mode === 'mirror' || mode === 'overlay') {
    const mx = Math.max(
      1,
      ...series.flatMap((s) => s.values.map((v) => Math.abs(v ?? 0))),
    )
    top = yMaxFix ?? mx * (mode === 'overlay' ? 1.12 : 1.15)
  } else {
    const stackMax = Math.max(
      1,
      ...ts.map((_, i) => series.reduce((a, s) => a + (s.values[i] ?? 0), 0)),
    )
    top = yMaxFix ?? stackMax * 1.12
  }
  const zeroY = mode === 'mirror' ? PAD_T + plotH / 2 : PAD_T + plotH
  const scaleH = mode === 'mirror' ? plotH / 2 : plotH
  const sy = (v: number) => zeroY - (v / top) * scaleH

  // построение площадей
  const areas: { s: Series; d: string; sign: number }[] = []
  const cum = new Array(n).fill(0) // для стека
  series.forEach((s, si) => {
    const sign = mode === 'mirror' && si === 1 ? -1 : 1
    const upper: string[] = []
    const lower: string[] = []
    for (let i = 0; i < n; i++) {
      const base = mode === 'stack' ? cum[i] : 0
      const val = s.values[i] ?? 0
      const y0 = sy(base * sign)
      const y1 = sy((base + val) * sign)
      upper.push(`${i ? 'L' : 'M'}${sx(i).toFixed(1)} ${y1.toFixed(1)}`)
      lower.push(`L${sx(i).toFixed(1)} ${y0.toFixed(1)}`)
      if (mode === 'stack') cum[i] += s.values[i] ?? 0
    }
    lower.reverse()
    areas.push({ s, d: `${upper.join(' ')} ${lower.join(' ')} Z`, sign })
  })

  const gridN = mode === 'mirror' ? 2 : 3
  const grid =
    mode === 'mirror'
      ? [-top, 0, top].map((v) => ({ v, y: sy(v) }))
      : Array.from({ length: gridN + 1 }, (_, i) => {
          const v = (top * i) / gridN
          return { v, y: sy(v) }
        })
  // Метки времени по оси X: раньше их было ровно две — по краям, и «когда был
  // этот пик» приходилось выяснивать курсором. Ставим 5-6 круглых моментов.
  const xt = timeTicks(tMin, tMax).map((tk) => ({
    ...tk,
    // ts может быть неравномерным (пропуски метрик), поэтому позицию считаем
    // по ВРЕМЕНИ, а не по индексу точки
    x: PAD_L + ((tk.t - tMin) / Math.max(1, tMax - tMin)) * plotW,
  }))
  const fmt = fmtY ?? ((v: number) => Math.round(v).toString())
  const fmtVal = fmtV ?? fmt

  // Datadog-стиль: overlay (много независимых линий) — почти без заливки, чтобы не
  // мутнело при наложении; стек/зеркало (part-to-whole) — умеренная мягкая заливка.
  const fillTop = mode === 'overlay' ? 0.14 : 0.3
  const fillBot = mode === 'overlay' ? 0.02 : 0.05
  // id градиента уникален по (режим+цвет) — иначе один цвет в overlay и стеке
  // на одной странице делит defs и заливка «перетекает» между графиками
  const gid = (c: string) => `sac-${mode}-${c.replace('#', '')}`

  const clientToVx = (clientX: number) => {
    const el = ref.current
    if (!el) return PAD_L
    const rect = el.getBoundingClientRect()
    return ((clientX - rect.left) / rect.width) * W
  }
  const vxToTime = (vx: number) => {
    const frac = Math.max(0, Math.min(1, (vx - PAD_L) / plotW))
    const fi = frac * (n - 1)
    const i0 = Math.floor(fi)
    const i1 = Math.min(n - 1, i0 + 1)
    return ts[i0] + (ts[i1] - ts[i0]) * (fi - i0)
  }

  function onMove(e: React.MouseEvent) {
    const vx = clientToVx(e.clientX)
    const frac = (vx - PAD_L) / plotW
    setHi(Math.max(0, Math.min(n - 1, Math.round(frac * (n - 1)))))
    if (drag) setDrag({ a: drag.a, b: vx })
  }
  function onDown(e: React.MouseEvent) {
    // точку нажатия помним ВСЕГДА, даже когда зум диапазоном не подключён:
    // по ней отличаем клик (раскрыть) от протяжки (выделить интервал)
    pressRef.current = { x: e.clientX, y: e.clientY }
    if (!onZoom || n < 2) return
    const vx = clientToVx(e.clientX)
    setDrag({ a: vx, b: vx })
  }
  function onUp(e: React.MouseEvent) {
    let zoomed = false
    if (drag && onZoom) {
      const x0 = Math.min(drag.a, drag.b)
      const x1 = Math.max(drag.a, drag.b)
      if (x1 - x0 > 6) {
        onZoom(vxToTime(x0), vxToTime(x1)) // порог: клик не зумит
        zoomed = true
      }
    }
    setDrag(null)
    const p = pressRef.current
    pressRef.current = null
    // мышь не уехала → это клик, а не выделение
    if (!zoomed && onExpand && p &&
        Math.abs(e.clientX - p.x) < 5 && Math.abs(e.clientY - p.y) < 5) {
      onExpand()
    }
  }

  const fmtTime =
    fmtTimeProp ??
    ((t: number) =>
      new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))
  const tipLeftPct = hi != null ? ((sx(hi) / W) * 100) : 0
  const tipRight = tipLeftPct > 60

  return (
    <div className="sac">
      <div className="mchart-legend">
        {series.map((s) => (
          <span key={s.name} className="mchart-leg">
            <span className="mchart-dot" style={{ background: s.color }} />
            {s.name}
            {hi != null && (
              <b className="sac-legval">{fmtVal(Math.abs(s.values[hi] ?? 0))}</b>
            )}
          </span>
        ))}
      </div>
      <div className="sac-wrap">
        <svg
          ref={ref}
          className={`line-chart${onZoom ? ' sac-zoomable' : ''}${onExpand ? ' sac-expandable' : ''}`}
          viewBox={`0 0 ${W} ${H}`}
          style={{ height: H }}
          preserveAspectRatio="none"
          role="img"
          onMouseMove={onMove}
          onMouseDown={onDown}
          onMouseUp={onUp}
          onMouseLeave={() => {
            setHi(null)
            setDrag(null)
          }}
        >
          <defs>
            {series.map((s) => (
              <linearGradient key={s.name} id={gid(s.color)} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={s.color} stopOpacity={fillTop} />
                <stop offset="100%" stopColor={s.color} stopOpacity={fillBot} />
              </linearGradient>
            ))}
          </defs>
          {grid.map((g, i) => (
            <line key={i} x1={PAD_L} y1={g.y} x2={W - PAD_R} y2={g.y} className="chart-grid" />
          ))}
          {areas.map(({ s, d }) => (
            <path
              key={s.name}
              d={d}
              fill={`url(#${gid(s.color)})`}
              stroke={s.color}
              strokeWidth={1.4}
              strokeLinejoin="round"
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
          ))}
          {xt.map((tk) => (
            <line
              key={`x${tk.t}`}
              x1={tk.x}
              y1={PAD_T}
              x2={tk.x}
              y2={PAD_T + plotH}
              className="sac-xgrid"
              vectorEffect="non-scaling-stroke"
            />
          ))}
          {drag && Math.abs(drag.b - drag.a) > 1 && (
            <rect
              x={Math.min(drag.a, drag.b)}
              y={PAD_T}
              width={Math.abs(drag.b - drag.a)}
              height={plotH}
              className="sac-brush"
            />
          )}
          {hi != null && !drag && (
            <line
              x1={sx(hi)}
              y1={PAD_T}
              x2={sx(hi)}
              y2={PAD_T + plotH}
              className="sac-cursor"
              vectorEffect="non-scaling-stroke"
            />
          )}
        </svg>
        {/* подписи осей — HTML, чтобы не растягивались вместе с SVG (preserveAspectRatio=none) */}
        {grid.map((g, i) => (
          <span
            key={i}
            className="sac-ylabel"
            style={{ top: `${(g.y / H) * 100}%`, width: `${(PAD_L / W) * 100}%` }}
          >
            {fmt(Math.abs(g.v))}
          </span>
        ))}
        {xt.map((tk) => (
          <span key={tk.t} className="sac-xtick" style={{ left: `${(tk.x / W) * 100}%` }}>
            {tk.label}
          </span>
        ))}
        {hi != null && (
          <div
            className="sac-tip"
            style={tipRight ? { right: `${100 - tipLeftPct}%` } : { left: `${tipLeftPct}%` }}
          >
            <div className="sac-tip-t">{fmtTime(ts[hi])}</div>
            {series.map((s) => (
              <div key={s.name} className="sac-tip-row">
                <span className="mchart-dot" style={{ background: s.color }} />
                <span className="sac-tip-name">{s.name}</span>
                <span className="sac-tip-val">{fmtVal(Math.abs(s.values[hi] ?? 0))}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
