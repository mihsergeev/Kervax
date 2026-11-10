import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import {
  ApiError,
  checksOverview,
  getAgentRelease,
  alertCoverage,
  fixAlertCoverage,
  applyLocalProbe,
  listServers,
  localProbeSuggestions,
  muteServerKeys,
  retireListeningPort,
  snoozeServer,
  snoozeServerAlert,
  type Check,
  type LocalProbeSuggestion,
  type MuteWarning,
  type Server,
  dropCheckKnown,
  dropServerKnown,
  setServerKnown,
  type KnownIssue,
} from './api'
import type { Section } from './App'
import { backupAlive, backupDest, backupDestWhere, backupWindowNote, dbAuditNames, knownText, dockRiskText, knownActive, proxyBuilt, shortList, sinceShort, srvIssues, srvKnown, knownMeta, tallyTile, type TallyTile } from './serverUtils'
import { RecheckButton, RecheckSummary, recheckKey, recheckUnavailable, useAlertRechecks, type RecheckRef } from './AlertRecheck'
import { activateHomeRow } from './homeKeyboard'
import { KnownDialog } from './KnownDialog'
import { LEAK_MUTE, LISTEN_MUTE, RESTARTS_MUTE, OOM_MUTE, collectMutes, isDecision, kindLabel, liftMute, muteDate, muteLiveText, muteQuiet, oomCulpritLabel, withLive, type MuteItem } from './mutes'
import { MaintChip, MaintHome } from './MaintModal'
import { useI18n } from './i18n'
import { toastError } from './toast'
import { expiryText, registrableDomain } from './checkUtils'
import { ExpirySnoozePick, ExpirySnoozeState, expEntry, expHidden, expiredDomainMuted, expSnoozeTip } from './ExpirySnooze'
import { useAuth } from './auth'
import { IconBackups, IconDocker, IconKube, IconServers, IconSites } from './icons'
import { CountryFlag } from './CountryFlag'
import { ActGroup, QuickMute } from './ActGroup'
import { grpMuteTip } from './homeGroups'
import { HomeDomainSuggestions } from './HomeDomainSuggestions'
import { backupProblems, dockerProblems, type ProbItem } from './sectionIssues'
import { AgentUpdRow, HelperUpdRow, UpdateAllButton, agentBehind, rolloutHosts as fleetHosts, useFleetRollout } from './FleetUpdate'

const ATTENTION_LIMIT = 15
const QUEUE_LIMIT = 5

type QueueRow = {
  key: string
  area: TallyTile
  critical: boolean
  render: () => ReactNode
}

function useOverviewSource<T>(request: () => Promise<T>, onUnauthorized: () => void) {
  const [state, setState] = useState<{ data: T | null; updatedAt: number | null; failed: boolean }>({
    data: null, updatedAt: null, failed: false,
  })
  const sequence = useRef(0)
  const applied = useRef(0)
  const refresh = useCallback(() => {
    const id = ++sequence.current
    request().then((data) => {
      if (id <= applied.current) return
      applied.current = id
      setState({ data, updatedAt: Date.now(), failed: false })
    }).catch((error: unknown) => {
      if (id <= applied.current) return
      applied.current = id
      if (error instanceof ApiError && error.status === 401) onUnauthorized()
      else setState((prev) => ({ ...prev, failed: true }))
    })
  }, [request, onUnauthorized])
  const replace = useCallback((update: (prev: T) => T) => {
    applied.current = ++sequence.current
    setState((prev) => prev.data === null ? prev : { ...prev, data: update(prev.data) })
  }, [])
  return { ...state, refresh, replace }
}

