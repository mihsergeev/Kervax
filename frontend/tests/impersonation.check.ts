// Вход админа под чужой учеткой, проверка интерфейса: гоняет
// backend/tests/test_impersonation_front.py (сборка rolldown + node). Печатает JSON с итогом.
// Хранилища - заглушки из --banner. До загрузки модуля в localStorage лежит вход, начатый до
// обновления: токен админа должен переехать в sessionStorage вкладки и больше не появляться
// в localStorage, общем для всех вкладок и живущем на диске. Вкладка входа подтверждает, что
// жива, токеном админа (уходя - keepalive-запросом), соседняя вкладка без него не шлет ничего.
import { getToken, impersonating, impersonationAlive, startImpersonation, stopImpersonation } from '../src/api'

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
// соседняя вкладка: тот же localStorage, свой sessionStorage
session._m.clear()
const otherTab = impersonating()
const otherBeat = await impersonationAlive(true)

console.log(JSON.stringify({ migrated, stopped, started, otherTab, alive, beats, otherBeat }))
