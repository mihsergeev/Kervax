// Лента доступности: по бину — один сегмент, цвет = up/degraded/down/unknown.
// Наведение → тултип со временем и статусом. Свой стиль: скруглённая, с делениями.
import { useState } from 'react'
import type { CheckStatus } from '../api'

const COLOR: Record<CheckStatus, string> = {
  up: 'var(--up)',
  degraded: 'var(--degraded)',
  down: 'var(--down)',
  unknown: 'var(--text-muted)',
}

export function StatusBar({
  segments,
}: {
  segments: { status: CheckStatus; title?: string }[]
}) {
  const [hi, setHi] = useState<number | null>(null)
  if (segments.length === 0)
    return <div className="chart-empty statusbar-empty">—</div>
  const n = segments.length
  const tipLeft = hi != null ? ((hi + 0.5) / n) * 100 : 0
  const tipRight = tipLeft > 60
  return (
    <div className="statusbar-wrap">
      <div className="statusbar" role="img" onMouseLeave={() => setHi(null)}>
        {segments.map((s, i) => (
          <span
            key={i}
            className={`statusbar-seg statusbar-seg-${s.status}${hi === i ? ' statusbar-seg-hi' : ''}`}
            onMouseEnter={() => setHi(i)}
            style={{ flex: `1 1 ${100 / n}%`, backgroundColor: COLOR[s.status] }}
          />
        ))}
      </div>
      {hi != null && segments[hi].title && (
        <div
          className="statusbar-tip"
          style={tipRight ? { right: `${100 - tipLeft}%` } : { left: `${tipLeft}%` }}
        >
          <span className="mchart-dot" style={{ background: COLOR[segments[hi].status] }} />
          {segments[hi].title}
        </div>
      )}
    </div>
  )
}