function HomeDisclosure({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  return (
    <details className="home-disclosure">
      <summary><span>{title}</span><span className="net-n">{count}</span></summary>
      <div className="home-disclosure-body">{children}</div>
    </details>
  )
}
// иконка проблемы по ее виду - чтобы список читался, не вчитываясь
const ISSUE_ICON: Record<string, string> = {
  offline: '🔥', cpu: '🧮', mem: '🧠', disk: '🗄', temp: '🌡', throttle: '🥵', conntrack: '🔗',
  disktemp: '🌡', disk_health: '💽', disk_forecast: '📈', inode: '🗂', windows_service: '⚙️', units: '⚙️', oom: '🧠', cpu_spin: '🌀',
  docker: '🐳', kuber: '☸️', backups: '💾', backup_transfer: '💾', docker_sock: '🔓',
  clock: '🕐', dns: '🌐', db_conn: '🔌', web_5xx: '🌐', queue: '🐇', kube_expiry: '⏳', flux_down: '☸️',
  flux_stale: '☸️', backup_dump: '🛢', backup_dump_space: '🛢', backup_cron: '🛢', backup_check: '🩺',
  unit_loop: '🔁', kube_cron: '⏰', exposed: '🔓', port_new: '🚪', cf_realip: '🎭', pressure: '🫁', net_health: '📶', link: '🔗', gre_down: '🚇', traffic: '📊', kube_node: '☸️', kube_job: '💥', access_change: '🔑', backup_restore: '🛟', docker_restarts: '🔄', kube_restarts: '🔄', mem_leak: '📈', mem_limit: '🧠', ssh_risk: '🔐', db_health: '🛢', web_config: '🧾', web_cert: '🔏', listen_gone: '🔌', disk_slow: '🐢', gpu: '🎛', gpu_heat: '🌡', llm: '💬', qdrant: '🛢', queue_stuck: '⏳', queue_failed: '🧨', workers: '👷', cron_stuck: '⏲', log_errors: '📜', fpm: '🐘', gitlab: '🦊', autoban: '🚫', access_risk: '🗝',
  tamper: '🕵️', webfiles: '🕵️',
}

type T = (k: string, p?: Record<string, string | number>) => string
// cc — ISO-код страны ноды (бэкенд определяет по IP): в этих списках сервер
// представлен строкой, объекта под рукой нет, поэтому код кладём в сам пункт
type ActItem = {
  key: string; icon: string; section: Section; id?: number; text: string; srv?: boolean; sec?: string; cc?: string; name?: string
  // обновление по парку, которое кнопкой не чинится (кривой релиз, отказ самообновления):
  // блок "Обновления" рядом со строками агента и helper'ов (FleetUpdate.tsx)
  fleet?: boolean
  // вид проблемы: одинаковые на двух и больше серверах сворачиваются в одну строку со списком
  // серверов (раскрывается кликом); detail - что именно на этом сервере, без его имени
  grp?: string
  detail?: string
  // заглушка до нового (osupd:<вид>@ у ОС, home:<группа>@ у остальных, ключ дает бэкенд): галки в
  // раскрытой группе и 🔕 у строки; muted - уже заглушен, виден внизу группы "заглушено N, показать"
  mute?: string
  muted?: boolean
  tip?: string // всплывающая подсказка строки: полный список пакетов
}

// Снуз на время понимают не все ключи: рост памяти, перезапуски и пропавший порт бэкенд берет только
// из alert_mutes, решения "так и должно быть" - не мьют вовсе
const snoozable = (k: string) => !isDecision(k) && ![LEAK_MUTE, RESTARTS_MUTE, LISTEN_MUTE].some((p) => k.startsWith(p))

// ВЕРХНИЙ блок: только то, что требует РУЧНОГО действия на ноде — обновить агент или
// выполнить setup-скрипт (docker proxy / kube-setup / backupserver-setup). Клик ведёт
// СРАЗУ в нужный хост (там показана команда). Проблемы здоровья — в карточках ниже.
// Команда для массовой переустановки helper'ов. Панель сознательно НЕ умеет
// выкатывать их сама: helper ставится от root, и «кнопка в панели» означала бы, что
// взлом панели = root на всём парке. Поэтому здесь только список хостов, а выкатывает
// человек своим ansible — панель к нодам с root-правами не ходит.
// Сайт снаружи закрыт белым списком (бэкенд app/allowlist_hint.py): режут соединение или
// отвечают 403 со всех точек часами, а изнутри его ноды он открывается или снаружи не
// открывался ни разу. Строка говорит, почему так решили, кнопка - что сделать. Включать
// молча нельзя: проверка изнутри отвечает на другой вопрос, чем внешняя.
function hintWhy(x: LocalProbeSuggestion, t: T): string {
  const out =
    x.outside === 'code'
      ? t('снаружи отвечает {code}', { code: x.code })
      : x.outside === 'reset'
        ? t('снаружи сбрасывает соединение')
        : t('снаружи не подключается')
  if (x.reason === 'inside_up') {
    const d = sinceShort(x.since, t)
    return t('{out}{d}, изнутри {srv} открывается', { out, d: d ? ' ' + t('уже {d}', { d }) : '', srv: x.server_name })
  }
  if (x.reason === 'never_up' && x.server_name)
    return t('{out} и ни разу не открылся, домен на {srv}', { out, srv: x.server_name })
  return t('{out} и ни разу не открылся', { out })
}

function LocalProbeHint({
  items,
  onDone,
}: {
  items: LocalProbeSuggestion[]
  onDone: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const apply = async (ids: number[]) => {
    setBusy(true)
    try {
      await applyLocalProbe(ids)
      onDone()
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="local-probe-hint">
      <ul className="local-probe-list">
        {items.map((x) => (
          <li key={x.check_id} title={x.message}>
            <span className="local-probe-txt">
              <span className="mono">{x.host}</span> <span className="muted small">{hintWhy(x, t)}</span>
            </span>
            <button className="ghost small" disabled={busy} onClick={() => apply([x.check_id])}>
              {x.kind === 'code' ? t('считать {code} нормой', { code: x.code }) : t('проверять изнутри')}
            </button>
          </li>
        ))}
      </ul>
      {items.length > 1 && (
        <button className="ghost" disabled={busy} onClick={() => apply(items.map((x) => x.check_id))}>
          {t('применить ко всем {n}', { n: items.length })}
        </button>
      )}
    </div>
  )
}

// Почему объект молчит — словами. Бэкенд отдаёт код, а не готовую фразу: текст живёт
// в словаре интерфейса и переводится вместе со всем остальным.
function muteReason(m: MuteWarning, t: T): string {
  if (m.reason_code === 'no_rules') return t('не включён ни один тип алертов')
  if (m.reason_code === 'no_group')
    return t('объект без группы, а области алертов заданы по группам')
  return t('группа «{g}» не входит ни в одну область алертов', { g: m.group })
}

// Немые объекты. Раньше строка просто вела в карточку объекта — а лечится это в
// другом месте (настройки → тип алерта → область действия) и в каждом типе отдельно.
// Поэтому чинить предлагаем прямо здесь: кнопка дописывает объект в область всех
// включённых правил.
function MuteHint({
  items,
  onOpen,
  onDone,
}: {
  items: MuteWarning[]
  onOpen: (s: Section, id?: number) => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const fixable = items.filter((x) => x.fixable)
  const fix = async (list: MuteWarning[]) => {
    setBusy(true)
    setErr('')
    try {
      await fixAlertCoverage(list.map((x) => ({ kind: x.kind, id: x.id })))
      onDone()
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="chart-card net-card net-card-wide home-block home-attention-mute">
      <div className="net-head">
        <div className="chart-cap net-cap">
          {t('Алерты не придут')}
          <span className="net-n net-n-warn">{items.length}</span>
        </div>
      </div>
      <div className="muted small home-attention-note">
        {t('По этим объектам не сработает ни один алерт: они не попадают ни в одну область действия правил. Метрики собираются и графики рисуются — но когда что-то сломается, не придёт ничего.')}
        {fixable.length > 0 &&
          ' ' +
            t('Кнопка дописывает объект в область всех включённых правил: уведомления по нему пойдут туда же, куда по остальным.')}
      </div>
      <div className="home-attention-list">
        {items.slice(0, ATTENTION_LIMIT).map((m) => (
          <div key={`${m.kind}-${m.id}`} className="home-attention-row">
            <span className="lk-dot lk-dot-warn" />
            <span className="home-attention-ico">{m.kind === 'server' ? '🖥' : '🌐'}</span>
            <span className="home-attention-txt">
              {m.name} — {muteReason(m, t)}
            </span>
            {m.fixable ? (
              <button className="ghost small" disabled={busy} onClick={() => fix([m])}>
                {t('включить алерты')}
              </button>
            ) : (
              <span className="muted small home-attention-todo">
                {m.reason_code === 'no_rules'
                  ? t('включите типы алертов: ⚙ → Алерты')
                  : t('задайте группу, которая уже в области')}
              </span>
            )}
            <button
              className="ghost small"
              title={t('Открыть')}
              disabled={busy}
              onClick={() => onOpen(m.kind === 'server' ? 'servers' : 'sites', m.id)}
            >
              →
            </button>
          </div>
        ))}
        {items.length > ATTENTION_LIMIT && (
          <div className="muted small home-attention-more">
            {t('…и ещё {n}', { n: items.length - ATTENTION_LIMIT })}
          </div>
        )}
      </div>
      {fixable.length > 1 && (
        <button className="ghost" disabled={busy} onClick={() => fix(fixable)}>
          {t('включить алерты для всех {n}', { n: fixable.length })}
        </button>
      )}
      {err && <div className="muted small">{err}</div>}
    </div>
  )
}

// "Что сломано": текст проблемы - шапка и подробности ("ошибки 5xx: 18% запросов",
// "старые снапшоты не вычищаются - repo-a (256 дн.)"). Одинаковая шапка у предупреждений с
// разных серверов - одна строка с раскрытием; красное не сворачивается никогда.
function splitHead(text: string): [string, string] {
  const m = /^(.+?)(?: - |: )([\s\S]+)$/.exec(text)
  return m ? [m[1], m[2]] : [text, '']
}

// Строка блока "Требует действий": пункт как есть или группа одинаковых с разных серверов.
// Заглушенные до нового - внизу своей группы; одна живая строка с заглушенными рядом - тоже группа,
// иначе заглушенные было бы не вернуть с главной. Все заглушены - группы нет: пустая главная - норма
type ActRow = { one: ActItem } | { grp: string; items: ActItem[]; muted: ActItem[] }

function groupActs(items: ActItem[]): ActRow[] {
  const by = new Map<string, ActItem[]>()
  for (const it of items) if (it.grp) by.set(it.grp, [...(by.get(it.grp) ?? []), it])
  const rows: ActRow[] = []
  const done = new Set<string>()
  // место группы - по первому пункту, заглушенному тоже: после заглушки группа не прыгает по списку
  for (const it of items) {
    const list = it.grp ? by.get(it.grp)! : null
    const live = list ? list.filter((x) => !x.muted) : []
    if (!list || list.length < 2) {
      if (!it.muted) rows.push({ one: it })
    } else if (!done.has(it.grp!) && live.length) {
      done.add(it.grp!)
      rows.push({ grp: it.grp!, items: live, muted: list.filter((x) => x.muted) })
    }
  }
  const bad = (r: ActRow) => ('one' in r ? r.one.icon === '⛔' : r.items.some((x) => x.icon === '⛔'))
  return rows.sort((a, b) => Number(bad(b)) - Number(bad(a)))
}

function actionItems(servers: Server[], relProblem: string, t: T): ActItem[] {
  const items: ActItem[] = []
  // ключ заглушки пункта группы и заглушен ли он (backend app/home_mute.py)
  const hm = (s: Server, grp: string) => {
    const mute = s.home_mutes?.[grp]
    return { grp, mute, muted: !!mute && (s.alert_mutes ?? []).includes(mute) }
  }
  // Релиз агента собран криво (раздаётся не тот бинарь, что подписан) — обновлять
  // нечем, и без этой строки поломка молчит: агенты просто вечно отвергают обновление.
  if (relProblem) {
    items.push({ key: 'agent-release', icon: '⛔', section: 'servers', text: relProblem, fleet: true })
  }
  // отстающий агент, helper'ы и настройка юнита агента (watchdog и т.п., раскатывается тем же
  // плейбуком) - своими строками с кнопками (AgentUpdRow, HelperUpdRow). Здесь остается только
  // то, что кнопкой не чинится: отказ самообновления агента. Агрегируем ПО ТЕКСТУ, а не по
  // нодам - иначе одна и та же беда размножилась бы на весь парк.
  const advHosts = new Map<string, string[]>()
  for (const s of servers) {
    if (!s.online) continue
    for (const title of s.agent_advice || []) advHosts.set(title, [...(advHosts.get(title) ?? []), s.hostname || s.name])
  }
  for (const [title, hosts] of advHosts) {
    if (!servers.some((s) => s.agent_update_error && title.endsWith(s.agent_update_error))) continue
    items.push({ key: `adv-${title}`, icon: '🩺', section: 'servers', fleet: true,
      text: t('{what}: нод без настройки — {n}', { what: title, n: hosts.length }) })
  }
  for (const s of servers) {
    if (!s.online) continue
    const rep = s.last_report
    // Только что поставленная нода: установщик еще поднимает прокси Docker и ставит helper'ы
    // (их бэкенд в эти минуты и не требует). Без паузы плашки мигали и пропадали сами.
    if (rep?.docker?.present && !rep.docker.access && !s.fresh) {
      items.push({ key: `d-acc-${s.id}`, icon: '🐳', section: 'docker', id: s.id, name: s.name, cc: s.country, ...hm(s, 'd-acc'),
        text: t('{name}: Docker без доступа — включите read-only proxy', { name: s.name }) })
    }
    if (rep?.kube?.present && !rep.kube.access && !s.fresh) {
      items.push({ key: `k-acc-${s.id}`, icon: '☸', section: 'kuber', id: s.id, name: s.name, cc: s.country, ...hm(s, 'k-acc'),
        text: t('{name}: Kubernetes без доступа — запустите kube-setup', { name: s.name }) })
    }
    // Внешний прокси на старом образе: смотрит в интернет, а за год в caddy и traefik закрывают
    // не одну дыру. Ведем в Docker ноды: там написано, на что и как обновить.
    // старый образ, который решили оставить как есть ("оставить как есть" в карточке Docker), - не дело
    const old = (s.proxy_outdated ?? []).filter((p) => !p.muted)
    if (old.length > 0) {
      const list = old.map((p) => `${p.name} (${proxyBuilt(p)})`).join(', ')
      items.push({ key: `d-old-${s.id}`, icon: '🧓', section: 'docker', id: s.id, name: s.name, cc: s.country, sec: 'proxy',
        ...hm(s, 'd-old'), detail: list,
        text: t('{name}: устарел образ внешнего прокси {list} - обновите', { name: s.name, list }) })
    }
    // Ноды кластера без агента: на них не видно ни дисков, ни SMART, ни упавших юнитов, ни
    // процессов (на k8s-a-prc так неделями жгли 45 ядер зависшие chrome). Ведем во
    // вкладку нод: там кнопка добавить ноду в панель или отметить, что агент не нужен.
    const unmon = rep?.kube?.access ? (s.kube_unmonitored ?? []) : []
    if (unmon.length > 0) {
      const names = unmon.slice(0, 4).map((n) => n.name).join(', ') + (unmon.length > 4 ? ', ...' : '')
      items.push({ key: `k-noagent-${s.id}`, icon: '☸', section: 'kuber', id: s.id, name: s.name, cc: s.country, sec: 'nodes',
        ...hm(s, 'k-noagent'), detail: `${unmon.length} (${names})`,
        text: t('{name}: нод кластера без агента - {n} ({nodes})', { name: s.name, n: unmon.length, nodes: names }) })
    }
    // restic самого сервера бэкапов (prune, forget, check) старше той версии, до которой его
    // обновляет helper: обычно обновление не скачалось. Ведем в модалку сервера, там команда.
    if (s.bsrv_restic_old) {
      items.push({ key: `bs-restic-${s.id}`, icon: '🧓', section: 'backups', id: s.id, name: s.name, cc: s.country, srv: true, sec: 'server',
        ...hm(s, 'bs-restic'), detail: `restic ${s.bsrv_restic_old}`,
        text: t('{name}: на сервере бэкапов старый restic {v} - обновите', { name: s.name, v: s.bsrv_restic_old }) })
    }
    // Соединение до панели режут по объему (замеры агента 2.56, backend app/gre.need): отчеты
    // проходят, а обновления агента и большие ответы - нет. Ведем в карточку: там кнопка включить GRE
    if (s.gre_need && !s.gre_enabled) {
      items.push({ key: `gre-need-${s.id}`, icon: '🚇', section: 'servers', id: s.id, name: s.name, cc: s.country,
        ...hm(s, 'gre-need'),
        text: t('{name}: соединение до панели режут после ~16кб - включите GRE до панели', { name: s.name }) })
    }
    // бэкап-сервер найден (по docker), но статистики репо нет → нужен backupserver-setup
    const bs = rep?.backup_server
    if (bs?.present && (!bs.repos || bs.repos.length === 0)) {
      items.push({ key: `bs-setup-${s.id}`, icon: '🗄', section: 'backups', id: s.id, name: s.name, cc: s.country, srv: true, sec: 'repos',
        ...hm(s, 'bs-setup'),
        text: t('{name}: бэкап-сервер — включите статистику (backupserver-setup)', { name: s.name }) })
    }
    // setup-скрипты (helper'ы) устарели → переустановить. Версии сверяет бэкенд (helper_advice);
    // ведём на ДЕТАЛЬ сервера — там показана команда переустановки. В Кубер/Бэкапы вести нельзя:
    // там команды нет (фича уже работает), открывался бы обычный экран без действия.
    // СУБД, которую надо бэкапить отдельным дампом. Пункт снимается галкой «дампы
    // настроены» в «Покрытии» — иначе он висел бы вечно и превратился бы в обои.
    // приглушённые точечно («этот RabbitMQ бэкапить не нужно») на главную не выносим
    const dbs = (s.backup_audit || []).filter((x) => x.kind === 'db' && !x.muted)
    if (dbs.length > 0 && !s.db_dumps_ok) {
      // всё про бэкап живёт в разделе «Бэкапы»; там модалка открывается для ЛЮБОЙ ноды —
      // клиента, бэкап-сервера или вообще без бэкапа (иначе пункт вёл бы в пустоту)
      const db = dbAuditNames(dbs).join(', ')
      items.push({ key: `dbdump-${s.id}`, icon: '🛢', section: 'backups', id: s.id, name: s.name, cc: s.country, sec: 'coverage',
        ...hm(s, 'dbdump'), detail: db, text: t('{name}: {db} — нужен отдельный дамп', { name: s.name, db }) })
    }
    // Бэкап по http через интернет (app/backup_target.py): логин и пароль репозитория летят открытым
    // текстом. Таких серверов много, поэтому без алерта, одним пунктом на всех; как перевести на https и
    // решение "оставить http" - в окне бэкапа, раздел "Расписание и пути"
    const bk = s.backup_target
    if (bk?.insecure && !bk.muted) {
      const d = backupDest(s, servers)
      const where = backupDestWhere(bk)
      const dest = d?.srv ? `${d.srv.name} (${where})` : where
      items.push({ key: `bk-http-${s.id}`, icon: '🔓', section: 'backups', id: s.id, name: s.name, cc: s.country, sec: 'manage',
        ...hm(s, 'bk-http'), detail: dest,
        text: t('{name}: бэкап идет по http без шифрования на {dest} - переведите на https', { name: s.name, dest }) })
    }
    // ОС: конец поддержки, перезагрузка, обновления безопасности - по пункту на вид совета:
    // десяток нод с отложенной перезагрузкой сворачивается в одну строку "нужна перезагрузка".
    // Принятое в карточке ("обновить ОС нельзя") сюда не идет: это решение, а не дело. Перезагрузка,
    // заглушенная до нового, идет приглушенной в низ своей группы; только dbus - своей группой "при случае"
    for (const x of (s.os_advice ?? []).filter((x) => !x.muted || x.muted_new)) {
      const grp = `os-${x.kind}${x.soft ? '-soft' : ''}`
      items.push({ key: `os-${x.kind}-${s.id}`, icon: x.level >= 2 ? '⛔' : '🧰', section: 'servers', id: s.id,
        name: s.name, cc: s.country, sec: 'osupd', grp, detail: x.text,
        text: `${s.name}: ${x.text}`, mute: x.new_key, muted: !!x.muted_new,
        tip: x.pkgs?.length ? `${t('обновлены пакеты')}: ${x.pkgs.join(', ')}` : undefined })
    }
    // версии без поддержки - список дел, а не авария: на сервер одна строка ("без поддержки
    // PostgreSQL 12, PHP 7.3 и 7.4"), такие серверы сворачиваются в один пункт, где и что - в карточке
    for (const lvl of [2, 1]) {
      const sw = (s.sw_advice ?? []).filter((x) => (x.level >= 2 ? 2 : 1) === lvl)
      if (!sw.length) continue
      const list = sw.map((x) => x.what).join(', ')
      items.push({ key: `sw-${lvl}-${s.id}`, icon: lvl >= 2 ? '⛔' : '🧰', section: 'servers', id: s.id,
        name: s.name, cc: s.country, sec: 'software', ...hm(s, lvl >= 2 ? 'sw-eol' : 'sw-soon'), detail: list,
        text: lvl >= 2 ? t('{name}: без поддержки {list}', { name: s.name, list })
          : t('{name}: скоро кончится поддержка {list}', { name: s.name, list }) })
    }
    // Чужой сервер по внешнему адресу сюда не идет: это шлюз исходящего NAT, а внешний адрес панель
    // берет из запроса агента, поправить нечего. Совет "поправьте внешний адрес" был невыполним
    // у контейнеров ключи от хоста сверх положенного: ведем в Docker, там что поправить и
    // кнопка "по назначению"
    const risk = (s.docker_risk ?? []).filter((x) => !x.muted)
    if (risk.length > 0 && !(s.alert_mutes ?? []).includes('docker_risk')) {
      const list = risk.map((x) => `${x.name} (${dockRiskText(x, t)})`).join(', ')
      items.push({ key: `dockrisk-${s.id}`, icon: '🔑', section: 'docker', id: s.id, name: s.name, cc: s.country, sec: 'host',
        ...hm(s, 'dockrisk'), detail: list,
        text: t('{name}: у контейнеров ключи от хоста: {list} - взлом приложения в них даст root на сервере',
          { name: s.name, list }) })
    }
    // не переживет перезагрузку: контейнеры - в Docker (там команда), юниты - в карточку
    const nb = s.noboot_advice
    if (nb && nb.docker.length > 0)
      items.push({ key: `noboot-d-${s.id}`, icon: '🔁', section: 'docker', id: s.id, name: s.name, cc: s.country, sec: 'boot',
        ...hm(s, 'noboot-d'), detail: shortList(nb.docker, t),
        text: t('{name}: после перезагрузки не поднимутся контейнеры без restart policy: {list}',
          { name: s.name, list: shortList(nb.docker, t) }) })
    if (nb && nb.units.length > 0)
      items.push({ key: `noboot-u-${s.id}`, icon: '🔁', section: 'servers', id: s.id, name: s.name, cc: s.country,
        sec: 'noboot', ...hm(s, 'noboot-u'), detail: shortList(nb.units, t), text: t('{name}: после перезагрузки не запустятся {list}: работают, но не включены',
          { name: s.name, list: shortList(nb.units, t) }) })
    // учетки с доступом, в которые давно не входят, и слабые ключи
    const ac = s.access_advice
    if (ac && ac.stale.length > 0)
      items.push({ key: `access-s-${s.id}`, icon: '🗝', section: 'servers', id: s.id, name: s.name, cc: s.country,
        sec: 'access', ...hm(s, 'access-s'), detail: shortList(ac.stale, t), text: t('{name}: учетки с доступом, в которые давно не входят: {list} - проверьте, нужны ли они',
          { name: s.name, list: shortList(ac.stale, t) }) })
    if (ac && ac.weak.length > 0)
      items.push({ key: `access-w-${s.id}`, icon: '🗝', section: 'servers', id: s.id, name: s.name, cc: s.country,
        sec: 'access', ...hm(s, 'access-w'), detail: shortList(ac.weak, t), text: t('{name}: слабые SSH-ключи у {list} - замените на ed25519',
          { name: s.name, list: shortList(ac.weak, t) }) })
    // SSH пускает по паролю, а порт открыт в интернет; "вход по паролю нужен" - решение, сюда не идет.
    // Есть проблема ssh_risk - она уже красной строкой в "Что сломано", совет не повторяем (как в карточке)
    const sshRisk = srvIssues(s, t).some((i) => i.kind === 'ssh_risk')
    for (const a of (s.ssh_advice ?? []).filter((a) => !a.muted && !sshRisk)) {
      items.push({ key: `ssh-${s.id}`, icon: a.level >= 2 ? '⛔' : '🔐', section: 'servers', id: s.id,
        name: s.name, cc: s.country, sec: 'ssh', ...hm(s, `ssh-${a.kind}`), detail: a.text, text: `${s.name}: ${a.text}` })
    }
  }
  return items
}

// Заглушено на время целиком: ключи, которые глушат вид у сервера разом, - голый вид (units, kube_pod,
// disk@1) снузом, и снуз всего сервера. По ним молчат алерты о любой вещи этого вида, в том числе о новых.
// Поштучные (unit:nginx.service, kube_pod:ns/web), решения "так и должно быть" и заглушки "Требует
// действий" (home:) сюда не идут: они про одну вещь. Насовсем - тоже: это решение, а не забытое, оно
// живет в карточке сервера и в "Заглушено" раздела Серверы. Кончившиеся при живой проблеме - своим
// блоком выше (overRows). Где тихо сутки, панель снимает сама (app/mute_watch.py)
type WideMute = MuteItem
function wideMutes(s: Server, t: T, now = Date.now()): WideMute[] {
  const wide = (k: string) => !!k && !k.includes(':') && !isDecision(k)
  // вид с уровнем (disk@1): подпись вида, если своей у ключа нет, и до какого уровня
  const label = (k: string) => {
    const own = kindLabel(k, t)
    const m = /^(.+)@(\d)$/.exec(k)
    if (own !== k || !m) return own
    return `${kindLabel(m[1], t)} - ${m[2] === '1' ? t('только предупреждения') : t('предупр. и проблемы')}`
  }
  const out: WideMute[] = []
  if (s.snooze_until && Date.parse(s.snooze_until) > now)
    out.push(withLive(s, { label: t('весь сервер'), until: s.snooze_until, group: 'alert', key: '' }))
  for (const [k, u] of Object.entries(s.alert_snoozes ?? {}))
    if (wide(k) && Date.parse(u) > now && !(s.alert_mutes ?? []).includes(k))
      out.push(withLive(s, { label: label(k), until: u, group: 'alert', key: k }))
  return out.filter((i) => !i.over)
}

type Props = {
  onNavigate: (s: Section) => void
  onOpen: (s: Section, id?: number, srv?: boolean, sec?: string) => void
  onUnauthorized: () => void
}

// more — сколько ЕЩЁ мониторов висит на том же истекающем имени; check - монитор, в который
// ведет строка (у домена - с наименьшим сроком), через него откладывают
type Warn = {
  id: number
  name: string
  kind: 'ssl' | 'domain'
  days: number
  more?: number
  check: Check
  members: number[]
}

const maxWarn = (w: number[]) => (w.length ? Math.max(...w) : 0)

// hidden: отложенные и "не следим" (ExpirySnooze.tsx) - не на главной, строкой внизу блока
function expiryWarnings(checks: Check[], hidden = false): Warn[] {
  const out: Warn[] = []
  // Домен продлевают ЦЕЛИКОМ, поэтому истекает он один, а мониторов на его
  // поддоменах может быть сколько угодно: пять строк «gitlab/y/bot/msg/env
  // .example.com · домен · 4 дн.» — это один и тот же example.com, и список из-за
  // них выглядит как пять разных проблем. Схлопываем в строку про сам домен.
  // Сертификаты так не сворачиваем: у каждого хоста он обычно свой, и wildcard
  // от отдельных по данным монитора не отличить.
  const byDomain = new Map<string, Warn>()
  // отложенные - вместе со сроком, когда отложили, даже если он уже за порогами монитора
  const inWarn = (c: Check, kind: 'ssl' | 'domain', days: number, warn: number[]) =>
    expHidden(c, kind) === hidden && (days <= maxWarn(warn) || (hidden && !!expEntry(c, kind)))
  for (const c of checks) {
    if (c.check_ssl && c.ssl_days != null && inWarn(c, 'ssl', c.ssl_days, c.ssl_warn_days))
      out.push({ id: c.id, name: c.name, kind: 'ssl', days: c.ssl_days, check: c, members: [c.id] })
    if (c.check_domain && c.domain_days != null && inWarn(c, 'domain', c.domain_days, c.domain_warn_days)) {
      const zone = registrableDomain(c.target || c.name)
      const seen = byDomain.get(zone)
      if (seen) {
        seen.members.push(c.id)
        seen.more = (seen.more ?? 0) + 1
        // ведём в монитор с наименьшим сроком — он и есть самый срочный
        if (c.domain_days < seen.days) {
          seen.days = c.domain_days
          seen.id = c.id
          seen.check = c
        }
        continue
      }
      const w: Warn = { id: c.id, name: zone, kind: 'domain', days: c.domain_days, check: c, members: [c.id] }
      byDomain.set(zone, w)
      out.push(w)
    }
  }
  return out.sort((a, b) => a.days - b.days)
}

function RetirePortButton({ id, port, since, onDone, onUnauthorized }: {
  id: number; port: number; since: string; onDone: (port: number, since: string) => void; onUnauthorized: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const retire = async () => {
    setBusy(true)
    setError('')
    try {
      await retireListeningPort(id, port, since)
      onDone(port, since)
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized()
      else setError(t(e instanceof ApiError && e.status === 409
        ? 'Состояние порта изменилось. Обновите страницу.'
        : 'Не удалось снять алерт порта'))
    } finally {
      setBusy(false)
    }
  }
  return <span className="home-recheck-wrap" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
    <button type="button" className="home-recheck" disabled={busy} onClick={retire}
      title={t('Если порт снова появится, панель начнет отслеживать его заново')}>
      {busy ? t('Снимаем алерт…') : t('Порт больше не нужен')}
    </button>
    {error && <span role="status" className="muted">{error}</span>}
  </span>
}

export function HomePage({ onNavigate, onOpen, onUnauthorized }: Props) {
  const { lang, t } = useI18n()
  // Главная — витрина всех разделов, поэтому она обязана уважать нарезку доступа:
  // иначе учётка без «Бэкапов» видит на ней и карточку бэкапов, и пункты, ведущие
  // в закрытый раздел (клик по ним всё равно упрётся в 403).
  const { sections, isViewer, isAdmin, username } = useAuth()
  const canSee = useCallback(
    (sec: Section) => sections.length === 0 || sections.includes(sec),
    [sections],
  )
  const showSites = canSee('sites')
  const showSrv = canSee('servers')
  const showServicesTile = !showSrv && canSee('services')
  const showServerData = showSrv || showServicesTile || canSee('docker') || canSee('kuber') || canSee('backups')
  const sitesSource = useOverviewSource(checksOverview, onUnauthorized)
  const serversSource = useOverviewSource(listServers, onUnauthorized)
  const { data: ov, refresh: refreshSites } = sitesSource
  const { data: servers, refresh: refreshServers, replace: replaceServers } = serversSource
  // отметить проблему сервера известной (📌 в "Что сломано")
  const [knownFor, setKnownFor] = useState<{ id: number; kind: string; title: string } | null>(null)
  const [avail, setAvail] = useState('')
  const [relProblem, setRelProblem] = useState('')
  const [probeHints, setProbeHints] = useState<LocalProbeSuggestion[]>([])
  const [mutes, setMutes] = useState<MuteWarning[]>([])
  const [queueAll, setQueueAll] = useState(false)
  const [queueArea, setQueueArea] = useState<TallyTile | null>(null)
  const [openGrps, setOpenGrps] = useState<Set<string>>(() => new Set())
  // идет заглушка одиночной строки (у групп свое, ActGroup.tsx)
  const [actBusy, setActBusy] = useState(false)
  const toggleGrp = (g: string) =>
    setOpenGrps((prev) => {
      const n = new Set(prev)
      if (n.has(g)) n.delete(g)
      else n.add(g)
      return n
    })

  useEffect(() => {
    getAgentRelease()
      .then((r) => { setAvail(r.version); setRelProblem(r.problem || '') })
      .catch(() => { setAvail(''); setRelProblem('') })
  }, [])

  const load = useCallback(() => {
    if (showSites) refreshSites()
    if (showServerData) refreshServers()
    // предложения «проверять локально»: тихо, без ошибок в интерфейсе — это подсказка,
    // а не состояние системы, и её недоступность не должна ничего ломать
    if (showSites) localProbeSuggestions()
      .then((r) => setProbeHints(r.items))
      .catch(() => setProbeHints([]))
    // объекты, по которым не сработает ни один алерт. Доступно только админу
    // (область действия — часть настроек алертов), остальным просто молчим
    if (isAdmin)
      alertCoverage()
        .then((r) => setMutes(r.items))
        .catch(() => setMutes([]))
    else setMutes([])
  }, [refreshSites, refreshServers, showSites, showServerData, isAdmin])
  const rechecks = useAlertRechecks(load)
  const recheckAction = (refs: RecheckRef[]) => {
    if (isViewer || !refs.length) return null
    const blocked = Object.fromEntries(refs.flatMap((r) => {
      const reason = recheckUnavailable(r, (servers ?? []).find((s) => s.id === r.object_id),
        !!ov?.checks.find((c) => c.id === r.object_id)?.probe_local)
      return reason ? [[recheckKey(r), reason]] : []
    }))
    return <RecheckButton refs={refs} jobs={rechecks.jobs} run={rechecks.run} blocked={blocked} />
  }
  const actRef = (it: ActItem): RecheckRef[] => it.id && it.grp
    ? [{ object_type: 'server', object_id: it.id, kind: `home:${it.grp}` }] : []

  // быстрый мут прямо с главной: приглушить типы алертов сервера навсегда (alert_mutes).
  // Для диска warn ключ 'disk@1' — крит и проблема продолжат алертить (см. _muted).
  const quickMute = useCallback((id: number, keys: string[]) => {
    muteServerKeys(id, keys).then(load).catch(toastError)
  }, [load])
  // Заглушить до нового (on) или вернуть пункты "Требует действий": ключ пункта в alert_mutes сервера.
  // Прежние заглушки того же вида (до "@") уходят: они про то, чего на сервере уже нет
  const muteActs = async (list: ActItem[], on: boolean) => {
    const jobs = list.flatMap((it) => {
      if (it.id == null || !it.mute) return []
      return [on ? muteServerKeys(it.id, [it.mute], [], true) : muteServerKeys(it.id, [], [it.mute])]
    })
    const results = await Promise.allSettled(jobs)
    for (const result of results) if (result.status === 'rejected') toastError(result.reason)
    load()
  }
  // 🔕 с выбором срока: на час, день, неделю (снуз) или постоянно. Ключи без снуза (рост памяти,
  // перезапуски, пропавший порт - их бэкенд читает только из alert_mutes) глушатся сразу постоянно
  const [mutePick, setMutePick] = useState<string | null>(null)
  // сроки сайтов: у какой строки открыт выбор "напомнить за"
  const [warnPick, setWarnPick] = useState<string | null>(null)
  // блок "заглушено на время": раскрыт ли и какой ключ сейчас снимается ('*' - все, где тихо); то же
  // у блока "заглушка кончилась"
  const [wideOpen, setWideOpen] = useState(false)
  const [wideBusy, setWideBusy] = useState('')
  const oomMuteText = (k: string) =>
    k.startsWith(OOM_MUTE) ? t('не алертить об OOM из-за {n}', { n: oomCulpritLabel(k.slice(OOM_MUTE.length), t, true) }) : null
  const muteFor = (id: number, key: string, hours: number) => {
    setMutePick(null)
    if (hours < 0) return quickMute(id, [key])
    snoozeServerAlert(id, key, hours).then(load).catch(toastError)
  }

  useEffect(() => {
    load()
    const id = window.setInterval(load, 15000)
    return () => window.clearInterval(id)
  }, [load])

  const warns = ov ? expiryWarnings(ov.checks) : []
  // Отложенные сроки и сроки, за которыми не следим, можно вернуть из отдельного блока.
  const warnsOff = ov ? expiryWarnings(ov.checks, true) : []
  // Заголовок — про СЕЙЧАС: сколько мониторов лежит или деградирует. Открытые
  // инциденты сюда не подмешиваем: иначе при нуле проблем выходило красное
  // «Проблемы: 0». Счётчик инцидентов виден строкой ниже.
  const downChecks = (ov?.checks ?? [])
    // только включённые: выключенный «down» — не проблема (и счётчики его не считают).
    // Отвечающий через раз тоже здесь: по последней проверке он обычно "работает".
    .filter(
      (c) =>
        c.enabled &&
        (c.last_status === 'down' || c.last_status === 'degraded' || !!c.flaky_since) &&
        // известная проблема - не в "проблемах", она в своем блоке
        !knownActive(c.known_issue) && !expiredDomainMuted(c),
    )
    .sort((a, b) => (a.last_status === 'down' ? 0 : 1) - (b.last_status === 'down' ? 0 : 1))
  // лежащие, но отмеченные известной проблемой: "все в норме" про них было бы неправдой
  const knownDown = (ov?.checks ?? []).filter(
    (c) =>
      c.enabled && knownActive(c.known_issue) &&
      (c.last_status === 'down' || c.last_status === 'degraded' || !!c.flaky_since),
  ).length
  const mutedDomainDown = (ov?.checks ?? []).filter((c) =>
    c.enabled && expiredDomainMuted(c) && !knownActive(c.known_issue) &&
    (c.last_status === 'down' || c.last_status === 'degraded' || !!c.flaky_since),
  ).length
  const mutedDomainText = mutedDomainDown && t('сбои заглушенных доменов: {n}', { n: mutedDomainDown })
  const srvTotal = servers?.length ?? 0
  const srvOnline = servers?.filter((s) => s.online).length ?? 0
  const srvList = servers ?? []
  // Fleet actions may finish with a list based on an older render. Read a fresh
  // snapshot through the same path as dashboard polling.
  const fleetRun = useFleetRollout(servers, avail, refreshServers, onUnauthorized, !isViewer)
  const allActions = actionItems(srvList, relProblem, t).filter((x) => canSee(x.section))
  // отказ самообновления, пока идет раскатка, виден в строке агента по ноде - без повтора
  const fleet = allActions.filter((x) => x.fleet && !(fleetRun.agent && x.key.startsWith('adv-')))
  const actionRows = groupActs(allActions.filter((x) => !x.fleet))
  const dockerHosts = srvList.filter((s) => s.last_report?.docker?.present && s.last_report.docker.access)
  const dockerRunning = dockerHosts.reduce((n, s) => n + (s.last_report?.docker?.containers?.filter((c) => c.state === 'running').length ?? 0), 0)
  const dockerProbs = dockerProblems(srvList, t)
  const kubeHosts = srvList.filter((s) => s.last_report?.kube?.present && s.last_report.kube.access)
  const kubePods = kubeHosts.reduce((n, s) => n + (s.last_report?.kube?.pods?.filter((p) => p.phase === 'Running').length ?? 0), 0)
  const backupClients = srvList.filter((s) => backupAlive(s.last_report?.backup))
  const backupServers = srvList.filter((s) => s.last_report?.backup_server?.present)
  const backupProbs = backupProblems(srvList, t)

  // Строки ведут в раздел, где можно разобраться с проблемой.
  type Broken = {
    key: string; id: number; name: string; cc?: string; tone: 't-down' | 't-degraded'; text: string
    kind: string; section: Section; srv?: boolean; sec?: string; mute?: string; since?: string | null
    note?: string
    listenPort?: number
    markable?: boolean // можно отметить известной: вид, который панель сейчас видит у сервера
  }
  const fromProbs = (items: ProbItem[], section: Section): Broken[] =>
    canSee(section)
      ? items.map((p) => ({
          key: p.key, id: p.id ?? 0, name: p.name ?? '', cc: p.cc, tone: p.down ? 't-down' : 't-degraded',
          text: p.name && p.text.startsWith(p.name + ': ') ? p.text.slice(p.name.length + 2) : p.text,
          kind: section, section, srv: p.srv, sec: p.sec, since: p.since,
        }))
      : []
  // сервер молчит вместе со шлюзом, за которым стоит: причина в строке, свой алерт он ждет
  const srvById = new Map(srvList.map((s) => [s.id, s]))
  const behindSilent = (s: Server) => {
    const p = s.parent_id ? srvById.get(s.parent_id) : undefined
    return p && !p.online ? p.name : ''
  }
  const broken: Broken[] = [
    // проблемы сервера ведут в его карточку, а то, что чинится в другом разделе (сроки и Flux,
    // очереди, дампы), - туда, и видно тем, кому этот раздел открыт
    ...srvList.flatMap((s) =>
      srvIssues(s, t)
        .map((i, n) => ({
          key: i.listenPort ? `${s.id}-listen-${i.listenPort}-${i.since}` : `${s.id}-${n}`,
          id: s.id, name: s.name, cc: s.country, tone: i.tone,
          text: i.kind === 'offline' && behindSilent(s)
            ? `${i.text} · ⛓ ${t('за {srv}, он тоже молчит', { srv: behindSilent(s) })}`
            : i.text,
          kind: i.kind ?? 'server', section: (i.section ?? 'servers') as Section, srv: i.srv ?? true,
          sec: i.sec, mute: i.mute, since: i.since, note: s.note, listenPort: i.listenPort,
          markable: !!i.kind && i.kind !== 'offline' && !!s.alert_since?.[i.kind],
        }))
        .filter((i) => canSee(i.section)),
    ),
    ...fromProbs(dockerProbs, 'docker'),
    // кубер (поды, NotReady-ноды, сроки, Flux) приходит с бэкенда строками problems (section kuber)
    // по тем же правилам, что алерт: здесь его второй раз не считаем
    ...fromProbs(backupProbs, 'backups'),
    // признаки взлома сайта (backend app/tamper.py): похоже на взлом - красным, один слабый
    // признак - желтым; клик ведет в раздел "Признаки взлома" карточки монитора
    ...(canSee('sites')
      ? (ov?.checks ?? [])
          .filter((c) => c.enabled && !!c.tamper && c.tamper.level !== 'none')
          .map((c): Broken => ({
            key: `tm-${c.id}`, id: c.id, name: c.name, tone: c.tamper!.level === 'hack' ? 't-down' : 't-degraded',
            text: c.tamper!.summary, kind: 'tamper', section: 'sites', srv: false, sec: 'tamper',
            since: c.tamper!.since,
          }))
      : []),
  ].sort((a, b) => (a.tone === b.tone ? a.name.localeCompare(b.name) : a.tone === 't-down' ? -1 : 1))
  // предупреждения с одинаковой шапкой с двух и больше серверов - одна строка; красное - как есть
  type BrokenRow = { one: Broken } | { grp: string; head: string; items: Broken[] }
  const brokenKey = (b: Broken) => (b.tone === 't-down' ? '' : `${tallyTile(b.section)}|${b.kind}|${splitHead(b.text)[0]}`)
  const brokenBy = new Map<string, Broken[]>()
  for (const b of broken) {
    const k = brokenKey(b)
    if (k) brokenBy.set(k, [...(brokenBy.get(k) ?? []), b])
  }
  const brokenRows: BrokenRow[] = []
  for (const b of broken) {
    const k = brokenKey(b)
    const list = k ? brokenBy.get(k)! : null
    if (!list || new Set(list.map((x) => x.id)).size < 2) brokenRows.push({ one: b })
    else if (list[0] === b) brokenRows.push({ grp: k, head: splitHead(b.text)[0], items: list })
  }

  // Известные проблемы: про них знают (backend app/known.py), алерт молчит, пока не стало хуже.
  // Отдельный серый блок вместо "Что сломано": видно, что висит и что про это написали.
  type KnownRow = {
    key: string; id: number; name: string; cc?: string; text: string; kind: string; section: Section
    srv?: boolean; sec?: string; known: KnownIssue; check?: boolean
  }
  const knownRows: KnownRow[] = [
    ...srvList.flatMap((s) =>
      srvKnown(s, t)
        .map((i, n) => ({
          key: `k-${s.id}-${n}`, id: s.id, name: s.name, cc: s.country, text: i.text,
          kind: i.kind ?? 'server', section: (i.section ?? 'servers') as Section, srv: i.srv ?? true,
          sec: i.sec, known: i.known,
        }))
        .filter((i) => canSee(i.section)),
    ),
    ...(canSee('sites')
      ? (ov?.checks ?? [])
          .filter(
            (c) =>
              c.enabled && knownActive(c.known_issue) &&
              (c.last_status === 'down' || c.last_status === 'degraded' || !!c.flaky_since),
          )
          .map((c) => ({
            key: `kc-${c.id}`, id: c.id, name: c.name, text: c.last_message || c.last_status,
            kind: 'site', section: 'sites' as Section, known: c.known_issue as KnownIssue, check: true,
          }))
      : []),
  ]
  const dropKnown = (it: KnownRow) =>
    (it.check ? dropCheckKnown(it.id) : dropServerKnown(it.id, it.kind)).then(load).catch(toastError)

  // Хосты для ansible: те, что плейбук реально чинит - устаревший helper (переустановка)
  // ИЛИ не включен watchdog (helper agent-watchdog, ALWAYS)
  const rolloutHosts = fleetHosts(srvList)
  // Блок "Обновления": строки агента и helper'ов со своими кнопками (подтверждение вторым
  // кликом в ту же точку, прогресс в строке, по успеху строка уходит сама) - FleetUpdate.tsx,
  // строка агента та же, что в баннере "Серверов". Ниже - то, что кнопкой не чинится
  const canUpd = !isViewer && showSrv
  const agentRowOn = showSrv && (agentBehind(srvList, avail).some((s) => s.online) || !!fleetRun.agent)
  const helperRowOn = showSrv && (rolloutHosts.length > 0 || !!fleetRun.helpers)
  const updN = fleet.length + (agentRowOn ? 1 : 0) + (helperRowOn ? 1 : 0)
  // У нескольких строк одного сервера может быть общий ключ заглушки, например disk_health.
  const mutePickerKey = (it: Broken) => JSON.stringify([it.id, it.kind, it.mute, it.sec, it.text])
  // строка "Что сломано"; sub - внутри раскрытой группы: сервер и подробности без шапки
  const brokenRow = (it: Broken, sub = false) => (
    <div role="button" tabIndex={0}
      key={it.key}
      className={`home-attention-row${sub ? ' home-attention-subrow' : ''}`}
      onClick={() => onOpen(it.section, it.id, it.srv, it.sec)}
      onKeyDown={(e) => activateHomeRow(e, () => onOpen(it.section, it.id, it.srv, it.sec))}
    >
      {!sub && <span className={`lk-dot ${it.tone === 't-down' ? 'lk-dot-bad' : 'lk-dot-warn'}`} />}
      {!sub && <span className="home-attention-ico">{ISSUE_ICON[it.kind] ?? '⚠️'}</span>}
      <span className="home-attention-txt">
        <CountryFlag code={it.cc} /> <b>{it.name}</b>
        {sub ? (splitHead(it.text)[1] ? <span className="muted"> - {splitHead(it.text)[1]}</span> : null) : ` - ${it.text}`}
        {sinceShort(it.since, t) && <span className="muted"> · {sinceShort(it.since, t)}</span>}
        {it.srv && it.section === 'servers' && <MaintChip kind="server" id={it.id} />}
        {it.note && (
          <span className="note-chip" title={it.note}>
            {' '}📝
          </span>
        )}
      </span>
      {!isViewer && it.listenPort && it.since && (
        <RetirePortButton id={it.id} port={it.listenPort} since={it.since} onUnauthorized={onUnauthorized}
          onDone={(port, since) => replaceServers((prev) => prev.map((s) => s.id === it.id ? {
            ...s, problems: s.problems?.filter((p) => !(p.kind === 'listen_gone' && p.listen_port === port && p.since === since)),
          } : s))} />
      )}
      {recheckAction([{ object_type: it.section === 'sites' ? 'check' : 'server', object_id: it.id, kind: it.kind }])}
      {it.markable && !isViewer && (
        <span className="quick-mute" title={t('Известная проблема: алерт молчит, пока не станет хуже')}
          onClick={(e) => {
            e.stopPropagation()
            setKnownFor({ id: it.id, kind: it.kind, title: `${it.name} - ${it.text}` })
          }}>
          📌
        </span>
      )}
      {it.mute && !isViewer && mutePick === mutePickerKey(it) && (
        <span className="home-mute-pick" onClick={(e) => e.stopPropagation()}>
          <span className="muted">{oomMuteText(it.mute) ? `${oomMuteText(it.mute)}:` : t('не алертить:')}</span>
          {[
            { h: 1, l: t('1 час') },
            { h: 24, l: t('1 день') },
            { h: 24 * 7, l: t('1 неделя') },
            { h: -1, l: t('постоянно') },
          ].map(({ h, l }) => (
            <span key={h} className="home-mute-opt" role="button" onClick={() => muteFor(it.id, it.mute!, h)}>
              {l}
            </span>
          ))}
          <span className="home-mute-opt muted" role="button" onClick={() => setMutePick(null)}>✕</span>
        </span>
      )}
      {it.mute && !isViewer && mutePick !== mutePickerKey(it) && (
        // OOM глушится по виновнику: в подсказке, по какому именно (он же в тексте строки)
        <span className="quick-mute" title={oomMuteText(it.mute) ?? t('Не алертить')}
          onClick={(e) => {
            e.stopPropagation()
            if (snoozable(it.mute!)) setMutePick(mutePickerKey(it))
            else quickMute(it.id, [it.mute!])
          }}>
          🔕
        </span>
      )}
      <span className="home-open">→</span>
    </div>
  )
  const brokenGrpRow = (r: BrokenRow) => {
    if ('one' in r) return brokenRow(r.one)
    const open = openGrps.has(`b-${r.grp}`)
    const names = shortList([...new Set(r.items.map((x) => x.name))], t, 4)
    return (
      <div key={`bg-${r.grp}`} className={`home-attention-grpbox${open ? ' open' : ''}`}>
        <div role="button" tabIndex={0} className="home-attention-row" onClick={() => toggleGrp(`b-${r.grp}`)}
          onKeyDown={(e) => activateHomeRow(e, () => toggleGrp(`b-${r.grp}`))}>
          <span className="lk-dot lk-dot-warn" />
          <span className="home-attention-ico">{ISSUE_ICON[r.items[0].kind] ?? '⚠️'}</span>
          <span className="home-attention-txt">
            {r.head}
            <span className="muted"> · {t('серверов: {n}', { n: new Set(r.items.map((x) => x.id)).size })} ({names})</span>
          </span>
          {recheckAction(r.items.map((it) => ({ object_type: it.section === 'sites' ? 'check' : 'server', object_id: it.id, kind: it.kind })))}
          <span className="home-open">{open ? '▾' : '▸'}</span>
        </div>
        {open && <div className="home-attention-sub">{r.items.map((x) => brokenRow(x, true))}</div>}
      </div>
    )
  }
  const actRow = (it: ActItem) => (
    <div role="button" tabIndex={0} key={it.key} className="home-attention-row" title={it.tip} onClick={() => onOpen(it.section, it.id, it.srv, it.sec)}
      onKeyDown={(e) => activateHomeRow(e, () => onOpen(it.section, it.id, it.srv, it.sec))}>
      <span className={`lk-dot ${it.fleet ? 'lk-dot-info' : 'lk-dot-warn'}`} />
      <span className="home-attention-ico">{it.icon}</span>
      <span className="home-attention-txt">
        <CountryFlag code={it.cc} /> {it.text}
      </span>
      {recheckAction(actRef(it))}
      {it.mute && !isViewer && it.grp && (
        <QuickMute tip={grpMuteTip(it.grp, t)} busy={actBusy}
          onMute={() => {
            setActBusy(true)
            muteActs([it], true).finally(() => setActBusy(false))
          }} />
      )}
      <span className="home-open">→</span>
    </div>
  )
  // группа - общий компонент с галками и заглушкой (ActGroup.tsx)
  const grpRow = (r: ActRow) =>
    'one' in r ? (
      actRow(r.one)
    ) : (
      <ActGroup key={`g-${r.grp}`} grp={r.grp} items={r.items} muted={r.muted} open={openGrps.has(r.grp)}
        canMute={!isViewer} onToggle={() => toggleGrp(r.grp)} onMute={muteActs}
        recheckHeader={recheckAction(r.items.flatMap(actRef))} recheckItem={(it) => recheckAction(actRef(it))}
        onOpen={(it) => onOpen(it.section, it.id, it.srv, it.sec)} />
    )
  // заглушено на время целиком (wideMutes): строка на сервер, ведет в его карточку
  const wideRows = canSee('servers')
    ? srvList.map((s) => ({ s, items: wideMutes(s, t) })).filter((x) => x.items.length > 0)
        .sort((a, b) => a.s.name.localeCompare(b.s.name))
    : []
  // вернуть: снять ключ теми же ручками, что в "Заглушено" (mutes.tsx liftMute) - снуз на 0 часов
  const liftWide = (s: Server, i: WideMute) => {
    const id = `${s.id}|${i.key}|${i.until ?? ''}`
    setWideBusy(id)
    liftMute(s, i).then(load).catch(() => {}).finally(() => setWideBusy(''))
  }
  // вернуть все, где тихо: по одной, у сервера по очереди (alert_snoozes сохраняется целиком)
  const wideQuiet = wideRows.flatMap(({ s, items }) => items.filter((i) => muteQuiet(i)).map((i) => ({ s, i })))
  const liftQuiet = async () => {
    setWideBusy('*')
    for (const { s, i } of wideQuiet) await liftMute(s, i).catch(() => {})
    setWideBusy('')
    load()
  }
  // временная заглушка кончилась, а проблема есть (app/mute_watch.py): панель держит ее и спрашивает,
  // что дальше - продлить, насовсем или вернуть алерты. Любые: и вид целиком, и одна вещь
  const overRows = canSee('servers')
    ? srvList.flatMap((s) => collectMutes(s, t, ['alert']).filter((i) => i.over).map((i) => ({ s, i })))
        .sort((a, b) => a.s.name.localeCompare(b.s.name) || a.i.label.localeCompare(b.i.label))
    : []
  // hours > 0 - продлить, -1 - насовсем (мьют, снуз снимается), 0 - вернуть алерты
  const overAct = async (s: Server, i: MuteItem, hours: number) => {
    setWideBusy(`over|${s.id}|${i.key}`)
    try {
      if (hours < 0) await muteServerKeys(s.id, [i.key])
      const h = Math.max(hours, 0)
      await (i.key === '' ? snoozeServer(s.id, h) : snoozeServerAlert(s.id, i.key, h))
    } catch {
      // сеть или права: строка останется, повторят
    }
    setWideBusy('')
    load()
  }
  const whenText = (u: string) =>
    new Date(u).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  // бэкап вышел за ночное окно: мягкое уведомление (НЕ алерт, не в Telegram)
  const late = canSee('backups')
    ? srvList.map((s) => ({ s, note: backupWindowNote(s) })).filter((x) => x.note)
    : []
  // строка срока сертификата/домена: 🔕 - напомнить за 7/1 дней или не следить (ExpirySnooze.tsx);
  // off - отложенная, в раскрытой строке внизу блока, с "вернуть"
  const warnRow = (w: Warn, off = false) => {
    const key = `${w.kind}-${w.id}`
    const e = expEntry(w.check, w.kind)
    return (
      <div key={key} role="button" tabIndex={0}
        className={`home-attention-row home-site-alert${off ? ' home-attention-subrow home-attention-muted' : ''}`}
        onClick={() => onOpen('sites', w.id, false, 'exp')}
        onKeyDown={(ev) => ev.key === 'Enter' && ev.target === ev.currentTarget && onOpen('sites', w.id, false, 'exp')}>
        <span className={`lk-dot ${off ? '' : w.days <= 3 ? 'lk-dot-bad' : 'lk-dot-warn'}`} />
        <span className="home-attention-ico">{w.kind === 'ssl' ? '🔐' : '🌐'}</span>
        <span className="home-attention-txt" title={off && e ? expSnoozeTip(e, t) : undefined}>
          <b>{w.name}</b>
          <span className="muted">
            {' - '}
            {w.kind === 'ssl' ? t('SSL') : t('домен')} · {expiryText(w.days, t)}
            {w.more ? ` · ${t('мониторов: {n}', { n: w.more + 1 })}` : ''}
          </span>
        </span>
        <span className="home-site-alert-actions">
          {!off && recheckAction(w.members.map((id) => ({ object_type: 'check', object_id: id, kind: w.kind })))}
          {off ? (
            <ExpirySnoozeState check={w.check} kind={w.kind} canEdit={!isViewer} onDone={load} />
          ) : !isViewer && warnPick === key ? (
            <ExpirySnoozePick check={w.check} kind={w.kind} short onDone={load} onClose={() => setWarnPick(null)} />
          ) : !isViewer && (
            <span className="quick-mute" title={w.kind === 'domain'
              ? t('Напомнить позже или не следить за доменом')
              : t('Напомнить позже или не следить за сертификатом')}
              onClick={(ev) => { ev.stopPropagation(); setWarnPick(key) }}>
              🔕
            </span>
          )}
        </span>
        <span className="home-open">→</span>
      </div>
    )
  }
  const siteStatusRow = (c: Check) => (
    <div role="button" tabIndex={0} key={c.id} className="home-attention-row home-site-alert" onClick={() => onOpen('sites', c.id)}
      onKeyDown={(e) => activateHomeRow(e, () => onOpen('sites', c.id))}>
      <span className={`lk-dot ${c.last_status === 'down' ? 'lk-dot-bad' : 'lk-dot-warn'}`} />
      <span className="home-attention-ico">🌐</span>
      <span className="home-attention-txt">
        <b>{c.name}</b>
        {(c.parents ?? []).filter((p) => p.silent).map((p) => (
          <span key={p.id} className="type-chip parent-chip" title={t('сервер не на связи, алерты монитора ждут его')}>
            ⛓ {p.name}
          </span>
        ))}
        <span className="muted">
          {' - '}
          {c.last_status === 'down'
            ? t('Недоступно')
            : c.last_status === 'degraded'
              ? t('Деградация')
              : t('Отвечает через раз')}
          {c.last_status !== 'up' && c.last_message ? ` · ${c.last_message}` : ''}
        </span>
      </span>
      <span className="home-site-alert-actions">
        {recheckAction([{ object_type: 'check', object_id: c.id, kind: 'status' }])}
      </span>
      <span className="home-open">→</span>
    </div>
  )

  const queueRows: QueueRow[] = [
    ...brokenRows.map((row): QueueRow => {
      const item = 'one' in row ? row.one : row.items[0]
      return {
        key: 'one' in row ? 'broken-' + item.key : 'broken-group-' + row.grp,
        area: tallyTile(item.section),
        critical: item.tone === 't-down',
        render: () => brokenGrpRow(row),
      }
    }),
    ...(canSee('sites') ? [
      ...downChecks.map((c): QueueRow => ({
        key: 'check-' + c.id, area: 'sites', critical: c.last_status === 'down', render: () => siteStatusRow(c),
      })),
      ...warns.map((w): QueueRow => ({
        key: w.kind + '-' + w.id, area: 'sites', critical: w.days <= 3, render: () => warnRow(w),
      })),
    ] : []),
    ...actionRows.map((row): QueueRow => ({
      key: 'one' in row ? 'action-' + row.one.key : 'action-group-' + row.grp,
      area: tallyTile('one' in row ? row.one.section : row.items[0].section),
      critical: false,
      render: () => grpRow(row),
    })),
  ].sort((a, b) => Number(b.critical) - Number(a.critical))
  const visibleRows = queueArea ? queueRows.filter((row) => row.area === queueArea) : queueRows
  const selectArea = (area: TallyTile | null) => {
    setQueueArea(area)
    setQueueAll(false)
  }
  const loadingText = t('загрузка…')
  const unavailableText = t('Данные недоступны')
  const siteSummary = ov
    ? t('работают {n} из {total}', { n: ov.up, total: ov.total })
    : loadingText
  const siteDetails = [
    siteSummary,
    ov && ov.partial > 0 && t('частично: {n}', { n: ov.partial }),
    knownDown && t('известных: {n}', { n: knownDown }),
    mutedDomainText,
    ov && ov.open_incidents > 0 && t('открытых инцидентов: {n}', { n: ov.open_incidents }),
  ].filter(Boolean).join(' · ')
  const tiles: { area: TallyTile; label: string; icon: ReactNode; summary: string; title?: string }[] = [
    { area: 'sites', label: t('Сайты'), icon: <IconSites />, summary: siteSummary, title: siteDetails },
    { area: 'servers', label: t(showServicesTile ? 'Сервисы' : 'Серверы'), icon: <IconServers />,
      summary: servers ? showServicesTile ? '' : t('онлайн {n} из {total}', { n: srvOnline, total: srvTotal }) : loadingText },
    { area: 'docker', label: 'Docker', icon: <IconDocker />,
      summary: servers ? t('запущено: {n}', { n: dockerRunning }) : loadingText },
    { area: 'kuber', label: 'Kubernetes', icon: <IconKube />,
      summary: servers ? t('подов: {n}', { n: kubePods }) : loadingText },
    { area: 'backups', label: t('Бэкапы'), icon: <IconBackups />,
      summary: servers ? t('под бэкапом: {n}', { n: backupClients.length }) : loadingText,
      title: servers ? t('серверов бэкапа: {n}', { n: backupServers.length }) : undefined },
  ]
  const areas = tiles.filter(({ area }) => canSee(area) || (area === 'servers' && showServicesTile))
  const sourceFor = (area: TallyTile) => area === 'sites' ? sitesSource : serversSource
  const queueSources = [...new Set(areas.filter(({ area }) => !queueArea || queueArea === area)
    .map(({ area }) => sourceFor(area)))]
  const failedSources = queueSources.filter((source) => source.failed)
  const queueLoading = queueSources.some((source) => source.data === null && !source.failed)

  // шапка карточки блока: подпись и счетчик, тон счетчика - по самому тяжелому в блоке
  const cap = (title: string, n?: number, tone?: 'bad' | 'warn' | 'info') => (
    <div className="net-head">
      <div className="chart-cap net-cap">
        {title}
        {n ? <span className={`net-n${tone ? ` net-n-${tone}` : ''}`}>{n}</span> : null}
      </div>
    </div>
  )
  return (
    <div>
      <p className="tagline">{t('Мониторинг инфраструктуры')}</p>
      <RecheckSummary refs={rechecks.last} jobs={rechecks.jobs} err={rechecks.err} />
      {knownFor && (
        <KnownDialog
          title={knownFor.title}
          onClose={() => setKnownFor(null)}
          onSave={async (text, days) => {
            await setServerKnown(knownFor.id, knownFor.kind, text, days)
            setKnownFor(null)
            load()
          }}
        />
      )}
      {areas.length > 0 && (
        <>
          <div className="home-queue-filters" role="group" aria-label={t('Фильтр по разделам')}>
            {areas.map(({ area, label, icon, summary, title }) => {
              const rows = queueRows.filter((row) => row.area === area)
              const critical = rows.some((row) => row.critical)
              const { data, failed } = sourceFor(area)
              return (
                <button key={area} type="button" className="home-queue-filter"
                  aria-pressed={queueArea === area} aria-controls="home-issue-queue"
                  onClick={() => selectArea(queueArea === area ? null : area)}>
                  <span className="home-queue-filter-head">
                    {icon}<strong>{label}</strong>
                    {rows.length > 0 && (
                      <span className={'net-n ' + (critical ? 'net-n-bad' : 'net-n-warn')}
                        aria-label={t('Активных проблем: {n}', { n: rows.length })}>{rows.length}</span>
                    )}
                  </span>
                  <span className="home-queue-filter-summary muted" title={data !== null ? title : undefined}>
                    {failed ? data !== null ? `${summary} · ${t('данные устарели')}` : unavailableText : summary}
                  </span>
                </button>
              )
            })}
          </div>
          <section id="home-issue-queue" className="chart-card home-queue" aria-labelledby="home-issue-queue-title">
            <div className="home-queue-head">
              <h2 id="home-issue-queue-title">{t('Активные проблемы')} <span className="net-n">{visibleRows.length}</span></h2>
              {queueArea ? (
                <div className="home-queue-actions">
                  <button type="button" className="ghost small" onClick={() => selectArea(null)}>{t('Все разделы')}</button>
                  <button type="button" className="ghost small"
                    onClick={() => onNavigate(queueArea === 'servers' && showServicesTile ? 'services' : queueArea)}>
                    {t('Открыть раздел')} →
                  </button>
                </div>
              ) : visibleRows.length > 0 && <span className="muted small">{t('Сначала критичные')}</span>}
            </div>
            {failedSources.map((source) => {
              const sites = source === sitesSource
              return <div key={sites ? 'sites' : 'servers'} className="home-queue-error small">
                <span role="status">
                  {t(sites ? 'Не удалось обновить данные о сайтах' : 'Не удалось обновить данные о серверах')}
                  {source.updatedAt !== null && ` · ${t('Данные на {time}', {
                    time: new Date(source.updatedAt).toLocaleString(lang, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }),
                  })}`}
                </span>
                <button type="button" className="ghost small" onClick={source.refresh}>{t('Повторить запрос')}</button>
              </div>
            })}
            {visibleRows.length > 0 ? (
              <div id="home-issue-queue-rows" className="home-attention-list home-queue-rows">
                {(queueAll ? visibleRows : visibleRows.slice(0, QUEUE_LIMIT)).map((row) => (
                  <Fragment key={row.key}>{row.render()}</Fragment>
                ))}
              </div>
            ) : failedSources.length === 0 && (
              <p className="muted small home-queue-empty">{queueLoading ? loadingText : t('Нет активных проблем')}</p>
            )}
            {visibleRows.length > QUEUE_LIMIT && (
              <button type="button" className="ghost small home-queue-more"
                aria-expanded={queueAll} aria-controls="home-issue-queue-rows"
                onClick={() => setQueueAll(!queueAll)}>
                {queueAll ? t('свернуть') : t('Показать еще {n}', { n: visibleRows.length - QUEUE_LIMIT })}
              </button>
            )}
          </section>
        </>
      )}
      {(knownRows.length > 0 || updN > 0 || (canSee('sites') && warnsOff.length > 0)) && (
        <div className="home-queue-secondary">
          {knownRows.length > 0 && (
            <HomeDisclosure title={t('Известные проблемы')} count={knownRows.length}>
              <div className="home-attention-list">
                {knownRows.map((it) => (
                  <button
                    key={it.key}
                    className="home-attention-row"
                    onClick={() => onOpen(it.section, it.id, it.srv, it.sec)}
                  >
                    <span className="lk-dot" />
                    <span className="home-attention-ico">{it.check ? '🌐' : (ISSUE_ICON[it.kind] ?? '⚠️')}</span>
                    <span className="home-attention-txt">
                      <CountryFlag code={it.cc} /> <b>{it.name}</b> - {it.text}
                      <span className="muted">
                        {' '}· {knownText(it.known, t)}
                        {knownMeta(it.known, t) ? ` (${knownMeta(it.known, t)})` : ''}
                      </span>
                    </span>
                    {!isViewer && (
                      <span className="quick-mute" title={t('Снять отметку: если проблема осталась, придет алерт')}
                        onClick={(e) => { e.stopPropagation(); dropKnown(it) }}>
                        ✕
                      </span>
                    )}
                    <span className="home-open">→</span>
                  </button>
                ))}
              </div>
            </HomeDisclosure>
          )}
          {updN > 0 && (
            <HomeDisclosure title={t('Обновления')} count={updN}>
              {canUpd && <div className="home-queue-actions"><UpdateAllButton servers={srvList} avail={avail} fleet={fleetRun} /></div>}
              <div className="home-attention-list">
                {showSrv && (
                  <AgentUpdRow servers={srvList} avail={avail} fleet={fleetRun} canUpd={canUpd}
                    onOpen={() => onOpen('servers')} onOpenNode={(id) => onOpen('servers', id)} />
                )}
                {showSrv && (
                  <HelperUpdRow servers={srvList} fleet={fleetRun} canUpd={canUpd}
                    onOpen={() => onOpen('servers')} />
                )}
                {fleet.map(actRow)}
              </div>
              {fleetRun.err && <div className="upd-err">{fleetRun.err}</div>}
            </HomeDisclosure>
          )}
          {canSee('sites') && warnsOff.length > 0 && (
            <HomeDisclosure title={t('Заглушенные сроки сайтов')} count={warnsOff.length}>
              <div className="home-attention-list">{warnsOff.map((w) => warnRow(w, true))}</div>
            </HomeDisclosure>
          )}
        </div>
      )}
      <div className="net-cards home-blocks">
      {mutes.length > 0 && isAdmin && <MuteHint items={mutes} onOpen={onOpen} onDone={load} />}
      <MaintHome />
      {/* Заглушка кончилась, а проблема есть: алерты придержаны, решение за человеком */}
      {overRows.length > 0 && (
        <div className="chart-card net-card net-card-wide home-block home-attention-over">
          {cap(t('Заглушка кончилась, а проблема есть'), overRows.length, 'warn')}
          <div className="muted small home-attention-note">
            {t('Алерты по ним панель придержала: придут, если станет хуже. Проблема уйдет - строка пропадет сама')}
          </div>
          <div className="home-attention-list">
            {overRows.map(({ s, i }) => {
              const busy = wideBusy === `over|${s.id}|${i.key}`
              return (
                <div key={`${s.id}|${i.key}`} role="button" tabIndex={0} className="home-attention-row"
                  onClick={() => onOpen('servers', s.id, undefined, 'alerts')}
                  onKeyDown={(ev) => ev.key === 'Enter' && ev.target === ev.currentTarget && onOpen('servers', s.id, undefined, 'alerts')}>
                  <span className="lk-dot lk-dot-warn" />
                  <span className="home-attention-ico">🔕</span>
                  <span className="home-attention-txt">
                    <CountryFlag code={s.country} /> <b>{s.name}</b> - {i.label}
                    <span className="muted"> ({t('кончилась {d}', { d: muteDate(i.over!) })})</span>
                    <span className="muted small home-over-what">{muteLiveText(i, t)}</span>
                    {!isViewer && (
                      <span className="home-over-acts" onClick={(e) => e.stopPropagation()}>
                        {busy ? (
                          <span className="muted small">...</span>
                        ) : (
                          <>
                            <button className="linklike small" disabled={!!wideBusy} onClick={() => overAct(s, i, 24)}>
                              {t('на день')}
                            </button>
                            <button className="linklike small" disabled={!!wideBusy} onClick={() => overAct(s, i, 24 * 7)}>
                              {t('на неделю')}
                            </button>
                            {i.key !== '' && !i.key.includes('@') && (
                              <button className="linklike small" disabled={!!wideBusy} onClick={() => overAct(s, i, -1)}>
                                {t('насовсем')}
                              </button>
                            )}
                            <button className="linklike small" disabled={!!wideBusy}
                              title={t('Снять заглушку: алерт придет на следующей проверке')}
                              onClick={() => overAct(s, i, 0)}>
                              {t('вернуть алерты')}
                            </button>
                          </>
                        )}
                      </span>
                    )}
                  </span>
                  <span className="home-open">→</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
      {/* подсказка - предложение поменять мониторы, учетке только для просмотра не нужна */}
      {probeHints.length > 0 && !isViewer && (
        <div className="chart-card net-card home-block home-attention-probe">
          {cap(t('Похоже на белый список'), probeHints.length)}
          <LocalProbeHint items={probeHints} onDone={load} />
        </div>
      )}
      {/* Мягкие уведомления (НЕ алерты, не в Telegram): бэкап вышел за ночное окно.
          Приглушённо - это «к сведению», а не «требует действий». */}
      {late.length > 0 && (
        <div className="chart-card net-card home-block home-attention-soft">
          {cap(t('Бэкап вышел за окно'), late.length)}
          <div className="home-attention-list">
            {late.slice(0, 6).map(({ s, note }) => (
              <button key={s.id} className="home-attention-row" onClick={() => onOpen('backups', s.id)}>
                <span className="lk-dot" />
                <span className="home-attention-ico">💾</span>
                <span className="home-attention-txt">
                  <CountryFlag code={s.country} /> <b>{s.name}</b> - {note}
                </span>
                <span className="home-open">→</span>
              </button>
            ))}
          </div>
        </div>
      )}
      {/* Заглушено на время целиком: свернуто, внизу - это к сведению, не беда */}
      {wideRows.length > 0 && (
        <div className="chart-card net-card net-card-wide home-block home-attention-soft">
          <div className="home-sel-foot">
            <button className="linklike muted small" onClick={() => setWideOpen(!wideOpen)}>
              {t('заглушено на время: {list}', { list: shortList(wideRows.map((x) => x.s.name), t) })}, {wideOpen ? t('скрыть') : t('показать')}
            </button>
          </div>
          {wideOpen && (
            <>
              <div className="muted small home-attention-note">
                {t('Алерты этого вида не приходят ни по одной вещи на сервере, в том числе по новым. Где тихо сутки, заглушка снимется сама')}
              </div>
              <div className="home-attention-list">
                {wideRows.map(({ s, items }) => (
                  <div key={s.id} role="button" tabIndex={0} className="home-attention-row home-attention-muted"
                    onClick={() => onOpen('servers', s.id)}
                    onKeyDown={(ev) => ev.key === 'Enter' && ev.target === ev.currentTarget && onOpen('servers', s.id)}>
                    <span className="lk-dot" />
                    <span className="home-attention-ico">🔕</span>
                    <span className="home-attention-txt">
                      <CountryFlag code={s.country} /> <b>{s.name}</b>
                      {' - '}
                      {items.map((i, n) => {
                        const id = `${s.id}|${i.key}|${i.until ?? ''}`
                        const live = muteLiveText(i, t)
                        return (
                          <span key={id}>
                            {n > 0 && ', '}
                            {i.label}
                            <span className="muted"> ({t('до {d}', { d: whenText(i.until!) })}{live ? `, ${live}` : ''})</span>
                            {!isViewer && (
                              <>
                                {' '}
                                <button className="linklike small" disabled={!!wideBusy}
                                  title={t('Вернуть алерты этого вида на сервере')}
                                  onClick={(e) => { e.stopPropagation(); liftWide(s, i) }}>
                                  {wideBusy === id ? '...' : t('вернуть')}
                                </button>
                              </>
                            )}
                          </span>
                        )
                      })}
                    </span>
                    <span className="home-open">→</span>
                  </div>
                ))}
              </div>
              {!isViewer && wideQuiet.length > 0 && (
                <div className="home-sel-foot">
                  <button className="linklike small" disabled={!!wideBusy} onClick={liftQuiet}
                    title={wideQuiet.map(({ s, i }) => `${s.name} - ${i.label}`).join('\n')}>
                    {wideBusy === '*' ? '...' : t('вернуть все, где тихо ({n})', { n: wideQuiet.length })}
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      )}
      </div>
      {!isViewer && canSee('sites') && (
        <HomeDomainSuggestions
          key={username}
          username={username}
          onAdded={load}
          onOpenCheck={(id) => onOpen('sites', id)}
        />
      )}
    </div>
  )
}
