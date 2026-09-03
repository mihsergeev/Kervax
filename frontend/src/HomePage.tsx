import { useCallback, useEffect, useState, type ReactNode } from 'react'
import {
  ApiError,
  checksOverview,
  getAgentRelease,
  alertCoverage,
  fixAlertCoverage,
  applyLocalProbe,
  listServers,
  localProbeSuggestions,
  podFinished,
  updateServer,
  type Check,
  type ChecksOverview,
  type LocalProbeSuggestion,
  type MuteWarning,
  type Server,
} from './api'
import type { Section } from './App'
import { backupWindowNote, fmtSetupVersion, srvIssues } from './serverUtils'
import { useI18n } from './i18n'
import { expiryText, registrableDomain } from './checkUtils'
import { useAuth } from './auth'
import { IconBackups, IconDocker, IconKube, IconServers, IconSites } from './icons'
import { CountryFlag } from './CountryFlag'

// сколько проблемных строк показывать в карточках главной
const HOME_LIST_LIMIT = 25
const ATTENTION_LIMIT = 15

type T = (k: string, p?: Record<string, string | number>) => string
// cc — ISO-код страны ноды (бэкенд определяет по IP): в этих списках сервер
// представлен строкой, объекта под рукой нет, поэтому код кладём в сам пункт
type ActItem = { key: string; icon: string; section: Section; id?: number; text: string; srv?: boolean; sec?: string; cc?: string }
type ProbItem = { key: string; id?: number; down: boolean; text: string; srv?: boolean; cc?: string }

const K_BAD_POD = ['CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'Error', 'OOMKilled', 'CreateContainerError']
const D_RESTART_POLICIES = ['always', 'unless-stopped', 'on-failure']

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
    <div className="home-attention home-attention-mute">
      <div className="home-attention-head">
        <span className="home-attention-ic">🔕</span>
        {t('Алерты не придут')}
        <span className="home-attention-n">{items.length}</span>
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
            <span className="dot degraded" />
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
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const cmd = `ansible-playbook playbooks/kervax_helpers.yml -l '${hosts.join(',')}'`
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
        </>
      )}
    </div>
  )
}

