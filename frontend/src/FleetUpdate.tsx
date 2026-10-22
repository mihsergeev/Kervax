import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError, agentUpdate, agentUpdateCancel, closeFleetRollout, getAnsibleAccess, getFleetRollout, startHelperRollout,
  type FleetRollout, type Server,
} from './api'
import { useAuth } from './auth'
import { useI18n } from './i18n'
import { fmtSetupVersion, shortList } from './serverUtils'

// Обновление парка из блока "Обновления" главной и баннера "Серверов": агент - OTA через
// панель (/api/servers/agent-update), helper'ы - командой ansible. Панель helper'ы сама не ставит:
// это root-скрипты, и раздача их панелью значила бы, что взлом панели дает root на всем парке
// (см. kervax_helpers.yml). Кнопка в строке: первый клик - та же кнопка спрашивает "Точно?",
// второй клик в ту же точку запускает; прогресс в строке, по успеху строка уходит сама.
// Ход раскатки хранится на панели (fleet_rollout.py): переживает перезагрузку страницы.

type T = (k: string, p?: Record<string, string | number>) => string

// за столько нода без движения считается застрявшей: агент спрашивает панель каждые
// полминуты, бинарь качается за минуту, на РФ-нодах за DPI докачка видна в agent_update_note
const STUCK_MS = 15 * 60 * 1000
// прогон ansible руками: за полчаса его или не запускали, или он упал на части хостов
const HELPER_STUCK_MS = 30 * 60 * 1000
// "Точно?" ждет второго клика столько, потом кнопка возвращается
const CONFIRM_MS = 4000
// зеленая строка "готово" висит столько и уходит сама
const DONE_MS = 3000

// агент отстает от подписанной версии; выключенным сервером панель цель не ставит
export function agentBehind(servers: Server[], avail: string): Server[] {
  return servers.filter((s) => s.enabled && s.agent_version && avail && s.agent_version !== avail)
}

export type HelperGroup = { key: string; name: string; fresh: boolean; a: string; b: string; nodes: Server[] }

// helper -> ноды, где его ставить или обновлять (по одной группе на helper и пару версий)
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

export const hostOf = (s: Server) => s.hostname || s.name

// хосты, где прогон kervax_helpers.yml что-то поменяет (helper или настройка юнита агента)
export function rolloutHosts(servers: Server[]): string[] {
  return [...new Set(servers.filter((s) => s.online && s.helper_rollout).map(hostOf))].sort()
}

// "обновить 1 ноду, 2 ноды, 5 нод" - кнопка с числом читается без "нод - 12"
function nodesAcc(n: number, t: T): string {
  const d = n % 10, dd = n % 100
  if (d === 1 && dd !== 11) return t('ноду')
  if (d >= 2 && d <= 4 && (dd < 12 || dd > 14)) return t('ноды')
  return t('нод')
}

// "на 1 ноде, на 4 нодах"
const nodesLoc = (n: number, t: T) => (n % 10 === 1 && n % 100 !== 11 ? t('ноде') : t('нодах'))

