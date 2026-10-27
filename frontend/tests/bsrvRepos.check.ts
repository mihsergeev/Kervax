// Сверка списка репозиториев сервера бэкапов с плиткой раздела "Бэкапы": гоняет
// backend/tests/test_bsrv_repos_front.py (сборка rolldown + node). Печатает JSON с итогом.
import type { Server } from '../src/api'
import { repoBad, repoCtx, repoState, repoTone } from '../src/bsrvRepos'
import { backupProblems } from '../src/sectionIssues'

const now = Date.now() / 1000
const t = (k: string, p?: Record<string, string | number>) =>
  k.replace(/\{(\w+)\}/g, (_m, n: string) => String(p?.[n] ?? ''))
const repo = (name: string, x: Record<string, unknown> = {}) => ({
  name, valid: true, snapshots: 10, size_bytes: 1, last_activity: now - 3600, ...x,
})
const server = (repos: ReturnType<typeof repo>[], x: Partial<Server> = {}): Server => ({
  id: 15, name: 'bsrv', online: true,
  last_report: { backup_server: { present: true, running: true, repos } },
  ...x,
} as unknown as Server)

const cases: Record<string, Server> = {
  // 27.10 на одной ноде: ночной прогон не записал снапшот, днем пошел новый со свежим локом
  late_running: server(
    [repo('db1', { last_activity: now - 35 * 3600, locked: true, lock_ts: now - 300 }), repo('ok1')],
    { bsrv_repo_reasons: { db1: 'бэкап опоздал: обычно раз в сутки, последний 35 ч назад' } },
  ),
  // свежий лок без опоздания - просто идет бэкап, не проблема
  running: server([repo('db1', { locked: true, lock_ts: now - 300 })]),
  // висячий лок больше суток
  lock_long: server([repo('db1', { locked: true, lock_ts: now - 2 * 86400 })]),
  // алерт опоздания заглушен: ни плитка, ни "Требуют внимания"
  late_alert_muted: server(
    [repo('db1', { last_activity: now - 5 * 86400 })],
    { bsrv_repo_reasons: { db1: 'нет новых бэкапов 5 дн' }, alert_mutes: ['backup_repo'] },
  ),
  // приглушенный репозиторий
  repo_muted: server(
    [repo('db1', { last_activity: now - 5 * 86400 })],
    { backup_repo_mutes: ['db1'] },
  ),
  // чистка не работает
  prune: server([repo('db1')], { bsrv_prune: { db1: 'не открывает репозиторий' } }),
}

const out: Record<string, unknown> = {}
for (const [k, s] of Object.entries(cases)) {
  const ctx = repoCtx(s)
  const repos = s.last_report?.backup_server?.repos ?? []
  out[k] = {
    tiles: backupProblems([s], t).filter((p) => p.srv).map((p) => p.text),
    bad: repos.filter((r) => repoBad(r, ctx)).map((r) => r.name),
    tones: Object.fromEntries(repos.map((r) => [r.name, repoTone(s, r, ctx)])),
    states: Object.fromEntries(repos.map((r) => [r.name, repoState(r, ctx)])),
  }
}
console.log(JSON.stringify(out))
