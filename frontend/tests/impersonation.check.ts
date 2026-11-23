// Вход админа под чужой учеткой, проверка интерфейса: гоняет
// backend/tests/test_impersonation_front.py (сборка rolldown + node). Печатает JSON с итогом.
// Хранилища - заглушки из --banner. До загрузки модуля в localStorage лежит вход, начатый до
// обновления: токен админа должен переехать в sessionStorage вкладки и больше не появляться
// в localStorage, общем для всех вкладок и живущем на диске. Вкладка входа подтверждает, что
// жива, токеном админа (уходя - keepalive-запросом), соседняя вкладка без него не шлет ничего.
import { getToken, IMP_BEAT_MS, IMP_REBEAT_MS, IMP_VISIBLE_MIN_MS, impersonating, impersonationAlive, keepImpersonationAlive,
  startImpersonation, stopImpersonation } from '../src/api'

type Store = Storage & { _m: Map<string, string> }
const local = globalThis.localStorage as Store
const session = globalThis.sessionStorage as Store
const dump = (s: Store) => Object.fromEntries(s._m)

const calls: { url: string; auth: string }[] = []
const beats: { url: string; auth: string; body: string; keepalive: boolean }[] = []
let aliveStatus = 200
globalThis.fetch = (async (url: string, init: RequestInit) => {
  const headers = (init.headers ?? {}) as Record<string, string>
  if (url.endsWith('/alive')) {
    beats.push({ url, auth: headers.Authorization ?? '', body: String(init.body), keepalive: !!init.keepalive })
    return new Response(JSON.stringify({ alive: aliveStatus === 200 }), { status: aliveStatus })
  }
  calls.push({ url, auth: headers.Authorization ?? '' })
  if (url.endsWith('/stop')) return new Response(null, { status: 204 })
  return new Response(JSON.stringify({ access_token: 'imp-new', imp_id: 8, username: 'u', expires_at: '' }))
}) as typeof fetch

const migrated = { local: dump(local), session: dump(session), impersonating: impersonating() }
await stopImpersonation()
const stopped = { token: getToken(), local: dump(local), session: dump(session), calls: [...calls] }
await startImpersonation(5)
const started = { token: getToken(), local: dump(local), session: dump(session), impersonating: impersonating() }
const alive = [await impersonationAlive(), await impersonationAlive(true)]
aliveStatus = 404
alive.push(await impersonationAlive())
aliveStatus = 503
alive.push(await impersonationAlive())

// Когда вкладка входа подтверждает: таймеры, часы и события - подделки, считаем запросы /alive
aliveStatus = 200
let clock = 1_000_000
Date.now = () => clock
const timers = new Map<number, { fn: () => void; at: number; every: number }>()
let nextId = 0
const win = new EventTarget()
const doc = Object.assign(new EventTarget(), { visibilityState: 'visible' })
Object.assign(globalThis.window, {
  addEventListener: win.addEventListener.bind(win),
  removeEventListener: win.removeEventListener.bind(win),
  setTimeout: (fn: () => void, ms: number) => (timers.set(++nextId, { fn, at: clock + ms, every: 0 }), nextId),
  setInterval: (fn: () => void, ms: number) => (timers.set(++nextId, { fn, at: clock + ms, every: ms }), nextId),
  clearTimeout: (id: number) => timers.delete(id),
  clearInterval: (id: number) => timers.delete(id),
})
globalThis.document = doc as unknown as Document
const advance = (ms: number) => {
  const end = clock + ms
  for (;;) {
    const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0]
    if (!due) break
    const [id, t] = due
    clock = t.at
    if (t.every) t.at += t.every
    else timers.delete(id)
    t.fn()
  }
  clock = end
}
const kinds = () => beats.slice(mark).map((b) => (b.keepalive ? 'leave' : 'beat'))
const show = (persisted: boolean) => win.dispatchEvent(Object.assign(new Event('pageshow'), { persisted }))
let mark = beats.length
const stop = keepImpersonationAlive(() => undefined, () => false)
// загрузка: сразу, обычный pageshow - не повтор, через IMP_REBEAT_MS - второй раз
show(false)
const schedule: Record<string, string[]> = { load: kinds() }
advance(IMP_REBEAT_MS)
schedule.rebeat = kinds()
advance(IMP_BEAT_MS - IMP_REBEAT_MS)
schedule.minute = kinds()
// возврат на вкладку: только если с прошлого подтверждения прошло IMP_VISIBLE_MIN_MS
mark = beats.length
advance(1000)
doc.dispatchEvent(new Event('visibilitychange'))
advance(IMP_VISIBLE_MIN_MS)
doc.dispatchEvent(new Event('visibilitychange'))
doc.dispatchEvent(new Event('visibilitychange'))
schedule.visible = kinds()
// возврат из bfcache: сразу и еще раз; уход - leaving
mark = beats.length
show(true)
advance(IMP_REBEAT_MS)
win.dispatchEvent(new Event('pagehide'))
schedule.bfcache = kinds()
// после размонтирования - ни таймеров, ни событий, и назначенный перед ним повтор отменен
show(true)
stop()
mark = beats.length
advance(10 * IMP_BEAT_MS)
win.dispatchEvent(new Event('pagehide'))
show(true)
schedule.stopped = [...kinds(), ...[...timers.keys()].map(String)]
// соседняя вкладка: тот же localStorage, свой sessionStorage
session._m.clear()
const otherTab = impersonating()
const otherBeat = await impersonationAlive(true)

console.log(JSON.stringify({ migrated, stopped, started, otherTab, alive, beats: beats.slice(0, 4), otherBeat, schedule }))