// Ход раскатки: читается с панели вместе с серверами, запуск и закрытие - тоже через панель.
// canWrite=false (просмотр) - строки видно, закрыть "готово" можно только у себя на странице
export function useFleetRollout(
  servers: Server[], avail: string, onServers: (s: Server[]) => void, onUnauthorized: () => void, canWrite: boolean,
) {
  const { t } = useI18n()
  const [data, setData] = useState<FleetRollout | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  // закрытое у себя: у просмотра на панель не пишем, а строке "готово" все равно пора уйти
  const [hidden, setHidden] = useState<string[]>([])
  // минутный тик: "застряла" наступает по времени, а не по новым данным
  const [, setTick] = useState(0)

  const fail = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) onUnauthorized()
    else setErr(e instanceof Error ? e.message : t('Ошибка'))
  }, [onUnauthorized, t])

  // страница перечитывает серверы по таймеру - вместе с ними и ход раскатки
  const unauth = useRef(onUnauthorized)
  useEffect(() => { unauth.current = onUnauthorized }, [onUnauthorized])
  useEffect(() => {
    let live = true
    getFleetRollout()
      .then((d) => { if (live) setData(d) })
      .catch((e) => { if (e instanceof ApiError && e.status === 401) unauth.current() })
    return () => { live = false }
  }, [servers])

  const running = !!(data?.agent || data?.helpers)
  useEffect(() => {
    if (!running) return
    const id = window.setInterval(() => setTick((x) => x + 1), 60000)
    return () => window.clearInterval(id)
  }, [running])

  const call = useCallback(async (f: () => Promise<void>) => {
    setBusy(true)
    setErr('')
    try {
      await f()
      return true
    } catch (e) {
      fail(e)
      return false
    } finally {
      setBusy(false)
    }
  }, [fail])

  // ответ - только затронутые серверы: подменяем их в общем списке
  const merge = useCallback((upd: Server[]) => {
    const by = new Map(upd.map((s) => [s.id, s]))
    onServers(servers.map((s) => by.get(s.id) ?? s))
  }, [servers, onServers])

  const startAgent = useCallback((ids: number[]) => call(async () => {
    if (!ids.length || !avail) return
    merge(await agentUpdate(avail, ids, true))
    setData(await getFleetRollout())
  }), [call, merge, avail])

  // "стоп": цель снимается с еще не обновившихся, строка возвращается к "отстают"
  const stopAgent = useCallback((ids: number[]) => call(async () => {
    if (ids.length) merge(await agentUpdateCancel(ids, true))
    setData(await closeFleetRollout('agent'))
  }), [call, merge])

  const startHelpers = useCallback((hosts: string[]) => call(async () => {
    if (hosts.length) setData(await startHelperRollout(hosts))
  }), [call])

  const close = useCallback((part: 'agent' | 'helpers') => {
    const p = data?.[part]
    if (!p) return
    setHidden((h) => [...h, `${part}|${p.at}`])
    if (canWrite) closeFleetRollout(part).then(setData).catch(() => {})
  }, [data, canWrite])

  const agent = data?.agent && data.agent.v === avail && !hidden.includes(`agent|${data.agent.at}`) ? data.agent : null
  const helpers = data?.helpers && !hidden.includes(`helpers|${data.helpers.at}`) ? data.helpers : null
  return { agent, helpers, busy, err, startAgent, stopAgent, startHelpers, close }
}

export type Fleet = ReturnType<typeof useFleetRollout>

// Кнопка с подтверждением на месте: первый клик - она же "Точно? ...", второй клик в ту же
// точку - запуск; через 4 секунды без второго клика возвращается как была
export function ConfirmButton({ label, sure, onGo, disabled, big }: {
  label: string; sure: string; onGo: () => void; disabled?: boolean; big?: boolean
}) {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return
    const id = window.setTimeout(() => setArmed(false), CONFIRM_MS)
    return () => window.clearTimeout(id)
  }, [armed])
  return (
    <button
      className={`upd-go${big ? '' : ' small'}${armed ? ' upd-sure' : big ? '' : ' ghost'}`}
      disabled={disabled}
      onClick={() => {
        if (!armed) return setArmed(true)
        setArmed(false)
        onGo()
      }}
    >
      {armed ? sure : label}
      {armed && <span className="upd-sure-tm" style={{ animationDuration: `${CONFIRM_MS}ms` }} />}
    </button>
  )
}

const Bar = ({ n, of }: { n: number; of: number }) => (
  <span className="upd-bar"><i style={{ width: `${of ? Math.round((n / of) * 100) : 0}%` }} /></span>
)

type NodeState = { s: Server; st: 'done' | 'wait' | 'note' | 'bad'; why: string }

// Где нода сейчас и что делать, если не обновилась: конкретно по ноде, без общих слов
function nodeState(s: Server, v: string, at: number, t: T): NodeState {
  if (s.agent_version === v) return { s, st: 'done', why: '' }
  if (s.agent_update_error)
    return { s, st: 'bad', why: t('агент отклонил обновление: {err}', { err: s.agent_update_error }) }
  if (!s.enabled) return { s, st: 'bad', why: t('сервер выключен в панели - обновление ему не ставится') }
  if (s.target_agent_version !== v) return { s, st: 'bad', why: t('раскатку на этой ноде отменили') }
  if (!s.online) return { s, st: 'note', why: t('не на связи - обновится сам, когда агент вернется') }
  if (s.agent_update_note) return { s, st: 'note', why: s.agent_update_note }
  if (Date.now() - at > STUCK_MS)
    return { s, st: 'bad', why: t('за 15 минут не обновился - на ноде: journalctl -u kervax-agent -n 50; если агент очень старый и сам не обновляется, переустановите его командой из карточки') }
  return { s, st: 'wait', why: '' }
}

