import { useCallback, useEffect, useState, type ReactNode } from 'react'
import {
  ApiError,
  checksOverview,
  getAgentRelease,
  alertCoverage,
  fixAlertCoverage,
  applyLocalProbe,
  getAnsibleAccess,
  listServers,
  localProbeSuggestions,
  podFinished,
  updateServer,
  type Check,
  type ChecksOverview,
  type LocalProbeSuggestion,
  type MuteWarning,
  type Server,
  dropCheckKnown,
  dropServerKnown,
  setServerKnown,
  type KnownIssue,
} from './api'
import type { Section } from './App'
import { backupAlive, backupWindowNote, dbAuditNames, knownText, dockRiskText, fmtSetupVersion, knownActive, proxyBuilt, shortList, sinceShort, srvIssues, srvKnown, knownMeta } from './serverUtils'
import { KnownDialog } from './KnownDialog'
import { MaintChip, MaintHome } from './MaintModal'
import { useI18n } from './i18n'
import { expiryText, registrableDomain } from './checkUtils'
import { useAuth } from './auth'
import { IconBackups, IconDocker, IconKube, IconServers, IconSites } from './icons'
import { CountryFlag } from './CountryFlag'
import { NetTile, type Tone } from './NetLinks'

// сколько проблемных строк показывать в карточках главной
const HOME_LIST_LIMIT = 25
const ATTENTION_LIMIT = 15
// "Что сломано": столько строк сразу, остальное - по кнопке
const BROKEN_LIMIT = 12
// иконка проблемы по ее виду - чтобы список читался, не вчитываясь
const ISSUE_ICON: Record<string, string> = {
  offline: '🔥', cpu: '🧮', mem: '🧠', disk: '🗄', temp: '🌡', throttle: '🥵', conntrack: '🔗',
  disktemp: '🌡', disk_health: '💽', disk_forecast: '📈', inode: '🗂', units: '⚙️', cpu_spin: '🌀',
  docker: '🐳', kuber: '☸️', backups: '💾', docker_sock: '🔓',
  clock: '🕐', dns: '🌐', db_conn: '🔌', web_5xx: '🌐', queue: '🐇', kube_expiry: '⏳', flux_down: '☸️',
  flux_stale: '☸️', backup_dump: '🛢', backup_dump_space: '🛢', backup_cron: '🛢', backup_check: '🩺',
  unit_loop: '🔁', kube_cron: '⏰', exposed: '🔓', port_new: '🚪', pressure: '🫁', net_health: '📶', link: '🔗', traffic: '📊', kube_node: '☸️', kube_job: '💥', access_change: '🔑', backup_restore: '🛟', docker_restarts: '🔄', kube_restarts: '🔄', mem_leak: '📈', ssh_risk: '🔐', db_health: '🛢', web_config: '🧾', web_cert: '🔏', listen_gone: '🔌', disk_slow: '🐢', access_risk: '🗝',
}

type T = (k: string, p?: Record<string, string | number>) => string
// cc — ISO-код страны ноды (бэкенд определяет по IP): в этих списках сервер
// представлен строкой, объекта под рукой нет, поэтому код кладём в сам пункт
type ActItem = {
  key: string; icon: string; section: Section; id?: number; text: string; srv?: boolean; sec?: string; cc?: string; name?: string
  // обновление по парку (агент, helper'ы, настройка юнита): отдельный блок "Обновления" над
  // "Требует действий", иначе среди длинных строк по серверам эти пункты терялись
  fleet?: boolean
  // вид проблемы: одинаковые на двух и больше серверах сворачиваются в одну строку со списком
  // серверов (раскрывается кликом); detail - что именно на этом сервере, без его имени
  grp?: string
  detail?: string
}
// name - имя сервера: по нему "Что сломано" пишет "сервер - что" (text начинается с "имя: ")
type ProbItem = {
  key: string; id?: number; down: boolean; text: string; srv?: boolean; cc?: string; name?: string
  since?: string | null // с какого момента (ISO), для "Что сломано"
  sec?: string // раздел карточки, куда ведет строка
}

// самое раннее из известных начал: у нескольких упавших контейнеров хоста - старшее
function earliest(list: (string | null | undefined)[]): string | null {
  const ok = list.filter((x): x is string => !!x).sort()
  return ok[0] ?? null
}

