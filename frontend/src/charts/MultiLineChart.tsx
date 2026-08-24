// Мультисерийный SVG-график с легендой (несколько линий на общих осях).
// Без внешних библиотек. y=null рвёт линию (пропуск данных).

export type Series = {
  name: string
  color: string
  points: { t: number; y: number | null }[]
}

type Props = {
  series: Series[]
  unit?: string
  fmtY?: (v: number) => string
  fmtX?: (t: number) => string
  yMin?: number // фикс. нижняя граница (напр. 0 для %)
  yMax?: number // фикс. верхняя граница (напр. 100 для %)
  height?: number
}

const W = 640
const PAD_L = 48
const PAD_R = 10
const PAD_T = 10
const PAD_B = 20

export function MultiLineChart({
  series,
  unit = '',
  fmtY,
  fmtX = (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
  yMin: yMinFix,
  yMax: yMaxFix,
  height = 150,
}: Props) {
  const H = height
  const allY = series.flatMap((s) => s.points.map((p) => p.y)).filter((v): v is number => v != null)
  const allT = series.flatMap((s) => s.points.map((p) => p.t))
  if (allY.length === 0 || allT.length === 0) {
    return <div className="chart-empty">—</div>
  }
  const tMin = Math.min(...allT)
  const tMax = Math.max(...allT)
  let yMin = yMinFix ?? Math.min(...allY)
  let yMax = yMaxFix ?? Math.max(...allY)
  if (yMin === yMax) {
    yMin = Math.max(0, yMin - 1)
    yMax += 1
  }
  if (yMaxFix == null) yMax += (yMax - yMin) * 0.12

  const plotW = W - PAD_L - PAD_R
  const plotH = H - PAD_T - PAD_B
  const sx = (t: number) =>
    tMax === tMin ? PAD_L + plotW / 2 : PAD_L + ((t - tMin) / (tMax - tMin)) * plotW
  const sy = (y: number) => PAD_T + (1 - (y - yMin) / (yMax - yMin)) * plotH

  const path = (pts: { t: number; y: number | null }[]) => {
    const segs: string[] = []
    let started = false
    for (const p of pts) {
      if (p.y == null) {
        started = false
        continue
      }
      segs.push(`${started ? 'L' : 'M'}${sx(p.t).toFixed(1)} ${sy(p.y).toFixed(1)}`)
      started = true
    }
    return segs.join(' ')
  }

  const range = yMax - yMin
  const dec = range >= 20 ? 0 : range >= 2 ? 1 : 2
  const fmt = fmtY ?? ((v: number) => v.toFixed(dec))
  const gridN = 3
  const grid = Array.from({ length: gridN + 1 }, (_, i) => yMin + ((yMax - yMin) * i) / gridN)

  return (
    <div className="mchart">
      <div className="mchart-legend">
        {series.map((s) => (
          <span key={s.name} className="mchart-leg">
            <span className="mchart-dot" style={{ background: s.color }} />
            {s.name}
          </span>
        ))}
      </div>
      <svg className="line-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img">
        {grid.map((v, i) => {
          const y = sy(v)
          return (
            <g key={i}>
              <line x1={PAD_L} y1={y} x2={W - PAD_R} y2={y} className="chart-grid" />
              <text x={PAD_L - 6} y={y + 3} className="chart-axis" textAnchor="end">
                {fmt(v)}
              </text>
            </g>
          )
        })}
        {series.map((s) => (
          <path
            key={s.name}
            d={path(s.points)}
            fill="none"
            stroke={s.color}
            strokeWidth={1.8}
            strokeLinejoin="round"
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />
        ))}
        <text x={PAD_L} y={H - 6} className="chart-axis" textAnchor="start">
          {fmtX(tMin)}
        </text>
        <text x={W - PAD_R} y={H - 6} className="chart-axis" textAnchor="end">
          {fmtX(tMax)}
        </text>
        {unit && (
          <text x={W - PAD_R} y={PAD_T + 4} className="chart-axis" textAnchor="end">
            {unit}
          </text>
        )}
      </svg>
    </div>
  )
}