function NodeIssues({ rows, onOpenNode }: { rows: NodeState[]; onOpenNode?: (id: number) => void }) {
  if (!rows.length) return null
  return (
    <div className="upd-issues">
      {rows.map((r) => (
        <div key={r.s.id} className={`upd-issue${r.st === 'bad' ? ' bad' : ''}`}>
          {onOpenNode ? (
            <button className="linklike" onClick={() => onOpenNode(r.s.id)}><b>{r.s.name}</b></button>
          ) : <b>{r.s.name}</b>}
          <span className="muted"> - {r.why}</span>
        </div>
      ))}
    </div>
  )
}

const OpenBtn = ({ onOpen }: { onOpen?: () => void }) => {
  const { t } = useI18n()
  return onOpen ? <button className="ghost small upd-open" title={t('Открыть')} onClick={onOpen}>→</button> : null
}

// Строка агента: отстают -> "Точно?" -> обновились N из M -> готово (уходит сама) или
// по нодам, что не так, с "повторить" и "скрыть". canary - кнопка "сначала одна нода" (баннер "Серверов")
export function AgentUpdRow({ servers, avail, fleet, canUpd, canary, onOpen, onOpenNode }: {
  servers: Server[]; avail: string; fleet: Fleet; canUpd: boolean; canary?: boolean
  onOpen?: () => void; onOpenNode?: (id: number) => void
}) {
  const { t } = useI18n()
  const run = fleet.agent
  const at = run ? Date.parse(run.at) : 0
  const rows = run
    ? run.ids.map((id) => servers.find((s) => s.id === id)).filter((s): s is Server => !!s)
      .map((s) => nodeState(s, run.v, at, t))
    : []
  const open = rows.filter((r) => r.st !== 'done')
  const finished = rows.length > 0 && open.length === 0
  const { close } = fleet
  useEffect(() => {
    if (!finished) return
    const id = window.setTimeout(() => close('agent'), DONE_MS)
    return () => window.clearTimeout(id)
  }, [finished, close])

  if (!rows.length) {
    // цель ставим только нодам на связи: не на связи в ход раскатки не идут, иначе строка
    // висела бы "обновились 11 из 12" до их возвращения; вернутся - встанут сюда же отстающими
    const on = agentBehind(servers, avail).filter((s) => s.online)
    if (!on.length) return null
    const from = [...new Set(on.map((s) => s.agent_version))].sort().join(', ')
    const notes = on.filter((s) => s.agent_update_note)
    const n = on.length
    return (
      <div className="home-attention-row upd-row">
        <span className="lk-dot lk-dot-info" />
        <span className="home-attention-ico">⬆</span>
        <span className="home-attention-txt">
          {t('Агент {v}: отстают нод - {n}', { v: avail, n })}{' '}
          <span className="muted small">({from}: {shortList(on.map((s) => s.name), t, 3)})</span>
          {/* медленный канал (РФ-ноды за DPI): цель уже стоит, идет докачка */}
          {notes.length > 0 && (
            <span className="muted small upd-notes">{notes.map((s) => `${s.name}: ${s.agent_update_note}`).join(' · ')}</span>
          )}
        </span>
        {canUpd && canary && n > 1 && (
          <button className="ghost small" disabled={fleet.busy} onClick={() => fleet.startAgent([on[0].id])}
            title={t('Обновить сначала одну ноду (canary), потом остальные')}>
            {t('canary - 1 нода')}
          </button>
        )}
        {canUpd && (
          <ConfirmButton disabled={fleet.busy}
            label={t('Обновить {n} {nodes}', { n, nodes: nodesAcc(n, t) })}
            sure={t('Точно? {n} {nodes}', { n, nodes: nodesAcc(n, t) })}
            onGo={() => fleet.startAgent(on.map((s) => s.id))} />
        )}
        <OpenBtn onOpen={onOpen} />
      </div>
    )
  }

  const done = rows.length - open.length
  if (finished) {
    return (
      <div className="home-attention-row upd-row upd-ok">
        <span className="lk-dot lk-dot-ok" />
        <span className="home-attention-ico">✓</span>
        <span className="home-attention-txt">
          {t('Агент {v} на всех нодах: {n}', { v: run!.v, n: rows.length })}
          <span className="upd-fade" style={{ animationDuration: `${DONE_MS}ms` }} />
        </span>
      </div>
    )
  }
  const bad = open.filter((r) => r.st === 'bad')
  // все, что осталось, само уже не обновится: строка ждет решения человека
  if (open.every((r) => r.st === 'bad')) {
    return (
      <div className="home-attention-row upd-row upd-fail">
        <span className="lk-dot lk-dot-warn" />
        <span className="home-attention-ico">⬆</span>
        <span className="home-attention-txt">
          {t('Агент {v}: обновились {done} из {n}, остальные сами не обновятся', { v: run!.v, done, n: rows.length })}
          <NodeIssues rows={bad} onOpenNode={onOpenNode} />
        </span>
        {canUpd && (
          <button className="ghost small" disabled={fleet.busy}
            onClick={() => fleet.startAgent(bad.filter((r) => r.s.enabled).map((r) => r.s.id))}>
            {t('Повторить: {n}', { n: bad.length })}
          </button>
        )}
        <button className="ghost small" onClick={() => close('agent')}>{t('скрыть')}</button>
        <OpenBtn onOpen={onOpen} />
      </div>
    )
  }
  const going = open.filter((r) => r.st === 'wait').map((r) => r.s.name)
  return (
    <div className="home-attention-row upd-row">
      <span className="lk-dot lk-dot-info" />
      <span className="home-attention-ico">⬆</span>
      <span className="home-attention-txt">
        {t('Агент {v}: обновились {done} из {n}', { v: run!.v, done, n: rows.length })}
        <Bar n={done} of={rows.length} />
        {going.length > 0 && <span className="muted small upd-notes">{t('качают: {list}', { list: shortList(going, t, 3) })}</span>}
        <NodeIssues rows={open.filter((r) => r.why)} onOpenNode={onOpenNode} />
      </span>
      {canUpd && (
        <button className="ghost small" disabled={fleet.busy} title={t('Снять обновление с тех, кто еще не обновился')}
          onClick={() => fleet.stopAgent(open.map((r) => r.s.id))}>
          {t('стоп')}
        </button>
      )}
      <OpenBtn onOpen={onOpen} />
    </div>
  )
}