const K_BAD_POD = ['CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'Error', 'OOMKilled', 'CreateContainerError']

// ВЕРХНИЙ блок: только то, что требует РУЧНОГО действия на ноде — обновить агент или
// выполнить setup-скрипт (docker proxy / kube-setup / backupserver-setup). Клик ведёт
// СРАЗУ в нужный хост (там показана команда). Проблемы здоровья — в карточках ниже.
// Команда для массовой переустановки helper'ов. Панель сознательно НЕ умеет
// выкатывать их сама: helper ставится от root, и «кнопка в панели» означала бы, что
// взлом панели = root на всём парке. Поэтому здесь только список хостов, а выкатывает
// человек своим ansible — панель к нодам с root-правами не ходит.
// Сайт лежит по внешней проверке, а его домен обслуживает известная панели нода —
// почти всегда это белый список: снаружи рвут соединение, изнутри сайт цел. Панель
// уже знает и то, и другое, так что незачем заставлять человека сопоставлять это
// руками. Но и включать молча нельзя: проверка изнутри отвечает на другой вопрос,
// чем внешняя, и подменять одну другой без ведома владельца — обман.
function LocalProbeHint({
  items,
  onDone,
}: {
  items: LocalProbeSuggestion[]
  onDone: () => void
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
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
    <div className="ansible-hint">
      <button className="ghost" onClick={() => setOpen(!open)}>
        {open
          ? t('скрыть')
          : t('🏠 проверять локально: подходит {n} сайт(ов)', { n: items.length })}
      </button>
      {open && (
        <div className="local-probe-hint">
          <div className="muted small">
            {t('Эти сайты панель снаружи проверить не может — похоже на белый список. Те, чей домен держит ваш сервер, она предлагает проверять изнутри него. А если сайт ОТВЕЧАЕТ «доступ запрещён» — он жив, и довольно считать этот код нормой: изнутри там проверять нечего, прокси закрывает сайт сам.')}
          </div>
          <ul className="local-probe-list">
            {items.map((x) => (
              <li key={x.check_id}>
                <span className="mono">{x.host}</span>{' '}
                <span className="muted small">
                  {x.kind === 'code'
                    ? t('отвечает {code} — считать нормой', { code: x.code })
                    : `→ ${x.server_name}`}
                </span>{' '}
                <button className="ghost small" disabled={busy} onClick={() => apply([x.check_id])}>
                  {x.kind === 'code' ? t('принять код') : t('включить')}
                </button>
              </li>
            ))}
          </ul>
          {items.length > 1 && (
            <button
              className="ghost"
              disabled={busy}
              onClick={() => apply(items.map((x) => x.check_id))}
            >
              {t('включить для всех {n}', { n: items.length })}
            </button>
          )}
        </div>
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

function AnsibleHint({ hosts }: { hosts: string[] }) {
  const { t } = useI18n()
  const { isAdmin } = useAuth()
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  // Доступ ansible включен - плагин инвентаря сам спрашивает у панелей, где что
  // обновить, и команда одна на все панели. Иначе - список хостов этой панели.
  const [viaPanels, setViaPanels] = useState(false)
  useEffect(() => {
    if (!isAdmin) return
    getAnsibleAccess().then((a) => setViaPanels(a.enabled)).catch(() => setViaPanels(false))
  }, [isAdmin])
  const cmd = viaPanels
    ? 'ansible-playbook playbooks/kervax_helpers.yml -l kervax_outdated'
    : `ansible-playbook playbooks/kervax_helpers.yml -l '${hosts.join(',')}'`
  return (
    <div className="ansible-hint">
      <button className="ghost" onClick={() => setOpen(!open)}>
        {open ? t('скрыть') : t('🧩 обновить сразу на {n} нодах', { n: hosts.length })}
      </button>
      {open && (
        <>
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
            {viaPanels
              ? t('Одна команда на все панели: группу kervax_outdated плагин инвентаря собирает, спрашивая сами панели.')
              : isAdmin && t('Чтобы на всех панелях была одна команда без списка хостов, включите доступ для ansible: меню настроек -> Ansible.')}
          </div>
        </>
      )}
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

// Строка блока "Требует действий": пункт как есть или группа одинаковых с разных серверов
type ActRow = { one: ActItem } | { grp: string; items: ActItem[] }

function groupActs(items: ActItem[]): ActRow[] {
  const by = new Map<string, ActItem[]>()
  for (const it of items) if (it.grp) by.set(it.grp, [...(by.get(it.grp) ?? []), it])
  const rows: ActRow[] = []
  const done = new Set<string>()
  for (const it of items) {
    const list = it.grp ? by.get(it.grp)! : null
    if (!list || list.length < 2) rows.push({ one: it })
    else if (!done.has(it.grp!)) {
      done.add(it.grp!)
      rows.push({ grp: it.grp!, items: list })
    }
  }
  const bad = (r: ActRow) => ('one' in r ? r.one.icon === '⛔' : r.items.some((x) => x.icon === '⛔'))
  return rows.sort((a, b) => Number(bad(b)) - Number(bad(a)))
}

// Подпись группы: что за проблема, без имени сервера (оно в списке после)
function grpLabel(grp: string, t: T): string {
  const fixed: Record<string, string> = {
    'access-s': t('учетки с доступом, в которые давно не входят - проверьте, нужны ли они'),
    'access-w': t('слабые SSH-ключи - замените на ed25519'),
    'os-eol': t('поддержка ОС кончилась или скоро кончится'),
    'os-reboot': t('нужна перезагрузка после обновлений'),
    'os-security': t('не установлены обновления безопасности'),
    'os-apt': t('список пакетов давно не обновлялся'),
    'ssh-ssh_password': t('SSH пускает по паролю'),
    'noboot-u': t('после перезагрузки не запустятся юниты: работают, но не включены'),
    'noboot-d': t('после перезагрузки не поднимутся контейнеры без restart policy'),
    dbdump: t('базы без отдельного дампа'),
    'd-acc': t('Docker без доступа - включите read-only proxy'),
    'k-acc': t('Kubernetes без доступа - запустите kube-setup'),
    'd-old': t('устарел образ внешнего прокси - обновите'),
    'k-noagent': t('ноды кластера без агента'),
    'bs-restic': t('на сервере бэкапов старый restic - обновите'),
    'bs-setup': t('бэкап-сервер без статистики - запустите backupserver-setup'),
    dockrisk: t('у контейнеров ключи от хоста - взлом приложения в них даст root на сервере'),
  }
  return fixed[grp] ?? grp
}

function actionItems(servers: Server[], avail: string, relProblem: string, t: T): ActItem[] {
  const items: ActItem[] = []
  // Релиз агента собран криво (раздаётся не тот бинарь, что подписан) — обновлять
  // нечем, и без этой строки поломка молчит: агенты просто вечно отвергают обновление.
  if (relProblem) {
    items.push({ key: 'agent-release', icon: '⛔', section: 'servers', text: relProblem, fleet: true })
  }
  const behind = servers.filter((s) => s.online && s.agent_version && avail && s.agent_version !== avail)
  if (behind.length > 0) {
    items.push({ key: 'agent-upd', icon: '⬆', section: 'servers', fleet: true,
      text: t('Доступно обновление агента {v}: отстаёт нод — {n}', { v: avail, n: behind.length }) })
  }
  // Требования к systemd-ЮНИТУ агента (watchdog и т.п.): агент шлёт caps, бэкенд → agent_advice
  // (человекочитаемый текст). Агрегируем ПО ТЕКСТУ требования, а не по нодам — иначе один и тот
  // же «нет watchdog» размножился бы на весь парк. Раскатывается ansible-плейбуком (кнопка ниже).
  const advCount = new Map<string, number>()
  for (const s of servers) {
    if (!s.online) continue
    for (const title of s.agent_advice || []) advCount.set(title, (advCount.get(title) || 0) + 1)
  }
  for (const [title, n] of advCount) {
    items.push({ key: `adv-${title}`, icon: '🩺', section: 'servers', fleet: true,
      text: t('{what}: нод без настройки — {n}', { what: title, n }) })
  }
  // helper -> ноды, где его ставить или обновлять: одной строкой на helper, а не на ноду
  const helpers = new Map<string, { name: string; fresh: boolean; a: string; b: string; nodes: Server[] }>()
  for (const s of servers) {
    if (!s.online) continue
    const rep = s.last_report
    // Только что поставленная нода: установщик еще поднимает прокси Docker и ставит helper'ы
    // (их бэкенд в эти минуты и не требует). Без паузы плашки мигали и пропадали сами.
    if (rep?.docker?.present && !rep.docker.access && !s.fresh) {
      items.push({ key: `d-acc-${s.id}`, icon: '🐳', section: 'docker', id: s.id, name: s.name, cc: s.country, grp: 'd-acc',
        text: t('{name}: Docker без доступа — включите read-only proxy', { name: s.name }) })
    }
    if (rep?.kube?.present && !rep.kube.access && !s.fresh) {
      items.push({ key: `k-acc-${s.id}`, icon: '☸', section: 'kuber', id: s.id, name: s.name, cc: s.country, grp: 'k-acc',
        text: t('{name}: Kubernetes без доступа — запустите kube-setup', { name: s.name }) })
    }
    // Внешний прокси на старом образе: смотрит в интернет, а за год в caddy и traefik закрывают
    // не одну дыру. Ведем в Docker ноды: там написано, на что и как обновить.
    const old = s.proxy_outdated ?? []
    if (old.length > 0) {
      const list = old.map((p) => `${p.name} (${proxyBuilt(p)})`).join(', ')
      items.push({ key: `d-old-${s.id}`, icon: '🧓', section: 'docker', id: s.id, name: s.name, cc: s.country, sec: 'proxy',
        grp: 'd-old', detail: list,
        text: t('{name}: устарел образ внешнего прокси {list} - обновите', { name: s.name, list }) })
    }
    // Ноды кластера без агента: на них не видно ни дисков, ни SMART, ни упавших юнитов, ни
    // процессов (на k8s-a-prc так неделями жгли 45 ядер зависшие chrome). Ведем во
    // вкладку нод: там кнопка добавить ноду в панель или отметить, что агент не нужен.
    const unmon = rep?.kube?.access ? (s.kube_unmonitored ?? []) : []
    if (unmon.length > 0) {
      const names = unmon.slice(0, 4).map((n) => n.name).join(', ') + (unmon.length > 4 ? ', ...' : '')
      items.push({ key: `k-noagent-${s.id}`, icon: '☸', section: 'kuber', id: s.id, name: s.name, cc: s.country, sec: 'nodes',
        grp: 'k-noagent', detail: `${unmon.length} (${names})`,
        text: t('{name}: нод кластера без агента - {n} ({nodes})', { name: s.name, n: unmon.length, nodes: names }) })
    }
    // restic самого сервера бэкапов (prune, forget, check) старше той версии, до которой его
    // обновляет helper: обычно обновление не скачалось. Ведем в модалку сервера, там команда.
    if (s.bsrv_restic_old) {
      items.push({ key: `bs-restic-${s.id}`, icon: '🧓', section: 'backups', id: s.id, name: s.name, cc: s.country, srv: true, sec: 'server',
        grp: 'bs-restic', detail: `restic ${s.bsrv_restic_old}`,
        text: t('{name}: на сервере бэкапов старый restic {v} - обновите', { name: s.name, v: s.bsrv_restic_old }) })
    }
    // Идет подключение (backend app/onboard.py): алертом уходят только офлайн и критичный диск,
    // остальное ждет, пока человек посмотрит, что нашлось, и закроет подключение в карточке
    if (s.onboard) {
      items.push({ key: `onb-${s.id}`, icon: '🆕', section: 'servers', id: s.id, name: s.name, cc: s.country, grp: 'onb',
        text: t('{name}: идет подключение - посмотрите, что нашлось, и закройте его в карточке', { name: s.name }) })
    }
    // бэкап-сервер найден (по docker), но статистики репо нет → нужен backupserver-setup
    const bs = rep?.backup_server
    if (bs?.present && (!bs.repos || bs.repos.length === 0)) {
      items.push({ key: `bs-setup-${s.id}`, icon: '🗄', section: 'backups', id: s.id, name: s.name, cc: s.country, srv: true, sec: 'repos',
        grp: 'bs-setup',
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
        grp: 'dbdump', detail: db, text: t('{name}: {db} — нужен отдельный дамп', { name: s.name, db }) })
    }
    // ОС: конец поддержки, перезагрузка, обновления безопасности - по пункту на вид совета:
    // десяток нод с отложенной перезагрузкой сворачивается в одну строку "нужна перезагрузка"
    for (const x of s.os_advice ?? []) {
      items.push({ key: `os-${x.kind}-${s.id}`, icon: x.level >= 2 ? '⛔' : '🧰', section: 'servers', id: s.id,
        name: s.name, cc: s.country, sec: 'osupd', grp: `os-${x.kind}`, detail: x.text,
        text: `${s.name}: ${x.text}` })
    }
    // Чужой сервер по внешнему адресу сюда не идет: это шлюз исходящего NAT, а внешний адрес панель
    // берет из запроса агента, поправить нечего. Совет "поправьте внешний адрес" был невыполним
    // у контейнеров ключи от хоста сверх положенного: ведем в Docker, там что поправить и
    // кнопка "по назначению"
    const risk = (s.docker_risk ?? []).filter((x) => !x.muted)
    if (risk.length > 0 && !(s.alert_mutes ?? []).includes('docker_risk')) {
      const list = risk.map((x) => `${x.name} (${dockRiskText(x, t)})`).join(', ')
      items.push({ key: `dockrisk-${s.id}`, icon: '🔑', section: 'docker', id: s.id, name: s.name, cc: s.country, sec: 'host',
        grp: 'dockrisk', detail: list,
        text: t('{name}: у контейнеров ключи от хоста: {list} - взлом приложения в них даст root на сервере',
          { name: s.name, list }) })
    }
    // не переживет перезагрузку: контейнеры - в Docker (там команда), юниты - в карточку
    const nb = s.noboot_advice
    if (nb && nb.docker.length > 0)
      items.push({ key: `noboot-d-${s.id}`, icon: '🔁', section: 'docker', id: s.id, name: s.name, cc: s.country, sec: 'boot',
        grp: 'noboot-d', detail: shortList(nb.docker, t),
        text: t('{name}: после перезагрузки не поднимутся контейнеры без restart policy: {list}',
          { name: s.name, list: shortList(nb.docker, t) }) })
    if (nb && nb.units.length > 0)
      items.push({ key: `noboot-u-${s.id}`, icon: '🔁', section: 'servers', id: s.id, name: s.name, cc: s.country,
        sec: 'noboot', grp: 'noboot-u', detail: shortList(nb.units, t), text: t('{name}: после перезагрузки не запустятся {list}: работают, но не включены',
          { name: s.name, list: shortList(nb.units, t) }) })
    // учетки с доступом, в которые давно не входят, и слабые ключи
    const ac = s.access_advice
    if (ac && ac.stale.length > 0)
      items.push({ key: `access-s-${s.id}`, icon: '🗝', section: 'servers', id: s.id, name: s.name, cc: s.country,
        sec: 'access', grp: 'access-s', detail: shortList(ac.stale, t), text: t('{name}: учетки с доступом, в которые давно не входят: {list} - проверьте, нужны ли они',
          { name: s.name, list: shortList(ac.stale, t) }) })
    if (ac && ac.weak.length > 0)
      items.push({ key: `access-w-${s.id}`, icon: '🗝', section: 'servers', id: s.id, name: s.name, cc: s.country,
        sec: 'access', grp: 'access-w', detail: shortList(ac.weak, t), text: t('{name}: слабые SSH-ключи у {list} - замените на ed25519',
          { name: s.name, list: shortList(ac.weak, t) }) })
    // SSH пускает по паролю, а порт открыт в интернет
    for (const a of s.ssh_advice ?? []) {
      items.push({ key: `ssh-${s.id}`, icon: a.level >= 2 ? '⛔' : '🔐', section: 'servers', id: s.id,
        name: s.name, cc: s.country, sec: 'ssh', grp: `ssh-${a.kind}`, detail: a.text, text: `${s.name}: ${a.text}` })
    }
    for (const h of s.helper_advice || []) {
      // «устарел (? → v0.1)» на ноде, где helper'а нет вовсе, вводит в заблуждение:
      // это не обновление, а первая установка, и делается она тем же прогоном.
      const fresh = !h.installed
      const k = `${h.name}|${fresh ? '' : fmtSetupVersion(h.installed)}|${fmtSetupVersion(h.current)}`
      const g = helpers.get(k) ?? { name: h.name, fresh, a: fmtSetupVersion(h.installed), b: fmtSetupVersion(h.current), nodes: [] }
      g.nodes.push(s)
      helpers.set(k, g)
    }
  }
  // одна нода - как раньше, с ее именем; клик ведет в карточку, там команда переустановки
  for (const [k, g] of helpers) {
    const s = g.nodes[0]
    const many = g.nodes.length > 1
    const names = shortList(g.nodes.map((x) => x.name), t, 4)
    items.push({ key: `hlp-${k}`, icon: '🧩', section: 'servers', id: s.id, name: many ? undefined : s.name,
      cc: many ? undefined : s.country, fleet: true,
      text: g.fresh
        ? many
          ? t('helper «{helper}» не установлен ({b}): нод - {n} ({list})', { helper: g.name, b: g.b, n: g.nodes.length, list: names })
          : t('{name}: helper «{helper}» не установлен ({b}) — поставьте', { name: s.name, helper: g.name, b: g.b })
        : many
          ? t('helper «{helper}» устарел ({a} → {b}): нод - {n} ({list})', { helper: g.name, a: g.a, b: g.b, n: g.nodes.length, list: names })
          : t('{name}: helper «{helper}» устарел ({a} → {b}) — переустановите', { name: s.name, helper: g.name, a: g.a, b: g.b }) })
  }
  return items
}

// проблемы Docker: упавшие/циклящиеся контейнеры (по хостам)
function dockerProblems(servers: Server[], t: T): ProbItem[] {
  const out: ProbItem[] = []
  for (const s of servers) {
    const d = s.online ? s.last_report?.docker : undefined
    if (!d?.access || !d.containers) continue
    // Что сломано - решает бэкенд, как и в разделе "Докер" (docker_alerts - по чему ушел алерт):
    // упавший - должен работать и не поднялся за выдержку, крутящийся - счетчик перезапусков
    // растет прямо сейчас. Сам счетчик копится за всю жизнь контейнера: семь перезапусков могли
    // быть месяц назад или руками, а главная по нему поднимала ложную тревогу.
    const al = s.docker_alerts ?? {}
    const down = d.containers.filter((c) => al[c.name] === 'down').map((c) => c.name)
    const loop = d.containers.filter((c) => al[c.name] === 'loop').map((c) => c.name)
    if (down.length > 0)
      out.push({
        key: `d-down-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, sec: 'containers',
        since: earliest(down.map((n) => s.docker_since?.[n])),
        text: t(down.length === 1 ? '{name}: не работает контейнер {list}' : '{name}: не работают контейнеры {list}',
          { name: s.name, list: down.join(', ') }),
      })
    if (loop.length > 0)
      out.push({
        key: `d-loop-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, sec: 'containers',
        text: t(loop.length === 1 ? '{name}: перезапускается в цикле {list}' : '{name}: перезапускаются в цикле {list}',
          { name: s.name, list: loop.join(', ') }),
      })
    // внешний прокси с docker-сокетом: не авария, а дыра - предупреждением, клик ведет в Docker,
    // где написано, как поправить
    const ex = s.docker_exposed ?? []
    if (ex.length > 0 && !(s.alert_mutes ?? []).includes('docker_sock'))
      out.push({
        key: `d-sock-${s.id}`, id: s.id, name: s.name, cc: s.country, down: false, sec: 'host',
        text: t('{name}: docker-сокет у {list} - взлом прокси даст root на хосте',
          { name: s.name, list: ex.map((e) => e.name).join(', ') }),
      })
  }
  return out
}

// проблемы Kube: NotReady ноды + проблемные поды (по кластерам)
function kubeProblems(servers: Server[], t: T): ProbItem[] {
  const out: ProbItem[] = []
  for (const s of servers) {
    const k = s.online ? s.last_report?.kube : undefined
    if (!k?.access) continue
    const nr = (k.nodes || []).filter((n) => !n.ready).length
    const bad = (k.pods || []).filter((p) => !podFinished(p) && (p.phase === 'Failed' || (p.reason ? K_BAD_POD.includes(p.reason) : false))).length
    if (nr > 0) out.push({ key: `k-node-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, sec: 'nodes', text: t('{name}: нод NotReady — {n}', { name: s.name, n: nr }) })
    if (bad > 0) out.push({ key: `k-pod-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, sec: 'pods', since: s.alert_since?.kube_pod, text: t('{name}: проблемных подов — {n}', { name: s.name, n: bad }) })
  }
  return out
}

// проблемы бэкапов: клиенты (ошибка/несвежесть) + серверы (стоп/битые репо)
function backupProblems(servers: Server[], t: T): ProbItem[] {
  const out: ProbItem[] = []
  for (const s of servers) {
    if (!s.online) continue
    const b = s.last_report?.backup
    if (b?.present && b.metric_present) {
      if (b.success === 0) out.push({ key: `b-fail-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, sec: 'runs', since: s.alert_since?.backup_failed, text: t('{name}: бэкап завершился с ошибкой', { name: s.name }) })
      else if (b.last_backup_ts && Date.now() / 1000 - b.last_backup_ts > 2 * 86400) out.push({ key: `b-stale-${s.id}`, id: s.id, name: s.name, cc: s.country, down: false, sec: 'runs', since: s.alert_since?.backup_stale, text: t('{name}: бэкап не свежий', { name: s.name }) })
    }
    const bs = s.last_report?.backup_server
    // свои бэкапы ноды (настроены без панели): упал прогон, давно не отрабатывал, выключен
    const own = (s.custom_backups ?? []).filter((j) => ['failed', 'stale', 'disabled'].includes(j.status))
    if (own.length > 0) {
      out.push({ key: `b-own-${s.id}`, id: s.id, name: s.name, cc: s.country, sec: 'own', down: own.some((j) => j.status === 'failed'), since: s.alert_since?.backup_custom,
        text: t('{name}: свой бэкап не отрабатывает — {jobs}', {
          name: s.name, jobs: own.map((j) => `${j.name} (${j.problem})`).join(', '),
        }) })
    }
    // прогон добавил в разы больше обычного (helper backup-setup 0.32, считает бэкенд)
    const g = s.backup_growth
    if (g && !(s.alert_mutes ?? []).includes('backup_growth'))
      out.push({ key: `b-growth-${s.id}`, id: s.id, name: s.name, cc: s.country, down: false, sec: 'runs',
        since: new Date(g.ts * 1000).toISOString(), text: `${s.name}: ${g.text}` })
    const ownFiles = (s.custom_backups ?? []).some((j) => j.files && !j.ignored)
    // сервер без настроенного бэкапа (и не помеченный «не требуется», и не сам бэкап-сервер)
    if (!bs?.present && !(b?.configured || b?.metric_present) && !s.backup_not_required && !ownFiles) {
      out.push({ key: `b-none-${s.id}`, id: s.id, name: s.name, cc: s.country, down: false, since: s.alert_since?.backup_missing, text: t('{name}: бэкап не настроен', { name: s.name }) })
    }
    if (bs?.present) {
      if (!bs.running) out.push({ key: `bs-stop-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, srv: true, sec: 'server', text: t('{name}: rest-server остановлен', { name: s.name }) })
      const rmuted = new Set(s.backup_repo_mutes ?? [])
      const smuted = new Set(s.alert_mutes ?? [])
      const now = Date.now() / 1000
      const live = (bs.repos || []).filter((r) => !rmuted.has(r.name))
      // Висячий лок дольше суток - своей строкой, как и свой алерт backup_lock: в общем
      // счетчике было не видно ни что это лок, ни что из-за него не идет чистка. Лок идущего
      // бэкапа и висячий моложе суток проблемой не считаются (их снимут чистка и клиент).
      const locked = live.filter((r) => r.locked && r.lock_ts && now - r.lock_ts > 86400)
      if (locked.length > 0 && !smuted.has('backup_lock'))
        out.push({
          key: `bs-lock-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, srv: true, sec: 'repos',
          since: new Date(Math.min(...locked.map((r) => r.lock_ts ?? now)) * 1000).toISOString(),
          text: t('{name}: залочен больше суток, чистка не идет - {list}', {
            name: s.name,
            list: locked.map((r) => `${r.name} (${Math.floor((now - (r.lock_ts ?? now)) / 86400)} ${t('дн.')})`).join(', '),
          }),
        })
      // те же репозитории, что в алерте backup_repo: бэкенд считает их с ритмом бэкапов (клиент
      // без агента опаздывает через сутки с небольшим), без приглушенных и архива
      const badNames = Object.keys(s.bsrv_repo_reasons ?? {}).sort()
      if (badNames.length > 0) out.push({ key: `bs-repo-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, srv: true, sec: 'repos', since: s.alert_since?.backup_repo,
        text: t('{name}: репозиториев с проблемой - {n} ({list})', { name: s.name, n: badNames.length,
          list: badNames.slice(0, 3).join(', ') + (badNames.length > 3 ? ', ...' : '') }) })
      // чистка не работает (не открывает репозиторий, падает, не запускается): те же
      // репозитории, что в алерте backup_prune, считает бэкенд
      const pruneNames = Object.keys(s.bsrv_prune ?? {}).sort()
      if (pruneNames.length > 0 && !smuted.has('backup_prune'))
        out.push({
          key: `bs-prune-${s.id}`, id: s.id, name: s.name, cc: s.country, down: true, srv: true, sec: 'cleanup',
          text: t('{name}: чистка не работает - {list}', {
            name: s.name, list: pruneNames.slice(0, 3).join(', ') + (pruneNames.length > 3 ? ', ...' : ''),
          }),
        })
      // ротация встала: считает бэкенд теми же правилами, что и алерт backup_rotation
      const rot = s.bsrv_rotation ?? []
      if (rot.length > 0 && !smuted.has('backup_rotation'))
        out.push({
          key: `bs-rot-${s.id}`, id: s.id, name: s.name, cc: s.country, down: false, srv: true, sec: 'cleanup',
          text: t('{name}: старые снапшоты не вычищаются - {list}', { name: s.name, list: rot.join(', ') }),
        })
    }
  }
  return out
}

type Props = {
  onNavigate: (s: Section) => void
  onOpen: (s: Section, id?: number, srv?: boolean, sec?: string) => void
  onUnauthorized: () => void
}

// more — сколько ЕЩЁ мониторов висит на том же истекающем имени
type Warn = {
  id: number
  name: string
  kind: 'ssl' | 'domain'
  days: number
  more?: number
}

const maxWarn = (w: number[]) => (w.length ? Math.max(...w) : 0)

function expiryWarnings(checks: Check[]): Warn[] {
  const out: Warn[] = []
  // Домен продлевают ЦЕЛИКОМ, поэтому истекает он один, а мониторов на его
  // поддоменах может быть сколько угодно: пять строк «gitlab/y/mmbot/msg/env
  // .example.com · домен · 4 дн.» — это один и тот же example.com, и список из-за
  // них выглядит как пять разных проблем. Схлопываем в строку про сам домен.
  // Сертификаты так не сворачиваем: у каждого хоста он обычно свой, и wildcard
  // от отдельных по данным монитора не отличить.
  const byDomain = new Map<string, Warn>()
  for (const c of checks) {
    if (c.check_ssl && c.ssl_days != null && c.ssl_days <= maxWarn(c.ssl_warn_days))
      out.push({ id: c.id, name: c.name, kind: 'ssl', days: c.ssl_days })
    if (c.check_domain && c.domain_days != null && c.domain_days <= maxWarn(c.domain_warn_days)) {
      const zone = registrableDomain(c.target || c.name)
      const seen = byDomain.get(zone)
      if (seen) {
        seen.more = (seen.more ?? 0) + 1
        // ведём в монитор с наименьшим сроком — он и есть самый срочный
        if (c.domain_days < seen.days) {
          seen.days = c.domain_days
          seen.id = c.id
        }
        continue
      }
      const w: Warn = { id: c.id, name: zone, kind: 'domain', days: c.domain_days }
      byDomain.set(zone, w)
      out.push(w)
    }
  }
  return out.sort((a, b) => a.days - b.days)
}

export function HomePage({ onNavigate, onOpen, onUnauthorized }: Props) {
  const { t } = useI18n()
  // Главная — витрина всех разделов, поэтому она обязана уважать нарезку доступа:
  // иначе учётка без «Бэкапов» видит на ней и карточку бэкапов, и пункты, ведущие
  // в закрытый раздел (клик по ним всё равно упрётся в 403).
  const { sections, isViewer } = useAuth()
  const canSee = useCallback(
    (sec: Section) => sections.length === 0 || sections.includes(sec),
    [sections],
  )
  const [ov, setOv] = useState<ChecksOverview | null>(null)
  const [servers, setServers] = useState<Server[] | null>(null)
  // отметить проблему сервера известной (📌 в "Что сломано")
  const [knownFor, setKnownFor] = useState<{ id: number; kind: string; title: string } | null>(null)
  const [avail, setAvail] = useState('')
  const [relProblem, setRelProblem] = useState('')
  const [probeHints, setProbeHints] = useState<LocalProbeSuggestion[]>([])
  const [mutes, setMutes] = useState<MuteWarning[]>([])
  const [brokenAll, setBrokenAll] = useState(false)
  // "Требует действий": раскрытые группы и показ всех строк сверх ATTENTION_LIMIT
  const [openGrps, setOpenGrps] = useState<Set<string>>(() => new Set())
  const [actAll, setActAll] = useState(false)
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
    checksOverview()
      .then(setOv)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
      })
    listServers()
      .then(setServers)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
      })
    // предложения «проверять локально»: тихо, без ошибок в интерфейсе — это подсказка,
    // а не состояние системы, и её недоступность не должна ничего ломать
    localProbeSuggestions()
      .then((r) => setProbeHints(r.items))
      .catch(() => setProbeHints([]))
    // объекты, по которым не сработает ни один алерт. Доступно только админу
    // (область действия — часть настроек алертов), остальным просто молчим
    alertCoverage()
      .then((r) => setMutes(r.items))
      .catch(() => setMutes([]))
  }, [onUnauthorized])

  // быстрый мут прямо с главной: приглушить типы алертов сервера навсегда (alert_mutes).
  // Для диска warn ключ 'disk@1' — крит и проблема продолжат алертить (см. _muted).
  const quickMute = useCallback((id: number, keys: string[]) => {
    const s = (servers ?? []).find((x) => x.id === id)
    if (!s) return
    const next = [...new Set([...(s.alert_mutes ?? []), ...keys])]
    updateServer(id, { alert_mutes: next }).then(load).catch(() => {})
  }, [servers, load])

  useEffect(() => {
    load()
    const id = window.setInterval(load, 15000)
    return () => window.clearInterval(id)
  }, [load])

  const warns = ov ? expiryWarnings(ov.checks) : []
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
        !knownActive(c.known_issue),
    )
    .sort((a, b) => (a.last_status === 'down' ? 0 : 1) - (b.last_status === 'down' ? 0 : 1))
  // лежащие, но отмеченные известной проблемой: "все в норме" про них было бы неправдой
  const knownDown = (ov?.checks ?? []).filter(
    (c) =>
      c.enabled && knownActive(c.known_issue) &&
      (c.last_status === 'down' || c.last_status === 'degraded' || !!c.flaky_since),
  ).length
  const srvTotal = servers?.length ?? 0
  const srvOnline = servers?.filter((s) => s.online).length ?? 0
  // проблемные серверы (оффлайн + перегрузка ресурсов) — что именно не так
  // проблема = есть t-down (оффлайн / ≥порога alert/crit); предупреждение = только
  // t-degraded (warn, напр. диск 85%). Раньше всё валилось в «Проблемы» скопом.
  // Проблемы других разделов (сроки и Flux, дампы, очереди) сюда не идут: их считают карточки
  // своих разделов (sectionProbs), иначе одна беда попадала бы в два счетчика.
  const srvProblems = (servers ?? [])
    .map((s) => {
      const iss = srvIssues(s, t).filter((i) => !i.section)
      if (!iss.length) return null
      const down = iss.some((i) => i.tone === 't-down')
      // ключи мута для приглушения (у предупреждений — свои, напр. disk@1)
      const mutes = [...new Set(iss.map((i) => i.mute).filter(Boolean) as string[])]
      return {
        id: s.id, name: s.name, down, cc: s.country,
        text: iss.map((i) => i.text).join(', '),
        mutes,
      }
    })
    .filter((x): x is NonNullable<typeof x> => x != null)
  const srvDown = srvProblems.filter((p) => p.down)
  const srvWarn = srvProblems.filter((p) => !p.down)

  const srvList = servers ?? []
  const allActions = actionItems(srvList, avail, relProblem, t).filter((x) => canSee(x.section))
  const fleet = allActions.filter((x) => x.fleet)
  const actions = allActions.filter((x) => !x.fleet)
  // по разделам: на серверах - сверху, бэкапное - ниже; внутри одинаковое свернуто в группы
  const actGroups = [
    { key: 'srv', label: t('На серверах'), rows: groupActs(actions.filter((x) => x.section !== 'backups')) },
    { key: 'bak', label: t('По бэкапам'), rows: groupActs(actions.filter((x) => x.section === 'backups')) },
  ]
  const actRowsN = actGroups.reduce((n, g) => n + g.rows.length, 0)
  const dockerHosts = srvList.filter((s) => s.last_report?.docker?.present && s.last_report.docker.access)
  const dockerRunning = dockerHosts.reduce((n, s) => n + (s.last_report?.docker?.containers?.filter((c) => c.state === 'running').length ?? 0), 0)
  const dockerProbs = dockerProblems(srvList, t)
  const kubeHosts = srvList.filter((s) => s.last_report?.kube?.present && s.last_report.kube.access)
  const kubePods = kubeHosts.reduce((n, s) => n + (s.last_report?.kube?.pods?.filter((p) => p.phase === 'Running').length ?? 0), 0)
  const kubeProbs = kubeProblems(srvList, t)
  const backupClients = srvList.filter((s) => backupAlive(s.last_report?.backup))
  const backupServers = srvList.filter((s) => s.last_report?.backup_server?.present)
  const backupProbs = backupProblems(srvList, t)
  // проблемы с бэкенда, которые относятся к разделу (сроки и Flux - к "Куберу", дампы и проверки
  // репозиториев - к "Бэкапам"): только для счетчика карточки, в "Что сломано" они идут общим списком
  const sectionProbs = (sec: Section): ProbItem[] =>
    srvList.flatMap((s) => srvIssues(s, t).filter((i) => i.section === sec).map((i, n) => ({
      key: `${sec}-${s.id}-${n}`, id: s.id, name: s.name, down: i.tone === 't-down', text: i.text, srv: i.srv,
    })))

  // "Что сломано": все проблемы одним списком, по строке на каждую - серверы (включая
  // поломки дисков, прогноз, inode, упавшие юниты), Docker, Kubernetes и бэкапы. Клик ведет
  // туда, где это чинится: в раздел карточки сервера или в карточку Docker/Кубера/бэкапа.
  // Проблемы сверху, предупреждения ниже; внутри - по имени сервера, чтобы беды одной ноды
  // стояли рядом. Списки в карточках разделов ниже убраны: там остались счетчики.
  type Broken = {
    key: string; id: number; name: string; cc?: string; tone: 't-down' | 't-degraded'; text: string
    kind: string; section: Section; srv?: boolean; sec?: string; mute?: string; since?: string | null
    note?: string
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
          key: `${s.id}-${n}`, id: s.id, name: s.name, cc: s.country, tone: i.tone,
          text: i.kind === 'offline' && behindSilent(s)
            ? `${i.text} · ⛓ ${t('за {srv}, он тоже молчит', { srv: behindSilent(s) })}`
            : i.text,
          kind: i.kind ?? 'server', section: (i.section ?? 'servers') as Section, srv: i.srv ?? true,
          sec: i.sec, mute: i.mute, since: i.since, note: s.note,
          markable: !!i.kind && i.kind !== 'offline' && !!s.alert_since?.[i.kind],
        }))
        .filter((i) => canSee(i.section)),
    ),
    ...fromProbs(dockerProbs, 'docker'),
    ...fromProbs(kubeProbs, 'kuber'),
    ...fromProbs(backupProbs, 'backups'),
  ].sort((a, b) => (a.tone === b.tone ? a.name.localeCompare(b.name) : a.tone === 't-down' ? -1 : 1))
  // предупреждения с одинаковой шапкой с двух и больше серверов - одна строка; красное - как есть
  type BrokenRow = { one: Broken } | { grp: string; head: string; items: Broken[] }
  const brokenKey = (b: Broken) => (b.tone === 't-down' ? '' : `${b.kind}|${splitHead(b.text)[0]}`)
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
    (it.check ? dropCheckKnown(it.id) : dropServerKnown(it.id, it.kind)).then(load).catch(() => {})

  // Хосты для ansible-кнопки «обновить сразу на N нодах»: те, что плейбук реально чинит —
  // устаревший helper (переустановка) ИЛИ не включён watchdog (helper agent-watchdog, ALWAYS).
  const rolloutHosts = [...new Set(
    srvList
      .filter((s) => s.online && s.helper_rollout)
      .map((s) => s.hostname || s.name),
  )].sort()
  // строка "Что сломано"; sub - внутри раскрытой группы: сервер и подробности без шапки
  const brokenRow = (it: Broken, sub = false) => (
    <button
      key={it.key}
      className={`home-attention-row${sub ? ' home-attention-subrow' : ''}`}
      onClick={() => onOpen(it.section, it.id, it.srv, it.sec)}
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
      {it.markable && !isViewer && (
        <span className="quick-mute" title={t('Известная проблема: алерт молчит, пока не станет хуже')}
          onClick={(e) => {
            e.stopPropagation()
            setKnownFor({ id: it.id, kind: it.kind, title: `${it.name} - ${it.text}` })
          }}>
          📌
        </span>
      )}
      {it.mute && (
        <span className="quick-mute" title={t('Приглушить')}
          onClick={(e) => { e.stopPropagation(); quickMute(it.id, [it.mute!]) }}>
          🔕
        </span>
      )}
      <span className="home-open">→</span>
    </button>
  )
  const brokenGrpRow = (r: BrokenRow) => {
    if ('one' in r) return brokenRow(r.one)
    const open = openGrps.has(`b-${r.grp}`)
    const names = shortList([...new Set(r.items.map((x) => x.name))], t, 4)
    return (
      <div key={`bg-${r.grp}`} className={`home-attention-grpbox${open ? ' open' : ''}`}>
        <button className="home-attention-row" onClick={() => toggleGrp(`b-${r.grp}`)}>
          <span className="lk-dot lk-dot-warn" />
          <span className="home-attention-ico">{ISSUE_ICON[r.items[0].kind] ?? '⚠️'}</span>
          <span className="home-attention-txt">
            {r.head}
            <span className="muted"> · {t('серверов: {n}', { n: new Set(r.items.map((x) => x.id)).size })} ({names})</span>
          </span>
          <span className="home-open">{open ? '▾' : '▸'}</span>
        </button>
        {open && <div className="home-attention-sub">{r.items.map((x) => brokenRow(x, true))}</div>}
      </div>
    )
  }
  const actRow = (it: ActItem) => (
    <button key={it.key} className="home-attention-row" onClick={() => onOpen(it.section, it.id, it.srv, it.sec)}>
      <span className={`lk-dot ${it.fleet ? 'lk-dot-info' : 'lk-dot-warn'}`} />
      <span className="home-attention-ico">{it.icon}</span>
      <span className="home-attention-txt">
        <CountryFlag code={it.cc} /> {it.text}
      </span>
      <span className="home-open">→</span>
    </button>
  )
  // строка внутри раскрытой группы: сервер и что на нем, без повтора самой проблемы
  const subRow = (it: ActItem) => (
    <button key={it.key} className="home-attention-row home-attention-subrow"
      onClick={() => onOpen(it.section, it.id, it.srv, it.sec)}>
      <span className="home-attention-txt">
        <CountryFlag code={it.cc} /> <b>{it.name}</b>
        {it.detail ? <span className="muted"> - {it.detail}</span> : null}
      </span>
      <span className="home-open">→</span>
    </button>
  )
  const grpRow = (r: ActRow) => {
    if ('one' in r) return actRow(r.one)
    const open = openGrps.has(r.grp)
    const names = shortList(r.items.map((x) => x.name ?? ''), t, 4)
    const icon = r.items.some((x) => x.icon === '⛔') ? '⛔' : r.items[0].icon
    return (
      <div key={`g-${r.grp}`} className={`home-attention-grpbox${open ? ' open' : ''}`}>
        <button className="home-attention-row" onClick={() => toggleGrp(r.grp)}>
          <span className="lk-dot lk-dot-warn" />
          <span className="home-attention-ico">{icon}</span>
          <span className="home-attention-txt">
            {grpLabel(r.grp, t)}
            <span className="muted"> · {t('серверов: {n}', { n: r.items.length })} ({names})</span>
          </span>
          <span className="home-open">{open ? '▾' : '▸'}</span>
        </button>
        {open && <div className="home-attention-sub">{r.items.map(subRow)}</div>}
      </div>
    )
  }
  // Сводка сверху: плитка на раздел, клик ведет в него. Тон плитки - по худшему в разделе:
  // красная - есть настоящая беда, желтая - только предупреждения
  const probTone = (list: ProbItem[]): Tone => (list.some((p) => p.down) ? 'bad' : list.length ? 'warn' : 'ok')
  const subOf = (...parts: (string | false | 0 | undefined)[]) => parts.filter(Boolean).join(' · ')
  const tile = (sec: Section, icon: ReactNode, label: string, value: string, sub: string, tone: Tone, unit?: string) => (
    <button key={sec} className="srv-tile-btn" onClick={() => onNavigate(sec)} title={t('Открыть')}>
      <NetTile label={<>{icon}{label}</>} value={value} unit={unit} sub={sub} tone={tone} />
    </button>
  )
  const siteDown = downChecks.filter((c) => c.last_status === 'down').length
  const kubeAll = [...kubeProbs, ...sectionProbs('kuber')]
  const backupAll = [...backupProbs, ...sectionProbs('backups')]
  const tiles: ReactNode[] = []
  if (canSee('sites'))
    tiles.push(
      ov == null || ov.total === 0
        ? tile('sites', <IconSites />, t('Сайты работают'), '—', ov == null ? t('загрузка…') : t('Мониторов пока нет.'), 'muted')
        : tile(
            'sites', <IconSites />, t('Сайты работают'), String(ov.up),
            subOf(
              downChecks.length ? t('проблем: {n}', { n: downChecks.length }) : !knownDown && t('все в норме'),
              knownDown && t('известных: {n}', { n: knownDown }),
              ov.open_incidents > 0 && t('{n} откр. инцидентов', { n: ov.open_incidents }),
            ),
            siteDown ? 'bad' : downChecks.length ? 'warn' : 'ok',
            t('из {n}', { n: ov.total }),
          ),
    )
  if (canSee('servers'))
    tiles.push(
      servers == null || srvTotal === 0
        ? tile('servers', <IconServers />, t('Серверы онлайн'), '—', servers == null ? t('загрузка…') : t('Серверов пока нет.'), 'muted')
        : tile(
            'servers', <IconServers />, t('Серверы онлайн'), String(srvOnline),
            // оффлайн-нода уже сидит в srvDown (srvIssues отдает ей "оффлайн"), отдельно не плюсуем
            subOf(
              srvDown.length > 0 && t('проблем: {n}', { n: srvDown.length }),
              srvWarn.length > 0 && t('предупреждений: {n}', { n: srvWarn.length }),
              !srvProblems.length && t('все в норме'),
            ),
            srvDown.length ? 'bad' : srvWarn.length ? 'warn' : 'ok',
            t('из {n}', { n: srvTotal }),
          ),
    )
  // Docker, Кубер и бэкапы - только если в разделе есть что показать
  if (canSee('docker') && dockerHosts.length)
    tiles.push(tile(
      'docker', <IconDocker />, t('Контейнеры Docker'), String(dockerRunning),
      subOf(t('хостов: {n}', { n: dockerHosts.length }), dockerProbs.length > 0 && t('проблем: {n}', { n: dockerProbs.length })),
      probTone(dockerProbs),
    ))
  if (canSee('kuber') && kubeHosts.length)
    tiles.push(tile(
      'kuber', <IconKube />, t('Поды Kubernetes'), String(kubePods),
      subOf(t('кластеров: {n}', { n: kubeHosts.length }), kubeAll.length > 0 && t('проблем: {n}', { n: kubeAll.length })),
      probTone(kubeAll),
    ))
  if (canSee('backups') && backupClients.length + backupServers.length)
    tiles.push(tile(
      'backups', <IconBackups />, t('Серверов под бэкапом'), String(backupClients.length),
      subOf(
        backupServers.length > 0 && t('серверов бэкапа: {n}', { n: backupServers.length }),
        backupAll.length > 0 ? t('проблем: {n}', { n: backupAll.length }) : t('все свежие'),
      ),
      probTone(backupAll),
    ))
  // бэкап вышел за ночное окно: мягкое уведомление (НЕ алерт, не в Telegram)
  const late = canSee('backups')
    ? srvList.map((s) => ({ s, note: backupWindowNote(s) })).filter((x) => x.note)
    : []
  const siteRows = canSee('sites') ? downChecks.length + warns.length : 0
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
      {tiles.length > 0 && <div className="net-tiles home-tiles">{tiles}</div>}
      <div className="net-cards home-blocks">
      {mutes.length > 0 && <MuteHint items={mutes} onOpen={onOpen} onDone={load} />}
      {broken.length > 0 && (
        <div className={`chart-card net-card net-card-wide home-block home-attention-broken${broken.some((x) => x.tone === 't-down') ? ' has-down' : ''}`}>
          {cap(t('Что сломано'), brokenRows.length, broken.some((x) => x.tone === 't-down') ? 'bad' : 'warn')}
          <div className="home-attention-list">
            {(brokenAll ? brokenRows : brokenRows.slice(0, BROKEN_LIMIT)).map(brokenGrpRow)}
            {brokenRows.length > BROKEN_LIMIT && (
              <button className="ghost small home-attention-more" onClick={() => setBrokenAll(!brokenAll)}>
                {brokenAll ? t('свернуть') : t('…и ещё {n}', { n: brokenRows.length - BROKEN_LIMIT })}
              </button>
            )}
          </div>
        </div>
      )}
      {/* Сайты: лежащие мониторы и истекающие SSL/домены. Клик - в сам монитор */}
      {siteRows > 0 && (
        <div className={`chart-card net-card home-block${siteDown ? ' has-down' : ''}`}>
          {cap(t('Сайты'), siteRows, siteDown ? 'bad' : 'warn')}
          <div className="home-attention-list">
            {downChecks.slice(0, HOME_LIST_LIMIT).map((c) => (
              <button key={c.id} className="home-attention-row" onClick={() => onOpen('sites', c.id)}>
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
                <span className="home-open">→</span>
              </button>
            ))}
            {downChecks.length > HOME_LIST_LIMIT && (
              <button className="ghost small home-attention-more" onClick={() => onNavigate('sites')}>
                {t('…ещё {n}', { n: downChecks.length - HOME_LIST_LIMIT })}
              </button>
            )}
            {warns.slice(0, HOME_LIST_LIMIT).map((w) => (
              <button key={`${w.kind}-${w.id}`} className="home-attention-row" onClick={() => onOpen('sites', w.id)}>
                <span className={`lk-dot ${w.days <= 3 ? 'lk-dot-bad' : 'lk-dot-warn'}`} />
                <span className="home-attention-ico">{w.kind === 'ssl' ? '🔐' : '🌐'}</span>
                <span className="home-attention-txt">
                  <b>{w.name}</b>
                  <span className="muted">
                    {' - '}
                    {w.kind === 'ssl' ? t('SSL') : t('домен')} · {expiryText(w.days, t)}
                    {w.more ? ` · ${t('мониторов: {n}', { n: w.more + 1 })}` : ''}
                  </span>
                </span>
                <span className="home-open">→</span>
              </button>
            ))}
            {warns.length > HOME_LIST_LIMIT && (
              <button className="ghost small home-attention-more" onClick={() => onNavigate('sites')}>
                {t('…ещё {n}', { n: warns.length - HOME_LIST_LIMIT })}
              </button>
            )}
          </div>
        </div>
      )}
      <MaintHome />
      {knownRows.length > 0 && (
        <div className="chart-card net-card home-block home-attention-known">
          {cap(t('Известные проблемы'), knownRows.length)}
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
        </div>
      )}
      {probeHints.length > 0 && (
        <div className="chart-card net-card home-block home-attention-probe">
          {cap(t('Похоже на белый список'), probeHints.length)}
          <LocalProbeHint items={probeHints} onDone={load} />
        </div>
      )}
      {(fleet.length > 0 || (canSee('servers') && rolloutHosts.length > 0)) && (
        <div className="chart-card net-card home-block home-attention-fleet">
          {cap(t('Обновления'), fleet.length, 'info')}
          {fleet.length > 0 && <div className="home-attention-list">{fleet.map(actRow)}</div>}
          {canSee('servers') && rolloutHosts.length > 0 && <AnsibleHint hosts={rolloutHosts} />}
        </div>
      )}
      {actions.length > 0 && (
        <div className="chart-card net-card net-card-wide home-block">
          {cap(t('Требует действий'), actRowsN, 'warn')}
          <div className="home-attention-list">
            {/* Делим по тому, КУДА ведёт пункт: чинится на сервере (агент, helper'ы,
                доступы) — сверху; бэкапное — ниже, оно обычно менее срочное. */}
            {actGroups.map((grp) =>
              grp.rows.length === 0 ? null : (
                <div key={grp.key} className="home-attention-grp">
                  {/* заголовок только если есть ОБЕ группы — иначе он лишний шум */}
                  {actGroups.every((g) => g.rows.length > 0) && (
                    <div className="home-attention-grp-head muted small">{grp.label}</div>
                  )}
                  {(actAll ? grp.rows : grp.rows.slice(0, ATTENTION_LIMIT)).map(grpRow)}
                  {grp.rows.length > ATTENTION_LIMIT && (
                    <button className="ghost small home-attention-more" onClick={() => setActAll(!actAll)}>
                      {actAll ? t('свернуть') : t('…и ещё {n}', { n: grp.rows.length - ATTENTION_LIMIT })}
                    </button>
                  )}
                </div>
              ),
            )}
          </div>
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
      </div>
    </div>
  )
}
