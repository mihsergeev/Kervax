import { useCallback, useEffect, useState } from 'react'
import { ApiError, agentUpdate, getAnsibleAccess, type Server } from './api'
import { useAuth } from './auth'
import { useI18n } from './i18n'
import { fmtSetupVersion, shortList } from './serverUtils'

// Обновление парка одной кнопкой: агент - OTA через панель (/api/servers/agent-update),
// helper'ы - командой ansible. Панель helper'ы сама не ставит: это root-скрипты, и раздача их
// панелью значила бы, что взлом панели дает root на всем парке (см. kervax_helpers.yml).
// Один код на главную ("Обновления") и баннер на "Серверах": подтверждение, прогресс и итог.

type T = (k: string, p?: Record<string, string | number>) => string

// за столько нода без движения считается застрявшей: агент спрашивает панель каждые
// полминуты, бинарь качается за минуту, на РФ-нодах за DPI докачка видна в agent_update_note
const STUCK_MS = 15 * 60 * 1000
const RUN_KEY = 'kx-agent-run'

// агент отстает от подписанной версии; выключенным сервером панель цель не ставит
export function agentBehind(servers: Server[], avail: string): Server[] {
  return servers.filter((s) => s.enabled && s.agent_version && avail && s.agent_version !== avail)
}

export type HelperGroup = { key: string; name: string; fresh: boolean; a: string; b: string; nodes: Server[] }

// helper -> ноды, где его ставить или обновлять (одной строкой на helper и пару версий)
export function helperGroups(servers: Server[]): HelperGroup[] {
  const by = new Map<string, HelperGroup>()
  for (const s of servers) {
    if (!s.online) continue
    for (const h of s.helper_advice || []) {
      // helper'а на ноде нет вовсе - это первая установка, тем же прогоном
      const fresh = !h.installed
      const a = fmtSetupVersion(h.installed)
      const b = fmtSetupVersion(h.current)
      const key = `${h.name}|${fresh ? '' : a}|${b}`
      const g = by.get(key) ?? { key, name: h.name, fresh, a, b, nodes: [] }
      g.nodes.push(s)
      by.set(key, g)
    }
  }
  return [...by.values()]
}

// хосты, где прогон kervax_helpers.yml что-то поменяет (helper или настройка юнита агента)
export function rolloutHosts(servers: Server[]): string[] {
  return [...new Set(servers.filter((s) => s.online && s.helper_rollout).map(hostOf))].sort()
}

const hostOf = (s: Server) => s.hostname || s.name

// Что обновлять: все, только агент (строка агента и баннер "Серверов") или helper'ы
// на заданных хостах (строка helper'а)
export type UpdScope = { agent: boolean; hosts: string[]; helpers: HelperGroup[] }

// ids - кому поставлена цель агента, hosts - где ждем прогона ansible для helper'ов
export type Run = { ids: number[]; hosts: string[]; v: string; at: number }

function loadRun(): Run | null {
  try {
    const r = JSON.parse(sessionStorage.getItem(RUN_KEY) || 'null')
    return r && Array.isArray(r.ids) && Array.isArray(r.hosts) && typeof r.v === 'string' ? r : null
  } catch {
    return null
  }
}

function saveRun(r: Run | null) {
  try {
    if (r) sessionStorage.setItem(RUN_KEY, JSON.stringify(r))
    else sessionStorage.removeItem(RUN_KEY)
  } catch {
    /* без хранилища прогресс просто не переживет переход между страницами */
  }
}

// Раскатка агента: запуск и прогресс по данным серверов. Запуск помнится на вкладку браузера,
// чтобы прогресс был виден и после перехода между главной и "Серверами"
export function useAgentRollout(
  servers: Server[], avail: string, onServers: (s: Server[]) => void, onUnauthorized: () => void,
) {
  const { t } = useI18n()
  const [run, setRun] = useState<Run | null>(loadRun)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  // минутный тик: "застряла" наступает по времени, а не по новым данным
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!run) return
    const id = window.setInterval(() => setTick((x) => x + 1), 60000)
    return () => window.clearInterval(id)
  }, [run])

  const start = useCallback(async (ids: number[], hosts: string[] = []) => {
    if (!ids.length && !hosts.length) return false
    setBusy(true)
    setErr('')
    try {
      if (ids.length) {
        if (!avail) return false
        const upd = await agentUpdate(avail, ids)
        // ответ - только затронутые серверы: подменяем их в общем списке
        const by = new Map(upd.map((s) => [s.id, s]))
        onServers(servers.map((s) => by.get(s.id) ?? s))
      }
      const r = { ids, hosts, v: avail, at: Date.now() }
      saveRun(r)
      setRun(r)
      return true
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized()
      else setErr(e instanceof Error ? e.message : t('Ошибка'))
      return false
    } finally {
      setBusy(false)
    }
  }, [avail, servers, onServers, onUnauthorized, t])

  const close = useCallback(() => {
    saveRun(null)
    setRun(null)
  }, [])

  return { run: run && run.v === avail ? run : null, busy, err, start, close }
}

