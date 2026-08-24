// Лёгкий SVG-график без внешних библиотек: линия + заливка под ней,
// сетка по Y и подписи начала/конца по X. y=null рвёт линию (пропуск данных).

export type Point = { t: number; y: number | null }

type Props = {
  points: Point[]
  unit: string
  color?: string
  // форматтер подписи времени под осью X
  fmtX?: (t: number) => string
  height?: number
}

const W = 640
const PAD_L = 44
const PAD_R = 10
const PAD_T = 10
const PAD_B = 20

export function LineChart({
  points,
  unit,
  color = '#4b74ff',
  fmtX = (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
  height = 160,
}: Props) {
  const H = height
  const ys = points.map((p) => p.y).filter((v): v is number => v != null)
  if (ys.length === 0) {
    return <div className="chart-empty">—</div>
  }
  const ts = points.map((p) => p.t)
  const tMin = Math.min(...ts)
  const tMax = Math.max(...ts)
  let yMin = Math.min(...ys)
  let yMax = Math.max(...ys)
  if (yMin === yMax) {
    yMin = Math.max(0, yMin - 1)
    yMax = yMax + 1
  }
  // немного «воздуха» сверху
  yMax = yMax + (yMax - yMin) * 0.12

  const plotW = W - PAD_L - PAD_R
  const plotH = H - PAD_T - PAD_B
  const sx = (t: number) =>
    tMax === tMin ? PAD_L + plotW / 2 : PAD_L + ((t - tMin) / (tMax - tMin)) * plotW
  const sy = (y: number) => PAD_T + (1 - (y - yMin) / (yMax - yMin)) * plotH

  // сегменты между пропусками
  const segs: Array<Array<[number, number]>> = []
  let cur: Array<[number, number]> = []
  for (const p of points) {
    if (p.y == null) {
      if (cur.length) segs.push(cur)
      cur = []
    } else {
      cur.push([sx(p.t), sy(p.y)])
    }
  }
  if (cur.length) segs.push(cur)

  const line = (seg: Array<[number, number]>) =>
    seg.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join(' ')

  // заливка под самым длинным (первым) непрерывным сегментом
  const main = segs.reduce((a, b) => (b.length > a.length ? b : a), segs[0] ?? [])
  const area =
    main.length > 1
      ? `${line(main)} L${main[main.length - 1][0].toFixed(1)} ${(H - PAD_B).toFixed(
          1,
        )} L${main[0][0].toFixed(1)} ${(H - PAD_B).toFixed(1)} Z`
      : ''

  const gridN = 3
  const gridY = Array.from({ length: gridN + 1 }, (_, i) => yMin + ((yMax - yMin) * i) / gridN)
  // число знаков — по ширине диапазона, чтобы близкие подписи не схлопывались
  const range = yMax - yMin
  const dec = range >= 20 ? 0 : range >= 2 ? 1 : 2
  const fmtVal = (v: number) => v.toFixed(dec)
  const gid = `lc-${color.replace('#', '')}`

  return (
    <svg className="line-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.28" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {gridY.map((v, i) => {
        const y = sy(v)
        return (
          <g key={i}>
            <line
              x1={PAD_L}
              y1={y}
              x2={W - PAD_R}
              y2={y}
              className="chart-grid"
            />
            <text x={PAD_L - 6} y={y + 3} className="chart-axis" textAnchor="end">
              {fmtVal(v)}
            </text>
          </g>
        )
      })}
      {area && <path d={area} fill={`url(#${gid})`} stroke="none" />}
      {segs.map((seg, i) =>
        seg.length > 1 ? (
          <path
            key={i}
            d={line(seg)}
            fill="none"
            stroke={color}
            strokeWidth={1.8}
            strokeLinejoin="round"
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />
        ) : (
          <circle key={i} cx={seg[0][0]} cy={seg[0][1]} r={2.2} fill={color} />
        ),
      )}
      <text x={PAD_L} y={H - 6} className="chart-axis" textAnchor="start">
        {fmtX(tMin)}
      </text>
      <text x={W - PAD_R} y={H - 6} className="chart-axis" textAnchor="end">
        {fmtX(tMax)}
      </text>
      <text x={W - PAD_R} y={PAD_T + 4} className="chart-axis" textAnchor="end">
        {unit}
      </text>
    </svg>
  )
}