// Команда ansible для helper'ов. С доступом ansible одна команда на все панели (плагин
// инвентаря сам спрашивает панели), иначе - список хостов этой панели. Нод немного - по
// именам и с доступом: имя в команде видно сразу, и не нужно гадать, попала ли нода в группу
const BY_NAME_MAX = 3
export function ansibleCmd(hosts: string[], viaPanels: boolean): string {
  return viaPanels && (hosts.length === 0 || hosts.length > BY_NAME_MAX)
    ? 'ansible-playbook playbooks/kervax_helpers.yml -l kervax_outdated'
    : helperCmd(hosts)
}

// Та же команда по именам нод: работает и без доступа ansible, и когда группа еще не собралась
export function helperCmd(hosts: string[]): string {
  const l = hosts.length === 1 ? hosts[0] : `'${hosts.join(',')}'`
  return `ansible-playbook playbooks/kervax_helpers.yml -l ${l}`
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

const copy = (s: string) => {
  try { navigator.clipboard?.writeText(s)?.catch(() => {}) } catch { /* без буфера команда видна в строке */ }
}

// Запуск helper'ов: копируем команду и ждем прогона (одна и та же команда в строке и в "Обновить все")
export function startHelpers(fleet: Fleet, hosts: string[], via: boolean) {
  copy(ansibleCmd(hosts, via))
  return fleet.startHelpers(hosts)
}

// Строка helper'ов: одна на все helper'ы, потому что их чинит одна команда ansible.
// Скопировали команду -> ждем, пока хосты перестанут требовать прогона -> готово (уходит сама)
export function HelperUpdRow({ servers, fleet, canUpd, via, onOpen }: {
  servers: Server[]; fleet: Fleet; canUpd: boolean; via: boolean; onOpen?: () => void
}) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const pending = rolloutHosts(servers)
  const run = fleet.helpers
  const left = run ? run.hosts.filter((h) => pending.includes(h)) : []
  const finished = !!run && left.length === 0
  const { close } = fleet
  useEffect(() => {
    if (!finished) return
    const id = window.setTimeout(() => close('helpers'), DONE_MS)
    return () => window.clearTimeout(id)
  }, [finished, close])

  if (!run) {
    if (!pending.length) return null
    const n = pending.length
    // что именно поменяет прогон: helper'ы с версиями, плюс настройка юнита агента (watchdog)
    const what = helperGroups(servers).map((g) => g.fresh
      ? t('{helper} не стоит', { helper: g.name })
      : `${g.name} ${g.a} -> ${g.b}`)
    // настройка systemd-юнита агента (нет watchdog и т.п.) - тем же плейбуком; отказ
    // самообновления агента ansible не чинит, он отдельной строкой
    for (const s of servers) {
      if (!s.online || !pending.includes(hostOf(s))) continue
      for (const a of s.agent_advice || [])
        if (!(s.agent_update_error && a.endsWith(s.agent_update_error)) && !what.includes(a)) what.push(a)
    }
    return (
      <div className="home-attention-row upd-row">
        <span className="lk-dot lk-dot-info" />
        <span className="home-attention-ico">🧩</span>
        <span className="home-attention-txt">
          {t('helper\'ы: обновить на {n} {nodes}', { n, nodes: nodesLoc(n, t) })}{' '}
          <span className="muted small">({[what.join(', '), shortList(pending, t, 3)].filter(Boolean).join('; ')})</span>
          {canUpd && <code className="upd-cmd">{ansibleCmd(pending, via)}</code>}
        </span>
        {canUpd && (
          <button className="ghost small" disabled={fleet.busy} onClick={() => startHelpers(fleet, pending, via)}>
            {t('Скопировать команду')}
          </button>
        )}
        <OpenBtn onOpen={onOpen} />
      </div>
    )
  }

  const n = run.hosts.length
  const done = n - left.length
  if (finished) {
    return (
      <div className="home-attention-row upd-row upd-ok">
        <span className="lk-dot lk-dot-ok" />
        <span className="home-attention-ico">✓</span>
        <span className="home-attention-txt">
          {t('helper\'ы обновлены на всех нодах: {n}', { n })}
          <span className="upd-fade" style={{ animationDuration: `${DONE_MS}ms` }} />
        </span>
      </div>
    )
  }
  const cmd = ansibleCmd(run.hosts, via)
  const again = (
    <button className="ghost small" onClick={() => {
      copy(cmd)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    }}>{copied ? t('Скопировано') : t('копировать еще раз')}</button>
  )
  // за полчаса прогон или не запускали, или он не дошел до части хостов
  const stuck = Date.now() - Date.parse(run.at) > HELPER_STUCK_MS
  return (
    <div className={`home-attention-row upd-row${stuck ? ' upd-fail' : ''}`}>
      <span className={`lk-dot ${stuck ? 'lk-dot-warn' : 'lk-dot-info'}`} />
      <span className="home-attention-ico">🧩</span>
      <span className="home-attention-txt">
        {done === 0 && !stuck ? (
          t('helper\'ы: команда скопирована, запустите ее в репозитории ansible')
        ) : (
          <>
            {t('helper\'ы: обновились {done} из {n}', { done, n })}
            <Bar n={done} of={n} />
          </>
        )}
        {stuck && (
          <div className="upd-issues muted">
            {done === 0
              ? t('прогона так и не было, ждут: {list}', { list: shortList(left, t, 6) })
              : t('после прогона все еще старые: {list} - смотрите вывод плейбука', { list: shortList(left, t, 6) })}
          </div>
        )}
        {/* команда нужна, пока прогона нет; пошел - видно прогресс, копия - кнопкой */}
        {(done === 0 || stuck) && <code className="upd-cmd">{cmd}</code>}
      </span>
      {canUpd && again}
      {(stuck || !canUpd) && <button className="ghost small" onClick={() => close('helpers')}>{t('скрыть')}</button>}
      <OpenBtn onOpen={onOpen} />
    </div>
  )
}

// "Обновить все" в шапке блока: нужна, только когда обновить можно и агент, и helper'ы -
// с одной строкой хватает ее кнопки. Тот же второй клик в ту же точку
export function UpdateAllButton({ servers, avail, fleet, via }: {
  servers: Server[]; avail: string; fleet: Fleet; via: boolean
}) {
  const { t } = useI18n()
  if (fleet.agent || fleet.helpers) return null
  const on = agentBehind(servers, avail).filter((s) => s.online)
  const hosts = rolloutHosts(servers)
  if (!on.length || !hosts.length) return null
  return (
    <ConfirmButton big disabled={fleet.busy}
      label={t('Обновить все')}
      sure={t('Точно? агент на {a} + helper\'ы на {h}', { a: on.length, h: hosts.length })}
      onGo={async () => {
        // по очереди: обе части пишутся в одну запись на панели
        await startHelpers(fleet, hosts, via)
        await fleet.startAgent(on.map((s) => s.id))
      }} />
  )
}
