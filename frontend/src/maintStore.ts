import { useEffect, useState } from 'react'
import { listMaint, type MaintWindow } from './api'

// Окна обслуживания для плашек в карточках и значков в списках: один запрос на всех и раз в
// минуту (окно начинается и кончается по часам, чаще незачем)
let cache: { ts: number; data: MaintWindow[] } = { ts: 0, data: [] }
let inflight = false
const subs = new Set<(d: MaintWindow[]) => void>()

function refresh(force = false): void {
  if (inflight || (!force && Date.now() - cache.ts < 55_000)) return
  inflight = true
  listMaint()
    .then((d) => {
      cache = { ts: Date.now(), data: d }
      subs.forEach((f) => f(d))
    })
    .catch(() => {})
    .finally(() => {
      inflight = false
    })
}

export function useMaint(): MaintWindow[] {
  const [data, setData] = useState(cache.data)
  useEffect(() => {
    subs.add(setData)
    refresh()
    const id = window.setInterval(() => refresh(), 60_000)
    return () => {
      subs.delete(setData)
      window.clearInterval(id)
    }
  }, [])
  return data
}

// После правки окон: перечитать сразу, а не через минуту
export function maintReload(): void {
  refresh(true)
}

// Идущее окно, в которое попал сервер или монитор
export function maintFor(ws: MaintWindow[], kind: 'server' | 'check', id: number): MaintWindow | undefined {
  return ws.find((w) => w.active && (kind === 'server' ? w.server_ids : w.check_ids).includes(id))
}

// "20:00" сегодня или "20:00 15.10" другим днем
export function maintTime(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return d.toDateString() === new Date().toDateString()
    ? hm
    : `${hm} ${d.toLocaleDateString([], { day: '2-digit', month: '2-digit' })}`
}
