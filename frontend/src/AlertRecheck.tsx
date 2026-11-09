import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type Server } from './api'
import { useI18n } from './i18n'

export type RecheckRef = { object_type: 'server' | 'check'; object_id: number; kind: string }
export type RecheckJob = RecheckRef & {
  id: string; status: 'checking' | 'fixed' | 'remains' | 'unavailable'; message: string
  created_at: string; done_at?: string | null
}
export const recheckKey = (r: RecheckRef) => `${r.object_type}:${r.object_id}:${r.kind}`

// Kept in sync with app.alert_recheck.SOURCES. The API independently authorizes
// the object, its section and group, and never trusts a client-supplied command.
const SOURCES: Record<string, string> = {
  kube_expiry: 'kube_expiry', flux_down: 'kube_expiry', flux_stale: 'kube_expiry',
  ssh_risk: 'changes', access_risk: 'changes',
  'home:ssh-ssh_password': 'changes', 'home:access-s': 'changes', 'home:access-w': 'changes',
  'home:sw-eol': 'software', 'home:sw-soon': 'software',
  'home:d-acc': 'docker', 'home:d-old': 'docker', 'home:dockrisk': 'docker', 'home:noboot-d': 'docker',
  'home:k-acc': 'kube',
  'home:dbdump': 'backup', 'home:bk-http': 'backup',
  mem: 'mem', disk: 'disk', inode: 'disk', conntrack: 'conntrack', temp: 'temp',
}

export function recheckUnavailable(r: RecheckRef, server?: Server, local = false): string {
  if (r.object_type === 'check') return local && r.kind !== 'status'
    ? 'Для этой проверки изнутри сервера пока нет свежего разбора. Откройте карточку объекта.' : ''
  const source = SOURCES[r.kind]
  if (!source) return 'Для этого вида пока нет принудительной проверки. Откройте карточку объекта.'
  if (!server?.online) return 'Сервер не на связи. Свежую проверку сейчас выполнить некому.'
  if (!server.last_report?.caps?.alert_recheck) return 'Обновите агент: нужна поддержка свежих проверок.'
  const root = ['kube_expiry', 'changes', 'backup'].includes(source) || source === 'software' && !!server.last_report?.db_stats?.length
  if (root && !server.last_report?.setup_versions?.['recheck-setup']) return 'Установите read-only helper recheck-setup.'
  if (root && server.last_report?.caps?.alert_recheck_root === false) return 'Очередь read-only helper недоступна агенту. Переустановите recheck-setup.'
  const backupVersion = String(server.last_report?.setup_versions?.['backup-setup'] || '0').split('.').map(Number)
  if (source === 'backup' && !(backupVersion[0] > 0 || backupVersion[1] >= 50)) return 'Обновите backup-helper до 0.50: нужна read-only команда inspect.'
  return ''
}

