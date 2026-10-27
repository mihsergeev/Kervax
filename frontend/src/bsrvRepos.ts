import type { BackupServerExtra, RepoStat, Server } from './api'
import { srvMutesNow } from './serverUtils'

// Состояние и беды репозиториев сервера бэкапов. Отдельно от BackupsPage, потому что по ним
// же считают плитка раздела "Бэкапы" и главная (sectionIssues.ts): правило одно на всех.

// Блок helper'а backupserver-setup 0.23: кто чистит репозитории и когда из них что-то
// удалялось. Старше 15 минут - helper встал, по такому блоку не судим.
export function bsrvExtra(s: Server): BackupServerExtra | null {
  const b = s.last_report?.extras?.['backup-server']
  if (!b || b.v !== 1 || !b.ts) return null
  const ref = s.last_report?.clock_unix || Date.now() / 1000
  return ref - b.ts > 15 * 60 ? null : b
}

// Беды репозиториев сервера бэкапов, по одной на репозиторий и вид. Одна функция для всех, кто
// их считает: плитка "Бэкапы" (в разделе и на главной), бейджи разделов карточки сервера и группы
// списка репозиториев. Раньше у каждого было свое правило, и они разошлись: опоздавший бэкап,
// у которого прямо сейчас идет новый прогон, плитка и бейдж считали, а список прятал его в
// "В порядке" с пометкой "идет бэкап".
// - repo: битый, опоздал или лок без времени - те же репозитории, что в алерте backup_repo
//   (bsrv_repo_reasons считает бэкенд с ритмом бэкапов). Идущий прогон опоздание не снимает:
//   пока он не записал снапшот, бэкапа нет;
// - lock: висячий лок дольше суток (алерт backup_lock), лок идущего бэкапа его освежает;
// - prune: не работает чистка (алерт backup_prune, считает бэкенд);
// - check: не прошла недельная проверка, моложе двух недель - правило failed_checks бэкенда
//   (алерт backup_check). В плитку и бейдж она приходит строкой с бэкенда (srvIssues), здесь -
//   для списка.
// Архив и приглушенные репозитории не в счет. off - алерт этого вида на сервере заглушен:
// плитка и бейдж такую беду не считают, в списке репозиторий среди приглушенных.
export type RepoProblemKind = 'repo' | 'lock' | 'prune' | 'check'
export type RepoProblem = { repo: string; kind: RepoProblemKind; alert: string; why: string; off: boolean; since?: number }
const REPO_ALERT: Record<RepoProblemKind, string> = {
  repo: 'backup_repo', lock: 'backup_lock', prune: 'backup_prune', check: 'backup_check',
}
export const LOCK_LONG_SECONDS = 86400
export function bsrvRepoProblems(s: Server): RepoProblem[] {
  const repos: RepoStat[] = s.last_report?.backup_server?.repos ?? []
  const muted = new Set(s.backup_repo_mutes ?? [])
  const archived = new Set(Object.keys(s.backup_repo_archive ?? {}).filter((k) => !k.includes('|')))
  const off = srvMutesNow(s)
  const now = Date.now() / 1000
  const ext = bsrvExtra(s)
  const out: RepoProblem[] = []
  const add = (repo: string, kind: RepoProblemKind, why: string, since?: number) =>
    out.push({ repo, kind, alert: REPO_ALERT[kind], why, off: off.has(REPO_ALERT[kind]), since })
  for (const r of repos) {
    if (!r.name || muted.has(r.name) || archived.has(r.name)) continue
    const reason = s.bsrv_repo_reasons?.[r.name]
    if (reason) add(r.name, 'repo', reason)
    if (r.locked && r.lock_ts && now - r.lock_ts > LOCK_LONG_SECONDS) add(r.name, 'lock', '', r.lock_ts)
    const prune = s.bsrv_prune?.[r.name]
    if (prune) add(r.name, 'prune', prune)
    const x = ext?.repos?.[r.name]
    if (x && x.check_ok === 0 && !!x.check_ts && now - x.check_ts < 14 * 86400) add(r.name, 'check', '', x.check_ts)
  }
  return out
}