type NodeState = { s: Server; st: 'done' | 'wait' | 'note' | 'bad'; why: string }

// Где нода сейчас и что делать, если не обновилась: конкретно по ноде, без общих слов
function nodeState(s: Server, run: Run, t: T): NodeState {
  if (s.agent_version === run.v) return { s, st: 'done', why: '' }
  if (s.agent_update_error)
    return { s, st: 'bad', why: t('агент отклонил обновление: {err}', { err: s.agent_update_error }) }
  if (!s.enabled) return { s, st: 'bad', why: t('сервер выключен в панели - обновление ему не ставится') }
  if (s.target_agent_version !== run.v) return { s, st: 'bad', why: t('раскатку на этой ноде отменили') }
  if (!s.online) return { s, st: 'note', why: t('не на связи - обновится сам, когда агент вернется') }
  if (s.agent_update_note) return { s, st: 'note', why: s.agent_update_note }
  if (Date.now() - run.at > STUCK_MS)
    return { s, st: 'bad', why: t('за 15 минут не обновился - на ноде: journalctl -u kervax-agent -n 50; если агент очень старый и сам не обновляется, переустановите его командой из карточки') }
  return { s, st: 'wait', why: '' }
}

// Подтверждение перед запуском: что именно и где обновится
export function UpdateConfirm({ scope, servers, avail, via, busy, onGo, onCancel }: {
  scope: UpdScope; servers: Server[]; avail: string; via: boolean; busy: boolean
  onGo: () => void; onCancel: () => void
}) {
  const { t } = useI18n()
  const lag = scope.agent ? agentBehind(servers, avail) : []
  const on = lag.filter((s) => s.online)
  // без единой ноды на связи агентом обновлять некого: строки агента нет
  const behind = on.length ? lag : []
  const off = behind.filter((s) => !s.online)
  const from = [...new Set(on.map((s) => s.agent_version))].sort().join(', ')
  return (
    <div className="upd-plan">
      <div className="upd-plan-head">{t('Будет обновлено')}</div>
      <ul className="upd-plan-list">
        {behind.length > 0 && (
          <li>
            {t('агент {a} -> {b}: нод - {n} ({list})', {
              a: from, b: avail, n: on.length, list: shortList(on.map((s) => s.name), t, 6),
            })}
            {off.length > 0 && (
              <div className="muted small">
                {t('не на связи: {list} - цель им поставится, обновятся, когда вернутся', { list: shortList(off.map((s) => s.name), t, 6) })}
              </div>
            )}
          </li>
        )}
        {scope.helpers.map((g) => (
          <li key={g.key}>
            {g.fresh
              ? t('helper "{helper}" {b} поставить: нод - {n} ({list})', { helper: g.name, b: g.b, n: g.nodes.length, list: shortList(g.nodes.map((s) => s.name), t, 6) })
              : t('helper "{helper}" {a} -> {b}: нод - {n} ({list})', { helper: g.name, a: g.a, b: g.b, n: g.nodes.length, list: shortList(g.nodes.map((s) => s.name), t, 6) })}
          </li>
        ))}
        {scope.helpers.length === 0 && scope.hosts.length > 0 && (
          <li>{t('настройка юнита агента: нод - {n} ({list})', { n: scope.hosts.length, list: shortList(scope.hosts, t, 6) })}</li>
        )}
      </ul>
      {scope.hosts.length > 0 && (
        <>
          <div className="muted small">
            {behind.length > 0
              ? t('Агент обновится через панель. helper\'ы работают под root, панель их не запускает: команда ниже скопируется при запуске, выполните ее в репозитории ansible.')
              : t('helper\'ы работают под root, панель их не запускает: команда ниже скопируется, выполните ее в репозитории ansible.')}
          </div>
          <AnsibleCmd hosts={scope.hosts} via={via} />
        </>
      )}
      <div className="upd-plan-btns">
        <button disabled={busy} onClick={onGo}>
          {busy ? t('…') : behind.length > 0 ? t('Запустить обновление') : t('Скопировать команду')}
        </button>
        <button className="ghost" disabled={busy} onClick={onCancel}>{t('Отмена')}</button>
      </div>
    </div>
  )
}