export function useAlertRechecks(onChanged: () => void) {
  const [jobs, setJobs] = useState<Record<string, RecheckJob>>({})
  const [last, setLast] = useState<RecheckRef[]>([])
  const [err, setErr] = useState('')
  const [watch, setWatch] = useState(0)
  const alive = useRef(true)
  const change = useRef(onChanged)
  change.current = onChanged
  const previous = useRef<Record<string, RecheckJob>>({})
  const accept = useCallback((rows: RecheckJob[], fromPoll = false) => {
    if (!alive.current) return false
    const next = { ...previous.current }
    let completed = false
    for (const r of rows) {
      const key = recheckKey(r)
      const old = next[key]
      if (fromPoll && old) {
        // A poll may have taken its snapshot before the current POST was processed.
        if (!old.id) continue
        if (old.id !== r.id) {
          if (!(Date.parse(r.created_at) > Date.parse(old.created_at))) continue
          if (old.status === 'checking') {
            next[key] = { ...old, status: 'unavailable', message: 'Проверку заменила более новая. Запустите ее снова.' }
            continue
          }
        } else {
          if (old.status !== 'checking' && r.status === 'checking') continue
          completed ||= old.status === 'checking' && r.status !== 'checking'
        }
      }
      next[key] = r
    }
    previous.current = next
    setJobs(next)
    return completed
  }, [])
  useEffect(() => {
    alive.current = true
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = () => api<RecheckJob[]>('/api/alert-rechecks').then((rows) => {
      if (!active) return
      setErr('')
      if (accept(rows, true)) change.current()
    }).catch(() => {
      if (active && Object.values(previous.current).some((x) => x.status === 'checking')) {
        setErr('Не удалось получить результат проверки. Повторяем запрос; алерт не скрыт.')
      }
    }).finally(() => {
      if (active && Object.values(previous.current).some((x) => x.status === 'checking')) timer = setTimeout(poll, 2000)
    })
    poll()
    return () => { active = false; alive.current = false; clearTimeout(timer) }
  }, [accept, watch])
  const run = async (refs: RecheckRef[]) => {
    const unique = [...new Map(refs.map((r) => [recheckKey(r), r])).values()]
      .filter((r) => previous.current[recheckKey(r)]?.status !== 'checking')
    if (!unique.length) return
    setLast(unique)
    setErr('')
    const loading = unique.map((r): RecheckJob => ({ ...r, id: '', status: 'checking', message: 'Проверяю', created_at: new Date().toISOString() }))
    accept(loading)
    setWatch((v) => v + 1)
    // Bound large groups. Every POST is independently authorized and rate limited.
    for (let i = 0; i < unique.length; i += 4) {
      const rows = await Promise.all(unique.slice(i, i + 4).map(async (r) => {
        try { return await api<RecheckJob>('/api/alert-rechecks', { method: 'POST', body: JSON.stringify(r) }) }
        catch (e) { return { ...r, id: '', status: 'unavailable' as const, message: String((e as Error).message || e), created_at: new Date().toISOString() } }
      }))
      accept(rows)
      if (alive.current && rows.some((r) => r.id && r.status !== 'checking')) change.current()
    }
  }
  return { jobs, run, last, err }
}

export function RecheckButton({ refs, jobs, run, blocked = {} }: {
  refs: RecheckRef[]; jobs: Record<string, RecheckJob>; run: (refs: RecheckRef[]) => Promise<void>; blocked?: Record<string, string>
}) {
  const { t } = useI18n()
  const eligible = refs.filter((r) => !blocked[recheckKey(r)])
  if (!eligible.length) return null
  const rows = refs.map((r) => jobs[recheckKey(r)]).filter(Boolean)
  const busy = rows.some((r) => r.status === 'checking')
  const still = rows.some((r) => r.status === 'remains')
  const failed = rows.some((r) => r.status === 'unavailable')
  const label = busy ? 'Проверяю' : still ? 'Проблема осталась' : failed ? 'Не удалось проверить' : 'Исправил'
  const title = failed ? rows.find((r) => r.status === 'unavailable')?.message : 'Исправил: запросить свежую проверку. Алерт исчезнет только после подтверждения.'
  return <span className="home-recheck-wrap" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
    <button type="button" className={`home-recheck ${still || failed ? 'has-result' : ''}`} disabled={busy}
      title={t(title || '')} aria-label={t('Исправил: перепроверить')} onClick={() => run(eligible)}>
      <span aria-hidden="true">{busy ? '◌' : '☑'}</span> {t(label)}
    </button>
    {eligible.length < refs.length && <span className="muted small" title={Object.values(blocked).map((x) => t(x)).join('\n')}>
      {t('доступно {n} из {total}', { n: eligible.length, total: refs.length })}
    </span>}
  </span>
}

export function RecheckSummary({ refs, jobs, err }: { refs: RecheckRef[]; jobs: Record<string, RecheckJob>; err: string }) {
  const { t } = useI18n()
  if (!refs.length && !err) return null
  const rows = refs.map((r) => jobs[recheckKey(r)]).filter(Boolean)
  const busy = rows.some((r) => r.status === 'checking')
  const n = (s: string) => rows.filter((r) => r.status === s).length
  return <div className="home-recheck-summary small" role="status" aria-live="polite">
    {busy ? t('Проверяю свежие данные…') : t('Проверка завершена')}
    {' · '}{t('подтверждено исправление: {n}', { n: n('fixed') })}
    {' · '}{t('проблема осталась: {n}', { n: n('remains') })}
    {' · '}{t('не удалось проверить: {n}', { n: n('unavailable') })}
    {err && <div className="warn">{t(err)}</div>}
  </div>
}