// Лок держится всё время бэкапа и освежается раз в 5 мин — сам по себе он значит
// «бэкап идёт», а не «сломалось». Проблема — лок, который перестали освежать.
const LOCK_STUCK_SECONDS = 30 * 60
// Висячий лок моложе суток снимают сами чистка и клиент (restic unlock перед работой).
// Дольше суток он уже точно остановил ежедневную чистку: с этого порога (LOCK_LONG_SECONDS)
// шлется алерт backup_lock, и тут с него же репозиторий считается проблемным.
// сколько секунд висит висячий лок (0 - лока нет или он живой); helper 0.23 отдает
// самый старый висячий лок, так что это время, с которого репозиторий заблокирован
export function lockAge(r: RepoStat): number {
  if (!r.locked || !r.lock_ts) return 0
  const age = Date.now() / 1000 - r.lock_ts
  return age > LOCK_STUCK_SECONDS ? age : 0
}
export function lockStuck(r: RepoStat): boolean {
  if (!r.locked) return false
  if (!r.lock_ts) return true // helper < v5 времени лока не отдаёт — считаем висячим
  return lockAge(r) > 0
}
export function lockLong(r: RepoStat): boolean {
  return !!r.locked && (!r.lock_ts || lockAge(r) > LOCK_LONG_SECONDS)
}
// stuck - висячий лок моложе суток: видно, но проблемой не считается (уйдет сам)
export type RepoState = 'archived' | 'muted' | 'invalid' | 'locked' | 'stuck' | 'running' | 'stale' | 'ok'
// Что панель знает о репозиториях сервера сверх них самих: приглушенные, архив и беды
// (bsrvRepoProblems - та же функция, по которой считают плитка раздела и бейджи карточки, так
// что список и счетчики расходиться не могут). Из бед для удобства:
// reasons - почему репозиторий в алерте backup_repo (бэкенд, с ритмом бэкапов),
// prune - не работает чистка (не открывает, падает, не запускается),
// checks - не прошла последняя недельная проверка, моложе двух недель (как у алерта backup_check).
export type RepoCtx = {
  muted: Set<string>; archived: Set<string>; reasons: Record<string, string>; prune: Record<string, string>
  checks: Set<string>; probs: Map<string, RepoProblem[]>
}
export function repoCtx(s: Server): RepoCtx {
  const all = bsrvRepoProblems(s)
  const probs = new Map<string, RepoProblem[]>()
  for (const p of all) probs.set(p.repo, [...(probs.get(p.repo) ?? []), p])
  const of = (k: RepoProblem['kind']) => all.filter((p) => p.kind === k)
  return {
    muted: new Set(s.backup_repo_mutes ?? []),
    archived: new Set(Object.keys(s.backup_repo_archive ?? {}).filter((k) => !k.includes('|'))),
    reasons: Object.fromEntries(of('repo').map((p) => [p.repo, p.why])),
    prune: Object.fromEntries(of('prune').map((p) => [p.repo, p.why])),
    checks: new Set(of('check').map((p) => p.repo)),
    probs,
  }
}
export function repoState(r: RepoStat, c: RepoCtx): RepoState {
  if (c.archived.has(r.name)) return 'archived'
  if (c.muted.has(r.name)) return 'muted'
  if (!r.valid) return 'invalid'
  if (lockLong(r)) return 'locked'
  // опоздание раньше идущего прогона: пока он не записал снапшот, бэкапа нет, а алерт и плитка
  // уже считают репозиторий проблемным (27.10 опоздавший с новым прогоном стоял "в порядке")
  if (c.reasons[r.name]) return 'stale'
  if (repoRunning(r)) return 'running'
  if (lockStuck(r)) return 'stuck'
  return 'ok'
}
// лок живой: его держит идущий сейчас бэкап (освежает раз в 5 минут)
export function repoRunning(r: RepoStat): boolean {
  return !!r.locked && !lockStuck(r)
}
// Проблемный = есть беда, по которой алерт не заглушен (для счетчиков и групп списка). Беды -
// bsrvRepoProblems, по ним же плитка раздела и бейджи: неработающая чистка и не прошедшая
// проверка тоже, при любом другом состоянии
export function repoBad(r: RepoStat, c: RepoCtx): boolean {
  return (c.probs.get(r.name) ?? []).some((p) => !p.off)
}
// Тон репозитория для списка и шапки: красный - проблема (как в счетчиках), желтый - висячий
// лок моложе суток или старые снапшоты не вычищаются, серый - приглушен
export type RepoTone = 'bad' | 'warn' | 'ok' | 'mute'
export function repoRot(s: Server, name: string) {
  return (s.bsrv_cleanup?.rotation ?? []).find((x) => x.repo === name)
}
export function repoTone(s: Server, r: RepoStat, c: RepoCtx): RepoTone {
  const st = repoState(r, c)
  if (st === 'muted') return 'mute'
  if (repoBad(r, c)) return 'bad'
  // беда есть, но ее алерт на сервере заглушен: в счетчиках ее нет, в списке - к приглушенным
  if (c.probs.has(r.name)) return 'mute'
  if (st === 'stuck' || repoRot(s, r.name)) return 'warn'
  return 'ok'
}