// Прогресс и итог раскатки агента прямо в блоке: сколько обновилось, где застряло и почему
export function RolloutProgress({ run, servers, onClose, onOpen }: {
  run: Run; servers: Server[]; onClose: () => void; onOpen?: (id: number) => void
}) {
  const { t } = useI18n()
  // helper'ы: из хостов запуска те, где прогон ansible все еще что-то поменяет
  const pending = new Set(rolloutHosts(servers))
  const helperLeft = run.hosts.length ? run.hosts.filter((h) => pending.has(h)) : null
  const rows = run.ids
    .map((id) => servers.find((s) => s.id === id))
    .filter((s): s is Server => !!s)
    .map((s) => nodeState(s, run, t))
  const done = rows.filter((r) => r.st === 'done').length
  const open = rows.filter((r) => r.st !== 'done')
  const over = open.every((r) => r.st === 'bad')
  const issues = open.filter((r) => r.why)
  return (
    <div className="upd-run">
      <div className="upd-run-head">
        <span className={`lk-dot ${over && issues.length ? 'lk-dot-warn' : over ? 'lk-dot-ok' : 'lk-dot-info'}`} />
        <span className="upd-run-txt">
          {rows.length === 0
            ? t('Обновление helper\'ов')
            : open.length === 0
            ? t('Готово: агент {v} на всех нодах ({n})', { v: run.v, n: rows.length })
            : over
              ? t('Агент {v}: обновились {done} из {n}, остальные не обновятся сами', { v: run.v, done, n: rows.length })
              : t('Агент {v}: обновились {done} из {n}', { v: run.v, done, n: rows.length })}
        </span>
        <button className="ghost small" onClick={onClose}>{t('скрыть')}</button>
      </div>
      {issues.length > 0 && (
        <div className="upd-run-list">
          {issues.map((r) => (
            <div key={r.s.id} className={`upd-run-row${r.st === 'bad' ? ' bad' : ''}`}>
              {onOpen ? (
                <button className="linklike" onClick={() => onOpen(r.s.id)}><b>{r.s.name}</b></button>
              ) : <b>{r.s.name}</b>}
              <span className="muted"> - {r.why}</span>
            </div>
          ))}
        </div>
      )}
      {helperLeft != null && (
        <div className="upd-run-row muted small">
          {helperLeft.length === 0
            ? t('helper\'ы: везде актуальные')
            : t('helper\'ы: ждут прогона ansible на нодах - {n} ({list})', { n: helperLeft.length, list: shortList(helperLeft, t, 6) })}
        </div>
      )}
    </div>
  )
}

// Команда ansible для helper'ов. С доступом ansible одна команда на все панели (плагин
// инвентаря сам спрашивает панели), иначе - список хостов этой панели
export function ansibleCmd(hosts: string[], viaPanels: boolean): string {
  return viaPanels
    ? 'ansible-playbook playbooks/kervax_helpers.yml -l kervax_outdated'
    : `ansible-playbook playbooks/kervax_helpers.yml -l '${hosts.join(',')}'`
}

export function useViaPanels(): boolean {
  const { isAdmin } = useAuth()
  const [via, setVia] = useState(false)
  useEffect(() => {
    if (!isAdmin) return
    getAnsibleAccess().then((a) => setVia(a.enabled)).catch(() => setVia(false))
  }, [isAdmin])
  return via
}

export function AnsibleCmd({ hosts, via }: { hosts: string[]; via: boolean }) {
  const { t } = useI18n()
  const { isAdmin } = useAuth()
  const [copied, setCopied] = useState(false)
  const cmd = ansibleCmd(hosts, via)
  return (
    <div className="ansible-hint">
      <div className="agent-advice-cmd">
        <pre>{cmd}</pre>
        <button className="ghost" onClick={() => {
          navigator.clipboard?.writeText(cmd)
          setCopied(true); window.setTimeout(() => setCopied(false), 1500)
        }}>{copied ? t('Скопировано') : t('Копировать')}</button>
      </div>
      <div className="muted small">
        {t('Запускать из своего репозитория ansible. Сначала можно вхолостую: добавьте --check --diff. Панель ничего не выполняет — только называет хосты.')}
      </div>
      <div className="muted small">
        {via
          ? t('Одна команда на все панели: группу kervax_outdated плагин инвентаря собирает, спрашивая сами панели.')
          : isAdmin && t('Чтобы на всех панелях была одна команда без списка хостов, включите доступ для ansible: меню настроек -> Ansible.')}
      </div>
    </div>
  )
}