function actionItems(servers: Server[], avail: string, relProblem: string, t: T): ActItem[] {
  const items: ActItem[] = []
  // Релиз агента собран криво (раздаётся не тот бинарь, что подписан) — обновлять
  // нечем, и без этой строки поломка молчит: агенты просто вечно отвергают обновление.
  if (relProblem) {
    items.push({ key: 'agent-release', icon: '⛔', section: 'servers', text: relProblem })
  }
  const behind = servers.filter((s) => s.online && s.agent_version && avail && s.agent_version !== avail)
  if (behind.length > 0) {
    items.push({ key: 'agent-upd', icon: '⬆', section: 'servers',
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
    items.push({ key: `adv-${title}`, icon: '🩺', section: 'servers',
      text: t('{what}: нод без настройки — {n}', { what: title, n }) })
  }
  for (const s of servers) {
    if (!s.online) continue
    const rep = s.last_report
    if (rep?.docker?.present && !rep.docker.access) {
      items.push({ key: `d-acc-${s.id}`, icon: '🐳', section: 'docker', id: s.id, cc: s.country,
        text: t('{name}: Docker без доступа — включите read-only proxy', { name: s.name }) })
    }
    if (rep?.kube?.present && !rep.kube.access) {
      items.push({ key: `k-acc-${s.id}`, icon: '☸', section: 'kuber', id: s.id, cc: s.country,
        text: t('{name}: Kubernetes без доступа — запустите kube-setup', { name: s.name }) })
    }
    // бэкап-сервер найден (по docker), но статистики репо нет → нужен backupserver-setup
    const bs = rep?.backup_server
    if (bs?.present && (!bs.repos || bs.repos.length === 0)) {
      items.push({ key: `bs-setup-${s.id}`, icon: '🗄', section: 'backups', id: s.id, cc: s.country, srv: true,
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
      items.push({ key: `dbdump-${s.id}`, icon: '🛢', section: 'backups', id: s.id, cc: s.country,
        text: t('{name}: {db} — нужен отдельный дамп',
          { name: s.name, db: dbs.map((x) => x.subject).join(', ') }) })
    }
    for (const h of s.helper_advice || []) {
      // «устарел (? → v0.1)» на ноде, где helper'а нет вовсе, вводит в заблуждение:
      // это не обновление, а первая установка, и делается она тем же прогоном.
      const fresh = !h.installed
      items.push({ key: `hlp-${h.name}-${s.id}`, icon: '🧩', section: 'servers', id: s.id, cc: s.country,
        text: fresh
          ? t('{name}: helper «{helper}» не установлен ({b}) — поставьте', {
              name: s.name, helper: h.name, b: fmtSetupVersion(h.current),
            })
          : t('{name}: helper «{helper}» устарел ({a} → {b}) — переустановите', {
              name: s.name, helper: h.name,
              a: fmtSetupVersion(h.installed), b: fmtSetupVersion(h.current),
            }) })
    }
  }
  return items
}

// проблемы Docker: упавшие/циклящиеся контейнеры (по хостам)
function dockerProblems(servers: Server[], t: T): ProbItem[] {
  const out: ProbItem[] = []
  for (const s of servers) {
    const d = s.online ? s.last_report?.docker : undefined
    if (!d?.access || !d.containers) continue
    const down = d.containers.filter((c) => (c.state === 'exited' || c.state === 'dead') && D_RESTART_POLICIES.includes((c.policy || '').toLowerCase()))
    const loop = d.containers.filter((c) => (c.restarts || 0) >= 3 && c.state === 'running')
    if (down.length > 0) out.push({ key: `d-down-${s.id}`, id: s.id, cc: s.country, down: true, text: t('{name}: контейнеров упало — {n}', { name: s.name, n: down.length }) })
    if (loop.length > 0) out.push({ key: `d-loop-${s.id}`, id: s.id, cc: s.country, down: true, text: t('{name}: перезапусков — {n}', { name: s.name, n: loop.length }) })
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
    if (nr > 0) out.push({ key: `k-node-${s.id}`, id: s.id, cc: s.country, down: true, text: t('{name}: нод NotReady — {n}', { name: s.name, n: nr }) })
    if (bad > 0) out.push({ key: `k-pod-${s.id}`, id: s.id, cc: s.country, down: true, text: t('{name}: проблемных подов — {n}', { name: s.name, n: bad }) })
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
      if (b.success === 0) out.push({ key: `b-fail-${s.id}`, id: s.id, cc: s.country, down: true, text: t('{name}: бэкап завершился с ошибкой', { name: s.name }) })
      else if (b.last_backup_ts && Date.now() / 1000 - b.last_backup_ts > 2 * 86400) out.push({ key: `b-stale-${s.id}`, id: s.id, cc: s.country, down: false, text: t('{name}: бэкап не свежий', { name: s.name }) })
    }
    const bs = s.last_report?.backup_server
    // сервер без настроенного бэкапа (и не помеченный «не требуется», и не сам бэкап-сервер)
    if (!bs?.present && !(b?.configured || b?.metric_present) && !s.backup_not_required) {
      out.push({ key: `b-none-${s.id}`, id: s.id, cc: s.country, down: false, text: t('{name}: бэкап не настроен', { name: s.name }) })
    }
    if (bs?.present) {
      if (!bs.running) out.push({ key: `bs-stop-${s.id}`, id: s.id, cc: s.country, down: true, srv: true, text: t('{name}: rest-server остановлен', { name: s.name }) })
      const rmuted = new Set(s.backup_repo_mutes ?? [])
      const badRepos = (bs.repos || []).filter((r) => {
        if (rmuted.has(r.name)) return false
        // лок идущего бэкапа — не проблема (см. lockStuck в BackupsPage)
        const stuck = r.locked && (!r.lock_ts || Date.now() / 1000 - r.lock_ts > 30 * 60)
        if (!r.valid || stuck) return true
        return r.last_activity ? Date.now() / 1000 - r.last_activity > 3 * 86400 : false
      }).length
      if (badRepos > 0) out.push({ key: `bs-repo-${s.id}`, id: s.id, cc: s.country, down: true, srv: true, text: t('{name}: репозиториев с проблемой — {n}', { name: s.name, n: badRepos }) })
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
  const { sections } = useAuth()
  const canSee = useCallback(
    (sec: Section) => sections.length === 0 || sections.includes(sec),
    [sections],
  )
  const [ov, setOv] = useState<ChecksOverview | null>(null)
  const [servers, setServers] = useState<Server[] | null>(null)
  const [avail, setAvail] = useState('')
  const [relProblem, setRelProblem] = useState('')
  const [probeHints, setProbeHints] = useState<LocalProbeSuggestion[]>([])
  const [mutes, setMutes] = useState<MuteWarning[]>([])

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
  const allUp = ov && ov.down === 0 && ov.degraded === 0 && ov.open_incidents === 0
  const downChecks = (ov?.checks ?? [])
    // только включённые: выключенный «down» — не проблема (и счётчики его не считают)
    .filter((c) => c.enabled && (c.last_status === 'down' || c.last_status === 'degraded'))
    .sort((a, b) => (a.last_status === 'down' ? 0 : 1) - (b.last_status === 'down' ? 0 : 1))
  const srvTotal = servers?.length ?? 0
  const srvOnline = servers?.filter((s) => s.online).length ?? 0
  const srvOffline = srvTotal - srvOnline
  // проблемные серверы (оффлайн + перегрузка ресурсов) — что именно не так
  // проблема = есть t-down (оффлайн / ≥порога alert/crit); предупреждение = только
  // t-degraded (warn, напр. диск 85%). Раньше всё валилось в «Проблемы» скопом.
  const srvProblems = (servers ?? [])
    .map((s) => {
      const iss = srvIssues(s, t)
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
  const actions = actionItems(srvList, avail, relProblem, t).filter((x) => canSee(x.section))
  const dockerHosts = srvList.filter((s) => s.last_report?.docker?.present && s.last_report.docker.access)
  const dockerRunning = dockerHosts.reduce((n, s) => n + (s.last_report?.docker?.containers?.filter((c) => c.state === 'running').length ?? 0), 0)
  const dockerProbs = dockerProblems(srvList, t)
  const kubeHosts = srvList.filter((s) => s.last_report?.kube?.present && s.last_report.kube.access)
  const kubePods = kubeHosts.reduce((n, s) => n + (s.last_report?.kube?.pods?.filter((p) => p.phase === 'Running').length ?? 0), 0)
  const kubeProbs = kubeProblems(srvList, t)
  const backupClients = srvList.filter((s) => s.last_report?.backup?.present)
  const backupServers = srvList.filter((s) => s.last_report?.backup_server?.present)
  const backupProbs = backupProblems(srvList, t)

  // Хосты для ansible-кнопки «обновить сразу на N нодах»: те, что плейбук реально чинит —
  // устаревший helper (переустановка) ИЛИ не включён watchdog (helper agent-watchdog, ALWAYS).
  // Сетка главной — 3 колонки; если разделы колонки закрыты, колонку не рендерим
  // вовсе, иначе в вёрстке остаётся пустая треть.
  const colSites = canSee('sites') || canSee('servers')
  const colDocker = canSee('docker') || canSee('kuber')
  const colBackups = canSee('backups')
  const homeCols = [colSites, colDocker, colBackups].filter(Boolean).length
  const rolloutHosts = [...new Set(
    srvList
      .filter((s) => s.online && ((s.helper_advice?.length ?? 0) > 0 || s.last_report?.caps?.watchdog === false))
      .map((s) => s.hostname || s.name),
  )].sort()
  return (
    <div>
      <p className="tagline">{t('Мониторинг инфраструктуры')}</p>
      {mutes.length > 0 && <MuteHint items={mutes} onOpen={onOpen} onDone={load} />}
      {probeHints.length > 0 && (
        <div className="home-attention home-attention-probe">
          <div className="home-attention-head">
            <span className="home-attention-ic">🏠</span>
            {t('Похоже на белый список')}
            <span className="home-attention-n">{probeHints.length}</span>
          </div>
          <LocalProbeHint items={probeHints} onDone={load} />
        </div>
      )}
      {actions.length > 0 && (
        <div className="home-attention">
          <div className="home-attention-head">
            <span className="home-attention-ic">⚠️</span>
            {t('Требует действий')}
            <span className="home-attention-n">{actions.length}</span>
          </div>
          <div className="home-attention-list">
            {/* Делим по тому, КУДА ведёт пункт: чинится на сервере (агент, helper'ы,
                доступы) — сверху; бэкапное — ниже, оно обычно менее срочное. */}
            {([
              { key: 'srv', label: t('На серверах'), items: actions.filter((x) => x.section !== 'backups') },
              { key: 'bak', label: t('По бэкапам'), items: actions.filter((x) => x.section === 'backups') },
            ] as const).map((grp) =>
              grp.items.length === 0 ? null : (
                <div key={grp.key} className="home-attention-grp">
                  {/* заголовок только если есть ОБЕ группы — иначе он лишний шум */}
                  {actions.some((x) => x.section === 'backups') &&
                    actions.some((x) => x.section !== 'backups') && (
                      <div className="home-attention-grp-head muted small">{grp.label}</div>
                    )}
                  {grp.items.slice(0, ATTENTION_LIMIT).map((it) => (
                    <button
                      key={it.key}
                      className="home-attention-row"
                      onClick={() => onOpen(it.section, it.id, it.srv, it.sec)}
                    >
                      <span className="dot degraded" />
                      <span className="home-attention-ico">{it.icon}</span>
                      <span className="home-attention-txt"><CountryFlag code={it.cc} /> {it.text}</span>
                      <span className="home-open">→</span>
                    </button>
                  ))}
                  {grp.items.length > ATTENTION_LIMIT && (
                    <div className="muted small home-attention-more">
                      {t('…и ещё {n}', { n: grp.items.length - ATTENTION_LIMIT })}
                    </div>
                  )}
                  {grp.key === 'srv' && rolloutHosts.length > 1 && (
                    <AnsibleHint hosts={rolloutHosts} />
                  )}
                </div>
              ),
            )}
          </div>
        </div>
      )}
      {/* Мягкие уведомления (НЕ алерты, не в Telegram): бэкап вышел за ночное окно.
          Отдельный блок, приглушённый — это «к сведению», а не «требует действий». */}
      {(() => {
        if (!canSee('backups')) return null // уведомление ведёт в закрытый раздел
        const late = srvList
          .map((s) => ({ s, note: backupWindowNote(s) }))
          .filter((x) => x.note)
        if (late.length === 0) return null
        return (
          <div className="home-notice">
            <div className="home-notice-head muted small">
              ⏰ {t('Бэкап вышел за окно ({n})', { n: late.length })}
            </div>
            {late.slice(0, 6).map(({ s, note }) => (
              <button key={s.id} className="home-notice-row"
                onClick={() => onOpen('backups', s.id)}>
                <span className="home-attention-ico">💾</span>
                <span className="home-attention-txt">
                  <CountryFlag code={s.country} /> {s.name}: {note}
                </span>
                <span className="home-open">→</span>
              </button>
            ))}
          </div>
        )
      })()}
      <div className={`home-cols cols-${homeCols}`}>
        {colSites && (
        <div className="home-col">
        {/* --- Сайты --- */}
        {canSee('sites') && (
        <button className="home-card" onClick={() => onNavigate('sites')}>
          <div className="home-card-head">
            <h3 className="home-card-title"><IconSites /><span>{t('Сайты')}</span></h3>
            <span className="home-open">{t('Открыть →')}</span>
          </div>

          {ov == null ? (
            <div className="muted small">{t('загрузка…')}</div>
          ) : ov.total === 0 ? (
            <div className="muted small">{t('Мониторов пока нет.')}</div>
          ) : (
            <>
              <div className="home-headline">
                {allUp ? (
                  <span className="t-up">{t('Все {n} в норме', { n: ov.total })}</span>
                ) : (
                  <span className="t-down">
                    {t('Проблемы: {n}', { n: ov.down + ov.degraded })}
                  </span>
                )}
              </div>
              <div className="home-stat-row">
                <Mini dot="up" label={t('Работает')} value={ov.up} />
                <Mini dot="degraded" label={t('Деградация')} value={ov.degraded} />
                <Mini dot="down" label={t('Недоступно')} value={ov.down} />
              </div>
              {ov.open_incidents > 0 && (
                <div className="home-inc">
                  {t('{n} откр. инцидентов', { n: ov.open_incidents })}
                </div>
              )}
              {downChecks.length > 0 && (
                <div className="home-warns">
                  {downChecks.slice(0, HOME_LIST_LIMIT).map((c) => (
                    <div key={c.id} className="home-warn">
                      <span className={`dot ${c.last_status === 'down' ? 'down' : 'degraded'}`} />
                      <span className="warn-name">{c.name}</span>
                      <span className="muted small">
                        {c.last_status === 'down' ? t('Недоступно') : t('Деградация')}
                        {c.last_message ? ` · ${c.last_message}` : ''}
                      </span>
                    </div>
                  ))}
                  {downChecks.length > HOME_LIST_LIMIT && (
                    <div className="muted small">
                      {t('…ещё {n}', { n: downChecks.length - HOME_LIST_LIMIT })}
                    </div>
                  )}
                </div>
              )}
              {warns.length > 0 && (
                <div className="home-warns">
                  {warns.slice(0, HOME_LIST_LIMIT).map((w, i) => (
                    <div key={i} className="home-warn">
                      <span className={`warn-tone ${w.days <= 3 ? 't-down' : 't-degraded'}`}>
                        {w.kind === 'ssl' ? '🔐' : '🌐'}
                      </span>
                      <span className="warn-name">{w.name}</span>
                      <span className="muted small">
                        {w.kind === 'ssl' ? t('SSL') : t('домен')} ·{' '}
                        {expiryText(w.days, t)}
                        {w.more ? ` · ${t('мониторов: {n}', { n: w.more + 1 })}` : ''}
                      </span>
                    </div>
                  ))}
                  {warns.length > HOME_LIST_LIMIT && (
                    <div className="muted small">
                      {t('…ещё {n}', { n: warns.length - HOME_LIST_LIMIT })}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </button>

        )}

        {/* --- Сервера --- */}
        {canSee('servers') && (
        <button
          className="home-card"
          onClick={() => onNavigate('servers')}
        >
          <div className="home-card-head">
            <h3 className="home-card-title"><IconServers /><span>{t('Серверы')}</span></h3>
            <span className="home-open">{t('Открыть →')}</span>
          </div>

          {servers == null ? (
            <div className="muted small">{t('загрузка…')}</div>
          ) : srvTotal === 0 ? (
            <div className="muted small">{t('Серверов пока нет.')}</div>
          ) : (
            <>
              <div className="home-headline">
                {srvDown.length > 0 ? (
                  /* оффлайн-нода УЖЕ сидит в srvDown (srvIssues отдаёт ей «оффлайн»),
                     складывать её с srvOffline нельзя — выходило «Проблемы: 2» на одну ноду */
                  <span className="t-down">{t('Проблемы: {n}', { n: srvDown.length })}</span>
                ) : srvWarn.length > 0 ? (
                  <span className="t-degraded">{t('Предупреждения: {n}', { n: srvWarn.length })}</span>
                ) : (
                  <span className="t-up">{t('Все {n} онлайн', { n: srvTotal })}</span>
                )}
              </div>
              <div className="home-stat-row">
                <Mini dot="up" label={t('Онлайн')} value={srvOnline} />
                <Mini dot="down" label={t('Оффлайн')} value={srvOffline} />
              </div>
              {srvProblems.length > 0 && (
                <div className="home-warns">
                  {/* проблемы сверху, предупреждения ниже — критичное первым */}
                  {[...srvDown, ...srvWarn].slice(0, HOME_LIST_LIMIT).map((p) => (
                    <div key={p.id} className="home-warn">
                      <span className={`dot ${p.down ? 'down' : 'degraded'}`} />
                      <CountryFlag code={p.cc} />
                      <span className="warn-name">{p.name}</span>
                      <span className="muted small">{p.text}</span>
                      {p.mutes.length > 0 && (
                        <span className="quick-mute" title={t('Приглушить')}
                          onClick={(e) => { e.stopPropagation(); quickMute(p.id, p.mutes) }}>
                          🔕
                        </span>
                      )}
                    </div>
                  ))}
                  {srvProblems.length > HOME_LIST_LIMIT && (
                    <div className="muted small">
                      {t('…ещё {n}', { n: srvProblems.length - HOME_LIST_LIMIT })}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </button>
        )}
        </div>
        )}

        {colDocker && (
        <div className="home-col">
        {/* --- Docker --- */}
        {canSee('docker') && (
        <SectionCard
          title="Docker" icon={<IconDocker />} section="docker" total={dockerHosts.length}
          okText={t('Все хосты ок')}
          stats={
            <div className="home-stat-row">
              <Mini dot="up" label={t('Хостов')} value={dockerHosts.length} />
              <Mini dot="up" label={t('Контейнеров')} value={dockerRunning} />
            </div>
          }
          problems={dockerProbs} onNavigate={onNavigate} onOpen={onOpen} t={t}
        />
        )}

        {/* --- Kubernetes --- */}
        {canSee('kuber') && (
        <SectionCard
          title="Kubernetes" icon={<IconKube />} section="kuber" total={kubeHosts.length}
          okText={t('Все кластеры ок')}
          stats={
            <div className="home-stat-row">
              <Mini dot="up" label={t('Кластеров')} value={kubeHosts.length} />
              <Mini dot="up" label={t('Подов')} value={kubePods} />
            </div>
          }
          problems={kubeProbs} onNavigate={onNavigate} onOpen={onOpen} t={t}
        />
        )}
        </div>
        )}

        {colBackups && (
        <div className="home-col">
        {/* --- Бэкапы --- */}
        <SectionCard
          title={t('Бэкапы')} icon={<IconBackups />} section="backups" total={backupClients.length + backupServers.length}
          okText={t('Все свежие')}
          stats={
            <div className="home-stat-row">
              <Mini dot="up" label={t('Клиентов')} value={backupClients.length} />
              <Mini dot="up" label={t('Серверов')} value={backupServers.length} />
            </div>
          }
          problems={backupProbs} onNavigate={onNavigate} onOpen={onOpen} t={t}
        />
        </div>
        )}
      </div>
    </div>
  )
}

function Mini({
  dot,
  label,
  value,
}: {
  dot: 'up' | 'degraded' | 'down'
  label: string
  value: number
}) {
  return (
    <div className="home-mini">
      <span className={`dot ${dot}`} />
      <span className={`home-mini-num t-${dot}`}>{value}</span>
      <span className="muted small">{label}</span>
    </div>
  )
}

// Карточка раздела (Docker/Kubernetes/Бэкапы): ВСЯ плитка кликабельна (→ в раздел),
// строки проблем перехватывают клик и ведут СРАЗУ в конкретный хост (deep-link).
// Не рендерится, если в разделе нет объектов.
function SectionCard({
  title, icon, section, total, okText, stats, problems, onNavigate, onOpen, t,
}: {
  title: string
  icon: ReactNode
  section: Section
  total: number
  okText: string
  stats?: ReactNode
  problems: ProbItem[]
  onNavigate: (s: Section) => void
  onOpen: (s: Section, id?: number, srv?: boolean) => void
  t: T
}) {
  if (total === 0) return null
  return (
    <div className="home-card home-card-clickable" role="button" tabIndex={0} onClick={() => onNavigate(section)}>
      <div className="home-card-head">
        <h3 className="home-card-title">{icon}<span>{title}</span></h3>
        <span className="home-open">{t('Открыть →')}</span>
      </div>
      <div className="home-headline">
        {problems.length > 0 ? (
          <span className="t-down">{t('Проблемы: {n}', { n: problems.length })}</span>
        ) : (
          <span className="t-up">{okText}</span>
        )}
      </div>
      {stats}
      {problems.length > 0 && (
        <div className="home-warns">
          {problems.slice(0, HOME_LIST_LIMIT).map((p) => (
            <button
              key={p.key}
              className="home-warn home-warn-btn"
              onClick={(e) => {
                e.stopPropagation()
                onOpen(section, p.id, p.srv)
              }}
            >
              <span className={`dot ${p.down ? 'down' : 'degraded'}`} />
              <CountryFlag code={p.cc} />
              <span className="warn-name">{p.text}</span>
              <span className="home-open">→</span>
            </button>
          ))}
          {problems.length > HOME_LIST_LIMIT && (
            <div className="muted small">{t('…ещё {n}', { n: problems.length - HOME_LIST_LIMIT })}</div>
          )}
        </div>
      )}
    </div>
  )
}
