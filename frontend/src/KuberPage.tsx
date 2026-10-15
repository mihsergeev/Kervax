import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  kubeCommand,
  kubeCommandStatus,
  kubeNodeIgnore,
  listServers,
  podFinished,
  type FluxState,
  type KubeCommand,
  type KubeExpiry,
  type KubeInfo,
  type KubeNodeRef,
  type KubePod,
  type KubeWorkload,
  type Server,
  type ServerEnroll,
} from './api'
import { useAuth } from './auth'
import { useUrlCard, useUrlParam } from './deeplink'
import { useI18n } from './i18n'
import { RESTARTS_DAY_MIN } from './mutes'
import { OsIcon } from './osIcon'
import { CountryFlag } from './CountryFlag'
import { fmtBytes } from './units'
import { shortList, sinceShort, srvIssues } from './serverUtils'
import { EnrollModal, fmtRel, InstallModal } from './ServersPage'
import { CardNav, FreshChip, OvAreas, OvRows, secInfoOf, type OvRow, type SecDef, type SecInfo, type SecItem } from './cardLayout'
import { NetTile } from './NetLinks'

// Вкладка «Кубер»: КОМПАКТНЫЙ список хостов с кластером; клик по хосту → модалка
// с нодами, воркоадами и подами (статусы, рестарты, логи, управление). Агент читает
// kube-api по токену УЗКОГО ServiceAccount (kube-setup.sh) — read + точечный write
// (rollout restart / delete pod), не cluster-admin. Логи не хранятся — разовый tail.

// команда для включения доступа (kube-setup.sh на control-plane ноде): создаёт
// ServiceAccount kervax-agent с узким RBAC (read + rollout restart + delete pod),
// кладёт токен в /etc/kervax/kube.json (root:kervax), перезапускает агент.
const KUBE_ENABLE_CMD = `curl -fsSL ${window.location.origin}/api/agent/kube-setup.sh | sudo bash
sudo systemctl restart kervax-agent`

// Что именно истекает. Путь к файлу сам по себе инженеру ничего не говорит —
// пока не сказано, чем этот файл держит кластер.
const EXPIRY_KIND: Record<string, string> = {
  'cluster-cert': 'сертификат control-plane',
  kubeconfig: 'kubeconfig',
  'kubelet-cert': 'сертификат kubelet',
  'flux-token': 'токен Flux',
  'secret-cert': 'TLS-сертификат',
}

// Ready=False по этим причинам — работа, а не поломка: Flux тянет артефакт или
// докатывает релиз.
const FLUX_TRANSIENT = new Set(['Progressing', 'Reconciling', 'ProgressingWithRetry', 'Unknown'])

// точка строки по тону: приглушенное и нейтральное - серая
const DOT: Record<string, string> = { 't-up': ' lk-dot-ok', 't-down': ' lk-dot-bad', 't-degraded': ' lk-dot-warn' }
// подпись плитки из частей: пустые выкидываем
const subOf = (...parts: (string | false | 0 | undefined)[]) => parts.filter(Boolean).join(' · ')

// К ближайшему, а не вниз — как считает бэкенд в алерте: остаток 4 дня 23 часа
// это «5 дн.», иначе панель и Telegram называют один и тот же срок по-разному.
function daysLeft(ts: number): number {
  return Math.round((ts * 1000 - Date.now()) / 86400000)
}

// Цвет по остатку: красный - уже истекло, желтый - в пределах первого порога алерта
// сервера (по умолчанию неделя), дальше обычный.
function expiryTone(d: number, horizon: number): string {
  return d < 0 ? 't-down' : d <= horizon ? 't-degraded' : ''
}
// "Скоро" - то же, что и у алерта: первый (самый дальний) порог сервера
function expiryHorizon(s: Server): number {
  return Math.max(0, ...(s.kube_expiry_warn_days ?? [7]))
}

function podTone(p: KubePod): string {
  // завершённые Job/CronJob-поды — история, не живой воркоад: Succeeded=ок,
  // Failed(job)=приглушённо (нейтрально), не красный «упал».
  if (podFinished(p)) return p.phase === 'Succeeded' ? 't-up' : 't-muted'
  const bad = ['CrashLoopBackOff', 'Error', 'OOMKilled', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerError']
  if (p.phase === 'Failed' || (p.reason && bad.includes(p.reason))) return 't-down'
  if (p.phase === 'Running' && p.ready) return 't-up'
  return 't-degraded'
}
function workloadTone(w: KubeWorkload): string {
  if (w.desired === 0) return 't-degraded'
  if (w.ready >= w.desired) return 't-up'
  if (w.ready === 0) return 't-down'
  return 't-degraded'
}
// Завершённые Job/CronJob-поды (Succeeded/Failed) — история, НЕ считаем «должны бежать».
// active = поды, которые должны работать; running = из них реально Running+ready.
function podCounts(pods: KubePod[]): { running: number; active: number } {
  const active = pods.filter((p) => !podFinished(p))
  const running = active.filter((p) => p.phase === 'Running' && p.ready).length
  return { running, active: active.length }
}

type KAction = 'rollout_restart' | 'delete_pod' | 'logs'

async function runAndWait(
  serverId: number,
  ns: string,
  kind: 'deployment' | 'statefulset' | 'daemonset' | 'pod',
  name: string,
  action: KAction,
  opts: { tail?: number; since?: number } = {},
): Promise<KubeCommand> {
  const c = await kubeCommand(serverId, ns, kind, name, action, opts)
  let last = c
  const maxTries = opts.since ? 200 : 100
  for (let i = 0; i < maxTries && last.status !== 'done' && last.status !== 'error'; i++) {
    await new Promise((r) => setTimeout(r, 300))
    last = await kubeCommandStatus(serverId, c.id)
  }
  return last
}

// Логотип Kubernetes: синий семиугольник + белый штурвал. Двухцветный — чтобы
// штурвал читался на маленьком размере (моно-силуэт из simple-icons сливался в пятно).
function KubeLogo() {
  return (
    <svg className="docker-logo" viewBox="0 0 128 128" aria-hidden>
      <path
        fill="#326CE5"
        d="M64 4 L112.5 26.4 L124.5 78.6 L91.2 120.5 L36.8 120.5 L3.5 78.6 L15.5 26.4 Z"
      />
      <g fill="none" stroke="#fff" strokeWidth="6" strokeLinejoin="round" strokeLinecap="round">
        <polygon points="64,34 87.5,45.3 93.3,70.7 77,91 51,91 34.7,70.7 40.5,45.3" />
        <path d="M64 64 L64 34 M64 64 L87.5 45.3 M64 64 L93.3 70.7 M64 64 L77 91 M64 64 L51 91 M64 64 L34.7 70.7 M64 64 L40.5 45.3" />
      </g>
      <circle cx="64" cy="64" r="12" fill="#326CE5" stroke="#fff" strokeWidth="6" />
    </svg>
  )
}

function EnableBlock() {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="chart-cap">{t('Нет доступа к kube-api')}</div>
      </div>
      <div className="net-note">
        {t('Кластер найден, но агент не имеет доступа к kube-api. Включите read-only + точечное управление: скрипт создаёт ServiceAccount с узким RBAC (не cluster-admin) и кладёт токен для агента.')}
      </div>
      <div className="agent-advice-cmd">
        <pre>{KUBE_ENABLE_CMD}</pre>
        <button
          className="ghost"
          onClick={() => {
            navigator.clipboard?.writeText(KUBE_ENABLE_CMD)
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          }}
        >
          {copied ? t('Скопировано') : t('Копировать')}
        </button>
      </div>
    </div>
  )
}

type LogRange = 'tail' | 'hour' | 'day'

// Логи пода (on-demand tail через агент). Аналог docker-логов.
function PodLogsModal({
  server,
  pod,
  onClose,
}: {
  server: Server
  pod: KubePod
  onClose: () => void
}) {
  const { t } = useI18n()
  const [logs, setLogs] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [range, setRange] = useState<LogRange>('tail')
  const load = useCallback(
    async (r: LogRange) => {
      setLoading(true)
      setErr(null)
      setRange(r)
      const opts = r === 'tail' ? { tail: 400 } : { since: r === 'hour' ? 3600 : 86400 }
      try {
        const res = await runAndWait(server.id, pod.ns, 'pod', pod.name, 'logs', opts)
        if (res.status === 'done') setLogs(res.result ?? '')
        else setErr(res.result || t('не удалось получить логи'))
      } catch {
        setErr(t('ошибка'))
      } finally {
        setLoading(false)
      }
    },
    [server.id, pod.ns, pod.name, t],
  )
  useEffect(() => {
    load('tail')
  }, [load])
  const download = () => {
    const blob = new Blob([logs ?? ''], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')
    a.href = url
    a.download = `${pod.name}_${stamp}.txt`
    a.click()
    URL.revokeObjectURL(url)
  }
  const ranges: { k: LogRange; l: string }[] = [
    { k: 'tail', l: t('400 строк') },
    { k: 'hour', l: t('за час') },
    { k: 'day', l: t('за день') },
  ]
  const VIEW_MAX = 500_000
  const truncated = !!logs && logs.length > VIEW_MAX
  const view = truncated ? logs!.slice(-VIEW_MAX) : logs
  return createPortal(
    <div className="modal-backdrop">
      <div className="card modal logs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3 className="mono">
            {pod.name} <span className="muted small">· {pod.ns} · <CountryFlag code={server.country} /> {server.name}</span>
          </h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="logs-toolbar">
          <div className="win-switch">
            {ranges.map(({ k, l }) => (
              <button
                key={k}
                className={`win-btn${range === k ? ' win-btn-active' : ''}`}
                disabled={loading}
                onClick={() => load(k)}
              >
                {l}
              </button>
            ))}
          </div>
          <div className="spacer" />
          <button className="ghost" disabled={loading} onClick={() => load(range)}>
            {loading ? t('…') : t('Обновить')}
          </button>
          <button className="ghost" disabled={!logs} onClick={download}>
            {t('Скачать .txt')}
          </button>
        </div>
        {err && <p className="form-error">{err}</p>}
        {loading && logs == null ? (
          <div className="muted small">{t('Запрашиваю логи у агента…')}</div>
        ) : logs === '' ? (
          <div className="logs-empty muted">
            {range === 'tail'
              ? t('Пусто — контейнер ничего не писал в лог.')
              : t('Нет записей за выбранный период. Контейнер давно не писал в лог — попробуйте «400 строк».')}
          </div>
        ) : (
          <pre className="logs-pre">{view}</pre>
        )}
        <div className="muted small">
          {truncated
            ? t('Показаны последние 500 КБ из {mb} МБ — скачайте .txt для полного.', {
                mb: ((logs?.length ?? 0) / 1_000_000).toFixed(1),
              })
            : t('Логи не хранятся — читаются с ноды по запросу (кап ~20 МБ).')}
        </div>
      </div>
    </div>,
    document.body,
  )
}

function WorkloadRow({
  serverId,
  w,
  canAct,
  onChanged,
}: {
  serverId: number
  w: KubeWorkload
  canAct: boolean
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const kind = w.kind.toLowerCase() as 'deployment' | 'statefulset' | 'daemonset'
  const restart = async () => {
    if (!window.confirm(t('Rollout restart «{n}» ({k})?', { n: w.name, k: w.kind }))) return
    setBusy(true)
    setErr(null)
    try {
      const res = await runAndWait(serverId, w.ns, kind, w.name, 'rollout_restart')
      if (res.status !== 'done') setErr(res.result || t('не удалось'))
      onChanged()
    } catch {
      setErr(t('ошибка'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className={`loc-res docker-row ${workloadTone(w)}`}>
      <span className={`lk-dot${DOT[workloadTone(w)] ?? ''}`} />
      <div className="docker-c-main">
        <div className="docker-c-name mono">
          <span className="type-chip">{w.kind}</span> {w.name}
          <span className="muted small"> · {w.ns}</span>
        </div>
        {err && <div className="form-error small">{err}</div>}
      </div>
      <div className={`docker-c-status mono small ${workloadTone(w)}`}>{w.ready}/{w.desired}</div>
      <div className="docker-actions">
        {canAct && (
          <button className="ghost icon-btn" disabled={busy} onClick={restart} title={t('Rollout restart')}>
            {busy ? '…' : '⟳'}
          </button>
        )}
      </div>
    </div>
  )
}

// CPU в ядрах: 0.25, 2.5 - как удобнее читать, чем милликоры
function fmtCores(m: number): string {
  return m >= 10000 ? String(Math.round(m / 1000)) : m >= 100 ? (m / 1000).toFixed(1) : (m / 1000).toFixed(2)
}

function PodRow({
  serverId,
  p,
  canAct,
  onLogs,
  onChanged,
  day = 0,
}: {
  serverId: number
  p: KubePod
  day?: number // перезапусков за сутки (restarts_day)
  canAct: boolean
  onLogs: () => void
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const done = podFinished(p)
  const act = async () => {
    const q = done
      ? t('Удалить завершённый под «{n}» (очистить историю)?', { n: p.name })
      : t('Перезапустить под «{n}» (удалить — контроллер пересоздаст)?', { n: p.name })
    if (!window.confirm(q)) return
    setBusy(true)
    setErr(null)
    try {
      const res = await runAndWait(serverId, p.ns, 'pod', p.name, 'delete_pod')
      if (res.status !== 'done') setErr(res.result || t('не удалось'))
      onChanged()
    } catch {
      setErr(t('ошибка'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className={`loc-res docker-row ${podTone(p)}`}>
      <span className={`lk-dot${DOT[podTone(p)] ?? ''}`} />
      <div className="docker-c-main">
        <div className="docker-c-name mono">
          {p.name}
          <span className="muted small"> · {p.ns}</span>
          {done ? (
            <span
              className="type-chip"
              title={t('Под Job/CronJob, уже отработал ({phase}). Это историческая запись, а не живой воркоад — чинить нечего; удалите, чтобы убрать из списка.', { phase: p.phase })}
            >
              {p.phase === 'Succeeded' ? t('отработал') : t('Job — упал разово')}
            </span>
          ) : (
            p.reason && <span className="type-chip off">{p.reason}</span>
          )}
          {!!p.restarts && p.restarts > 0 && (
            <span
              className={`type-chip${day >= RESTARTS_DAY_MIN ? ' t-degraded' : ''}`}
              title={day >= RESTARTS_DAY_MIN
                ? t('перезапусков за сутки: {d}, за все время: {n}. Падает раз в час-другой: между падениями работает, но теряет соединения и очереди', { d: day, n: p.restarts })
                : t('перезапусков за все время: {n} (само по себе не проблема, тревога - только CrashLoopBackOff)', { n: p.restarts })}
            >
              ⟳ {p.restarts}{day > 0 && ` · ${t('{d} за сутки', { d: day })}`}
            </span>
          )}
          {p.nolim && (
            <span className="type-chip t-degraded" title={t('У контейнера нет лимита памяти: один такой под может съесть всю ноду.')}>
              {t('без лимита памяти')}
            </span>
          )}
          {p.noprobe && (
            <span className="type-chip t-degraded" title={t('Нет ни liveness, ни readiness пробы: зависший под так и будет считаться работающим.')}>
              {t('без проб')}
            </span>
          )}
        </div>
        {p.msg && <div className="docker-c-img mono muted small">{p.msg}</div>}
        {p.use && (
          <div className="docker-c-img mono muted small" title={t('по metrics-server, как kubectl top')}>
            {fmtBytes(p.use.mem)} · CPU {fmtCores(p.use.cpu_m)}
            {p.node ? ` · ${p.node}` : ''}
          </div>
        )}
        {err && <div className="form-error small">{err}</div>}
      </div>
      <div className={`docker-c-status mono small ${podTone(p)}`}>{p.phase}</div>
      <div className="docker-actions">
        <button className="ghost icon-btn" onClick={onLogs} title={t('Логи')}>
          📄
        </button>
        {canAct && (
          <button className="ghost icon-btn" disabled={busy} onClick={act} title={done ? t('Удалить под (очистить)') : t('Перезапустить под')}>
            {busy ? '…' : done ? '🗑' : '⟳'}
          </button>
        )}
      </div>
    </div>
  )
}

// Карточка кластера: меню разделов слева, на экране один раздел, как у сервера (cardLayout).
// Раньше вкладки шли переключателем над списком, над ним плитки, и одно и то же было видно в
// плитке, в подписи вкладки и в плашке. На сводке проблемы кластера из "Что сломано", плитки и
// карта по областям. Логи пода - модалка сверху.
const KUBE_SECTIONS: SecDef[] = [
  { id: 'overview', label: 'Сводка', group: '' },
  { id: 'pods', label: 'Поды', group: 'Нагрузка' },
  { id: 'finished', label: 'Завершенные', group: 'Нагрузка' },
  { id: 'workloads', label: 'Воркоады', group: 'Нагрузка' },
  { id: 'cron', label: 'CronJob', group: 'Нагрузка' },
  { id: 'nodes', label: 'Ноды', group: 'Кластер' },
  { id: 'expiry', label: 'Сроки', group: 'Кластер' },
  { id: 'flux', label: 'Flux', group: 'Кластер' },
]
const KUBE_SEC_IDS = new Set(KUBE_SECTIONS.map((x) => x.id))
// разделы со списком: у них фильтр в шапке
const KUBE_FILTER = new Set(['pods', 'finished', 'workloads', 'cron', 'flux'])

function KubeHostModal({
  server: s,
  kube,
  canAct,
  initialTab,
  onClose,
  onChanged,
  onAddNode,
}: {
  server: Server
  kube: KubeInfo
  canAct: boolean
  initialTab?: string | null // из ссылки алерта: ?kube=id&ktab=expiry, или ?kube=id&sec=expiry
  onClose: () => void
  onChanged: () => void
  onAddNode: (n: KubeNodeRef) => void // завести ноду кластера в панели (форма добавления сервера)
}) {
  const { t } = useI18n()
  const [logsPod, setLogsPod] = useState<KubePod | null>(null)
  // открытый раздел: сводка или тот, куда вела ссылка; в адресе он виден (?sec=), ссылкой на
  // раздел можно поделиться
  const [active, setActive] = useState<string>(() => (KUBE_SEC_IDS.has(initialTab ?? '') ? initialTab! : 'overview'))
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<'state' | 'name' | 'restarts' | 'ns' | 'mem' | 'cpu'>('state')
  const layoutRef = useRef<HTMLDivElement>(null)
  // другой раздел - с начала и без фильтра прошлого; если страница прокручена ниже меню,
  // подкручиваем к началу разделов
  const go = (id: string) => {
    setActive(id)
    setQ('')
    const el = layoutRef.current
    if (el && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: 'start' })
  }
  const nodes = kube.nodes ?? []
  const workloads = kube.workloads ?? []
  const pods = kube.pods ?? []
  // CronJob: с проблемой сверху (причину считает бэкенд теми же правилами, что и алерты)
  const cronIssues = s.kube_cron_issues ?? {}
  const cronjobs = (kube.cronjobs ?? []).slice().sort((a, b) =>
    Number(!cronIssues[`${a.ns}/${a.name}`]) - Number(!cronIssues[`${b.ns}/${b.name}`])
    || `${a.ns}/${a.name}`.localeCompare(`${b.ns}/${b.name}`))
  const cronBad = cronjobs.filter((c) => cronIssues[`${c.ns}/${c.name}`]).length
  const failedJobs = (s.last_report?.kube?.failed_jobs ?? []).slice().sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0))
  const hygiene = {
    nolim: pods.filter((p) => p.nolim).length,
    noprobe: pods.filter((p) => p.noprobe).length,
  }
  const ql = q.trim().toLowerCase()
  const podRank = (p: KubePod) => ({ 't-down': 0, 't-degraded': 1, 't-up': 2 })[podTone(p)] ?? 3
  // при равенстве сохраняем исходный порядок (стабильная сортировка), кроме ns —
  // там логично добить именем внутри namespace.
  const pcmp: Record<string, (a: KubePod, b: KubePod) => number> = {
    state: (a, b) => podRank(a) - podRank(b),
    name: (a, b) => a.name.localeCompare(b.name),
    restarts: (a, b) => (b.restarts ?? 0) - (a.restarts ?? 0),
    ns: (a, b) => (a.ns || '').localeCompare(b.ns || '') || a.name.localeCompare(b.name),
    // кто съел память или CPU кластера (metrics-server): самые тяжелые сверху
    mem: (a, b) => (b.use?.mem ?? 0) - (a.use?.mem ?? 0),
    cpu: (a, b) => (b.use?.cpu_m ?? 0) - (a.use?.cpu_m ?? 0),
  }
  const hasUse = pods.some((p) => p.use)
  // ноды без агента панели (считает бэкенд по всем серверам) и те, где агент не нужен
  const unmon = new Map((s.kube_unmonitored ?? []).map((n) => [n.name, n]))
  const ignoredNodes = new Set(s.kube_node_ignored ?? [])
  const [nodeBusy, setNodeBusy] = useState<string | null>(null)
  const [nodeErr, setNodeErr] = useState<{ node: string; msg: string } | null>(null)
  const setNodeIgnored = (node: string, ignored: boolean) => {
    setNodeBusy(node)
    setNodeErr(null)
    kubeNodeIgnore(s.id, node, ignored)
      .then(() => onChanged())
      .catch((e) => setNodeErr({ node, msg: e instanceof Error ? e.message : t('Ошибка') }))
      .finally(() => setNodeBusy(null))
  }
  // Сроки и состояние Flux приезжают в отчёте агента (хелпер kubeexpiry-setup),
  // а не через kube-api: у ServiceAccount панели доступа к секретам нет и не будет.
  const expiry: KubeExpiry[] = (s.last_report?.kube_expiry ?? []).slice().sort((a, b) => a.expires - b.expires)
  const flux: FluxState[] = s.last_report?.flux ?? []
  const fluxBroken = flux.filter((f) => !f.ready && !FLUX_TRANSIENT.has(f.reason ?? ''))
  const horizon = expiryHorizon(s)
  const doneCount = pods.filter(podFinished).length
  const doneFailed = pods.filter((p) => podFinished(p) && p.phase === 'Failed').length

  // Разделы, которые есть у этого кластера. Завершенные - пока они есть, сроки и Flux - когда
  // их собирает helper; без доступа к kube-api остаются сводка, сроки и Flux
  const shown: Record<string, boolean> = {
    overview: true,
    pods: kube.access,
    finished: kube.access && doneCount > 0,
    workloads: kube.access,
    cron: kube.access && (cronjobs.length > 0 || failedJobs.length > 0),
    nodes: kube.access,
    expiry: expiry.length > 0,
    flux: flux.length > 0,
  }
  const secs = KUBE_SECTIONS.filter((x) => shown[x.id])
  // раздел пропал (последний завершенный под подчистили) - на сводку, а не на пустое место
  const cur = secs.find((x) => x.id === active) ?? secs[0]
  useUrlParam('sec', cur.id === 'overview' ? null : cur.id)

  // «Поды» = только живые; завершённые (история Job/CronJob) — свой раздел, чтобы не
  // подмешивались к тому, что надо чинить
  const fpods = pods
    .filter((p) => (cur.id === 'finished' ? podFinished(p) : !podFinished(p)))
    .filter((p) => !ql || `${p.name} ${p.ns} ${p.reason ?? ''}`.toLowerCase().includes(ql))
    .slice()
    .sort(pcmp[sort])
  const fworkloads = workloads.filter((w) => !ql || `${w.name} ${w.ns} ${w.kind}`.toLowerCase().includes(ql))
  // сломанные сверху: раздел открывают, чтобы найти, что не доехало
  const fflux = flux
    .filter((f) => !ql || `${f.where} ${f.kind} ${f.reason ?? ''}`.toLowerCase().includes(ql))
    .slice()
    .sort((a, b) => Number(a.ready) - Number(b.ready) || a.where.localeCompare(b.where))
  const nodesReady = nodes.filter((n) => n.ready).length
  const { running: podsRunning, active: podsActive } = podCounts(pods)
  // плитки: нагрузка нод по metrics-server, перезапуски подов за сутки
  const memUse = nodes.reduce((n, x) => n + (x.use?.mem ?? 0), 0)
  const memCap = nodes.reduce((n, x) => n + (x.use && x.cap ? x.cap.mem : 0), 0)
  const cpuUse = nodes.reduce((n, x) => n + (x.use?.cpu_m ?? 0), 0)
  const cpuCap = nodes.reduce((n, x) => n + (x.use && x.cap ? x.cap.cpu_m : 0), 0)
  const badPods = pods.filter((p) => !podFinished(p) && podTone(p) === 't-down')
  const nodesPress = nodes.filter((n) => (n.pressure ?? []).length > 0).length
  const days = pods
    .map((p) => ({ name: p.name, n: s.restarts_day?.[`pod:${p.ns}/${p.name}`] ?? 0 }))
    .filter((x) => x.n > 0)
  const dayTop = days.reduce<{ name: string; n: number } | null>((a, b) => (!a || b.n > a.n ? b : a), null)
  const next = expiry[0]
  const nextDays = next ? daysLeft(next.expires) : 0
  const daysText = (d: number) => (d < 0 ? t('истёк') : d === 0 ? t('сегодня') : t('{n} дн.', { n: d }))
  const pct = (u: number, c: number) => (c ? Math.round((u / c) * 100) : 0)
  const wlPartial = workloads.filter((w) => w.desired > 0 && w.ready < w.desired)

  // Замечания по разделам. Проблемы те же, что в "Что сломано" на главной: с бэкенда (сроки,
  // Flux, CronJob, ноды, утечки и перезапуски подов) и те, что главная считает сама (падающие
  // поды, ноды NotReady). Ноды без агента - совет из "Требует действий"
  const live = s.online && kube.access
  const issues = srvIssues(s, t).filter((i) => i.section === 'kuber')
  const items: Record<string, SecItem[]> = {}
  const add = (sec: string, x: SecItem) => (items[KUBE_SEC_IDS.has(sec) ? sec : 'overview'] ??= []).push(x)
  for (const i of issues) add(i.sec ?? '', { level: i.tone === 't-down' ? 2 : 1, text: i.text, since: i.since })
  if (live && badPods.length)
    add('pods', {
      level: 2,
      count: badPods.length,
      since: s.alert_since?.kube_pod,
      text: t('падают поды: {list}', {
        list: shortList(badPods.map((p) => `${p.ns}/${p.name} (${p.reason || p.phase})`), t, 3),
      }),
    })
  // NotReady с бэкенда (kube_node) приходит после выдержки и про то же - без повтора
  const notReady = nodes.filter((n) => !n.ready)
  if (live && notReady.length && !issues.some((i) => i.kind === 'kube_node'))
    add('nodes', {
      level: 2,
      count: notReady.length,
      since: s.alert_since?.kube_node,
      text: t('ноды не готовы (NotReady): {list}', { list: shortList(notReady.map((n) => n.name), t, 3) }),
    })
  if (live && unmon.size)
    add('nodes', { level: 1, count: unmon.size, text: t('нод без агента: {list}', { list: shortList([...unmon.keys()], t, 3) }) })

  // коротко, что в разделе, когда замечаний нет
  const ok: Record<string, string> = {
    pods: podsActive ? t('работают {a} из {b}', { a: podsRunning, b: podsActive }) : t('Подов нет'),
    finished: t('завершенных подов: {n}, упали {m}', { n: doneCount, m: doneFailed }),
    workloads: workloads.length
      ? t('в полном составе {a} из {b}', { a: workloads.length - wlPartial.length, b: workloads.length })
      : t('Воркоадов нет'),
    cron: cronBad
      ? t('заданий: {n}, с проблемой {m}', { n: cronjobs.length, m: cronBad })
      : t('заданий: {n}, все отрабатывают', { n: cronjobs.length }),
    nodes: t('готовы {a} из {b}', { a: nodesReady, b: nodes.length }),
    expiry: next ? t('ближайший: {what}, {d}', { what: t(EXPIRY_KIND[next.kind] ?? next.kind), d: daysText(nextDays) }) : '',
    flux: t('ресурсов: {n}, все в Ready', { n: flux.length }),
  }
  const info: Record<string, SecInfo> = {}
  for (const { id } of KUBE_SECTIONS) info[id] = secInfoOf(items[id] ?? [], ok[id] ?? '', t)
  // Не проблема, но и не "все в порядке": поды и воркоады не в полном составе (идет выкат или
  // под не поднимается), Flux не в Ready раньше выдержки алерта. Желтым на карте, без числа в меню
  const soft = (id: string, line: string) => {
    if (!info[id].level) info[id] = { ...info[id], level: 1, line }
  }
  const podsWait = podsActive - podsRunning - badPods.length
  if (live && podsWait > 0) soft('pods', `${t('не готовы: {n}', { n: podsWait })}, ${ok.pods}`)
  if (live && wlPartial.length)
    soft('workloads', t('не в полном составе: {list}', {
      list: shortList(wlPartial.map((w) => `${w.ns}/${w.name} ${w.ready}/${w.desired}`), t, 3),
    }))
  if (fluxBroken.length) soft('flux', t('не в Ready: {n}', { n: fluxBroken.length }))
  // гигиена подов - справка, не проблема: серым, ведет в поды, где про нее написано
  const hyg: SecInfo = hygiene.nolim || hygiene.noprobe
    ? { level: 0, n: 0, idle: true, line: t('без лимита памяти {a}, без проб {b}', { a: hygiene.nolim, b: hygiene.noprobe }) }
    : { level: 0, n: 0, line: t('у всех подов есть лимиты и пробы') }

  // строки сводки: все замечания кластера, красные сверху, у каждой - раздел, где разбирать
  const rows: OvRow[] = KUBE_SECTIONS.flatMap(({ id, label }) =>
    (items[id] ?? []).map((x) => ({
      tone: x.level >= 2 ? ('t-down' as const) : ('t-degraded' as const),
      text: x.text,
      since: x.since,
      act: id !== 'overview' && shown[id] ? { label: t(label), onClick: () => go(id) } : undefined,
    })),
  ).sort((a, b) => Number(b.tone === 't-down') - Number(a.tone === 't-down'))
  const tileTone = (sec: string, base: 'bad' | 'warn' | 'ok' | undefined) =>
    info[sec].n > 0 && info[sec].level >= 2 ? 'bad' : base
  const tileLabel = (sec: string, label: string) =>
    info[sec].fresh ? <>{label} <FreshChip since={info[sec].fresh!} /></> : label

  const overview = (
    <div className="srv-sec">
      {!kube.access && <EnableBlock />}
      <OvRows rows={rows} />
      {kube.access && (
        <div className="net-tiles srv-tiles">
          <button className="srv-tile-btn" onClick={() => go('nodes')} title={t('к разделу')}>
            <NetTile
              label={tileLabel('nodes', t('Ноды готовы'))}
              value={String(nodesReady)}
              unit={t('из {n}', { n: nodes.length })}
              sub={subOf(
                nodesPress > 0 && t('под давлением: {n}', { n: nodesPress }),
                unmon.size > 0 && t('без агента: {n}', { n: unmon.size }),
              ) || t('все готовы')}
              tone={tileTone('nodes', nodesReady < nodes.length ? 'bad' : nodesPress || unmon.size ? 'warn' : 'ok')}
            />
          </button>
          <button className="srv-tile-btn" onClick={() => go('pods')} title={t('к разделу')}>
            <NetTile
              label={tileLabel('pods', t('Поды работают'))}
              value={String(podsRunning)}
              unit={t('из {n}', { n: podsActive })}
              sub={subOf(
                badPods.length > 0 && t('падают: {n}', { n: badPods.length }),
                podsWait > 0 && t('не готовы: {n}', { n: podsWait }),
              ) || t('все работают')}
              tone={tileTone('pods', badPods.length ? 'bad' : podsRunning < podsActive ? 'warn' : 'ok')}
            />
          </button>
          {memCap > 0 && (
            <button className="srv-tile-btn" onClick={() => go('nodes')} title={t('к разделу')}>
              <NetTile
                label={t('Память нод')}
                value={`${pct(memUse, memCap)}%`}
                sub={subOf(`${fmtBytes(memUse)} / ${fmtBytes(memCap)}`, cpuCap > 0 && `CPU ${pct(cpuUse, cpuCap)}%`)}
                tone={pct(memUse, memCap) >= 90 ? 'bad' : pct(memUse, memCap) >= 75 ? 'warn' : undefined}
              />
            </button>
          )}
          <button className="srv-tile-btn" onClick={() => { go('pods'); setSort('restarts') }} title={t('к разделу')}>
            <NetTile
              label={t('Перезапусков за сутки')}
              value={String(days.reduce((n, x) => n + x.n, 0))}
              sub={dayTop ? `${dayTop.name}: ${dayTop.n}` : t('нет')}
              tone={dayTop && dayTop.n >= RESTARTS_DAY_MIN ? 'warn' : undefined}
            />
          </button>
        </div>
      )}
      <OvAreas
        cap={t('Кластер по областям')}
        items={[
          ...KUBE_SECTIONS.filter((x) => x.id !== 'overview' && shown[x.id]).map((x) => ({ id: x.id, label: x.label, info: info[x.id] })),
          ...(kube.access && pods.length ? [{ id: 'hygiene', label: 'Гигиена подов', info: hyg, go: 'pods' }] : []),
        ]}
        onGo={go}
      />
    </div>
  )

  const empty = (text: string) => <div className="srv-empty">{text}</div>
  return createPortal(
    <div className="modal-backdrop">
      <div className="card detail detail-server hs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
            <OsIcon os={s.os} size={18} />
            <CountryFlag code={s.country} />
            <h3>{s.name}</h3>
            {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          </div>
          <div className="detail-head-actions">
            <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
              ✕
            </button>
          </div>
        </div>
        {!s.online && (
          <div className="srv-offline-note">
            <span className="srv-offline-ic">🔴</span>
            <span>
              <b>{t('Сервер недоступен')}</b>{' '}
              {t('— агент молчит {ago}. Всё, что ниже, — последние данные, снятые тогда же.', { ago: fmtRel(s.last_seen) })}
            </span>
          </div>
        )}
        <div className="srv-meta srv-meta-line">
          <span className="srv-meta-item" title={kube.version}>
            <span className="srv-meta-k">Kubernetes</span> {(kube.version || '—').split('+')[0]}
            {kube.flavor ? ` (${kube.flavor})` : ''}
          </span>
          {kube.access && (
            <>
              <span className="srv-meta-item"><span className="srv-meta-k">namespace</span> {kube.namespaces ?? 0}</span>
              <span className="srv-meta-item"><span className="srv-meta-k">{t('нод')}</span> {nodes.length}</span>
              <span className="srv-meta-item"><span className="srv-meta-k">{t('подов')}</span> {podsActive}</span>
            </>
          )}
          <span className="srv-meta-item">
            <span className="srv-meta-k">{t('обновлено')}</span> {fmtRel(s.last_seen)}
          </span>
        </div>
        <div className="srv-layout" ref={layoutRef}>
          <CardNav secs={secs} active={cur.id} info={info} onGo={go} />
          <div className="srv-main">
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
              {KUBE_FILTER.has(cur.id) && (
                <div className="modal-toolbar srv-head-tools">
                  <input
                    className="checks-search modal-search"
                    placeholder={t('Фильтр: имя, namespace…')}
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                  />
                  {(cur.id === 'pods' || cur.id === 'finished') && (
                    <select className="modal-sort" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
                      <option value="state">{t('сорт: статус')}</option>
                      <option value="name">{t('сорт: имя')}</option>
                      <option value="restarts">{t('сорт: рестарты')}</option>
                      <option value="ns">{t('сорт: namespace')}</option>
                      {hasUse && <option value="mem">{t('сорт: память')}</option>}
                      {hasUse && <option value="cpu">{t('сорт: CPU')}</option>}
                    </select>
                  )}
                </div>
              )}
            </div>

            {cur.id === 'overview' && overview}

            {(cur.id === 'pods' || cur.id === 'finished') && (
              <div className="srv-sec">
                {cur.id === 'finished' && (
                  <p className="muted small kube-hint">
                    {t('Завершённые Job/CronJob-поды — историческая запись, а не живой воркоад. Их не нужно чинить; удалите под, чтобы убрать из списка.')}
                  </p>
                )}
                {cur.id === 'pods' && (hygiene.nolim > 0 || hygiene.noprobe > 0) && (
                  <div className="net-note">
                    {t('Без лимита памяти: {a}, без проб: {b}. Не авария, но под без лимита может съесть ноду, а без проб зависший под никто не перезапустит.', {
                      a: hygiene.nolim,
                      b: hygiene.noprobe,
                    })}
                  </div>
                )}
                <div className="loc-results docker-clist">
                  {pods.length === 0
                    ? empty(t('Подов нет'))
                    : fpods.length === 0
                      ? empty(t('Ничего не найдено.'))
                      : fpods.map((p) => (
                          <PodRow
                            key={p.ns + '/' + p.name}
                            serverId={s.id}
                            p={p}
                            canAct={canAct}
                            onLogs={() => setLogsPod(p)}
                            onChanged={onChanged}
                            day={s.restarts_day?.[`pod:${p.ns}/${p.name}`] ?? 0}
                          />
                        ))}
                </div>
              </div>
            )}

            {cur.id === 'workloads' && (
              <div className="loc-results docker-clist">
                {workloads.length === 0
                  ? empty(t('Воркоадов нет'))
                  : fworkloads.length === 0
                    ? empty(t('Ничего не найдено.'))
                    : fworkloads.map((w) => (
                        <WorkloadRow
                          key={w.kind + '/' + w.ns + '/' + w.name}
                          serverId={s.id}
                          w={w}
                          canAct={canAct}
                          onChanged={onChanged}
                        />
                      ))}
              </div>
            )}

            {cur.id === 'cron' && (
              <div className="srv-sec">
                <p className="muted small kube-hint">
                  {t('Задания по расписанию: последний запуск и последний успешный. Упавший или переставший отрабатывать CronJob дает алерт.')}
                </p>
                {failedJobs.length > 0 && (
                  <div className="net-prob">
                    {t('Упавшие за неделю Job, запущенные не по расписанию (миграции, разовые задачи): {n}', { n: failedJobs.length })}
                  </div>
                )}
                <div className="loc-results docker-clist">
                  {failedJobs.map((j) => (
                    <div className="loc-res docker-row t-down" key={`job/${j.ns}/${j.name}/${j.ts ?? 0}`}>
                      <span className="lk-dot lk-dot-bad" />
                      <div className="docker-c-main">
                        <div className="docker-c-name mono">
                          {j.ns}/{j.name}
                          <span className="type-chip">Job</span>
                        </div>
                        <div className="docker-c-img mono muted small">
                          {j.ts ? t('упал {a} назад', { a: sinceShort(new Date(j.ts * 1000).toISOString(), t) || t('только что') }) : ''}
                        </div>
                      </div>
                      <div className="docker-c-status mono small t-down">{j.reason || 'Failed'}</div>
                    </div>
                  ))}
                  {cronjobs
                    .filter((c) => !ql || `${c.ns}/${c.name} ${c.image ?? ''}`.toLowerCase().includes(ql))
                    .map((c) => {
                      const key = `${c.ns}/${c.name}`
                      const issue = cronIssues[key]
                      const ago = (ts?: number) => (ts ? sinceShort(new Date(ts * 1000).toISOString(), t) || t('только что') : '')
                      const tone = issue ? (c.suspend ? 't-degraded' : 't-down') : c.suspend ? '' : 't-up'
                      return (
                        <div className={`loc-res docker-row ${tone}`} key={key}>
                          <span className={`lk-dot${DOT[tone] ?? ''}`} />
                          <div className="docker-c-main">
                            <div className="docker-c-name mono">
                              {key}
                              {c.schedule && <span className="type-chip">{c.schedule}</span>}
                            </div>
                            <div className="docker-c-img mono muted small">
                              {c.last_schedule
                                ? t('запуск {a} назад', { a: ago(c.last_schedule) })
                                : t('еще не запускался')}
                              {c.last_success ? ` · ${t('успешный {a} назад', { a: ago(c.last_success) })}` : ''}
                              {c.image ? ` · ${c.image}` : ''}
                            </div>
                          </div>
                          <div className={`docker-c-status mono small ${tone}`}>
                            {issue || (c.suspend ? t('приостановлен') : c.active ? t('идет') : t('ок'))}
                          </div>
                        </div>
                      )
                    })}
                </div>
              </div>
            )}

            {cur.id === 'nodes' && (
              <div className="srv-sec">
                {unmon.size > 0 && (
                  <div className="net-note">
                    {t('Нод без агента: {n}. На них панель не видит диски, SMART, упавшие юниты и процессы - только то, что отдает Kubernetes.', { n: unmon.size })}
                  </div>
                )}
                <div className="loc-results docker-clist">
                  {nodes.map((n) => {
                    const tone = !n.ready ? 't-down' : (n.pressure ?? []).length ? 't-degraded' : 't-up'
                    return (
                      <div className={`loc-res docker-row ${tone}`} key={n.name}>
                        <span className={`lk-dot${DOT[tone]}`} />
                        <div className="docker-c-main">
                          <div className="docker-c-name mono">
                            {n.name}
                            {n.roles && <span className="type-chip">{n.roles}</span>}
                            {unmon.has(n.name) && <span className="type-chip t-degraded">{t('без агента')}</span>}
                            {(n.pressure ?? []).map((c) => (
                              <span className="type-chip off" key={c}>{c}</span>
                            ))}
                            {ignoredNodes.has(n.name) && (
                              <span className="type-chip muted" title={t('отмечено, что агент здесь не нужен')}>
                                {t('агент не нужен')}
                              </span>
                            )}
                          </div>
                          <div className="docker-c-img kube-node-line mono muted small">
                            {n.version || ''}{n.ip ? ` · ${n.ip}` : ''}
                          </div>
                          {n.use && (
                            <div className="docker-c-img kube-node-line mono muted small">
                              {t('память {u} из {c}', { u: fmtBytes(n.use.mem), c: n.cap ? fmtBytes(n.cap.mem) : '?' })}
                              {n.cap?.mem ? ` (${Math.round((n.use.mem / n.cap.mem) * 100)}%)` : ''}
                              {' · '}
                              {t('CPU {u} из {c}', { u: fmtCores(n.use.cpu_m), c: n.cap ? fmtCores(n.cap.cpu_m) : '?' })}
                            </div>
                          )}
                          {canAct && (unmon.has(n.name) || ignoredNodes.has(n.name)) && (
                            <div className="kube-node-acts">
                              {unmon.has(n.name) ? (
                                <>
                                  <button className="small" onClick={() => onAddNode(unmon.get(n.name)!)}>
                                    {t('Добавить в панель')}
                                  </button>
                                  <button className="ghost small" disabled={nodeBusy === n.name}
                                    onClick={() => setNodeIgnored(n.name, true)}>
                                    {t('Агент не нужен')}
                                  </button>
                                </>
                              ) : (
                                <button className="ghost small" disabled={nodeBusy === n.name}
                                  onClick={() => setNodeIgnored(n.name, false)}>
                                  {t('Вернуть подсказку')}
                                </button>
                              )}
                              {nodeErr && nodeErr.node === n.name && <span className="form-error small">{nodeErr.msg}</span>}
                            </div>
                          )}
                        </div>
                        <div className={`docker-c-status mono small ${n.ready ? 't-up' : 't-down'}`}>
                          {n.ready ? 'Ready' : 'NotReady'}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}

            {cur.id === 'expiry' && (
              <div className="srv-sec">
                <p className="muted small kube-hint">
                  {t('Сертификаты, kubeconfig-и и токены Flux с их сроками. Собирает root-хелпер на самой ноде: панель токенов не видит.')}
                </p>
                <div className="loc-results docker-clist">
                  {expiry.map((e) => {
                    const d = daysLeft(e.expires)
                    const tone = expiryTone(d, horizon)
                    return (
                      <div className={`loc-res docker-row ${tone}`} key={e.kind + '/' + e.where + '/' + (e.note ?? '')}>
                        <span className={`lk-dot${DOT[tone] ?? ' lk-dot-ok'}`} />
                        <div className="docker-c-main">
                          <div className="docker-c-name mono">
                            {e.where}
                            <span className="type-chip">{t(EXPIRY_KIND[e.kind] ?? e.kind)}</span>
                          </div>
                          <div className="docker-c-img mono muted small">
                            {new Date(e.expires * 1000).toLocaleDateString()}
                            {e.note ? ` · ${e.note}` : ''}
                          </div>
                        </div>
                        <div className={`docker-c-status mono small ${tone}`}>{daysText(d)}</div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}

            {cur.id === 'flux' && (
              <div className="srv-sec">
                <p className="muted small kube-hint">
                  {t('Ресурсы Flux и их готовность. Не в Ready — доставка встала: запущенное продолжает работать, а новые изменения из git не доезжают.')}
                </p>
                {fluxBroken.length > 0 && (
                  <div className="net-prob">
                    {t('Доставка Flux встала: {n} ресурс(ов) не в Ready. Уже запущенное продолжает работать — по метрикам это не видно.', {
                      n: fluxBroken.length,
                    })}
                  </div>
                )}
                <div className="loc-results docker-clist">
                  {fflux.length === 0 && empty(t('Ничего не найдено.'))}
                  {fflux.map((f) => {
                    const tone = f.ready ? 't-up' : FLUX_TRANSIENT.has(f.reason ?? '') ? '' : 't-down'
                    return (
                      <div className={`loc-res docker-row ${tone}`} key={f.kind + '/' + f.where}>
                        <span className={`lk-dot${DOT[tone] ?? ''}`} />
                        <div className="docker-c-main">
                          <div className="docker-c-name mono">
                            {f.where}
                            <span className="type-chip">{f.kind}</span>
                          </div>
                          <div className="docker-c-img mono muted small">
                            {f.reason || ''}
                            {f.message ? ` · ${f.message}` : ''}
                          </div>
                        </div>
                        <div className={`docker-c-status mono small ${f.ready ? 't-up' : 't-down'}`}>
                          {f.ready ? 'Ready' : 'NotReady'}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
      {logsPod && <PodLogsModal server={s} pod={logsPod} onClose={() => setLogsPod(null)} />}
    </div>,
    document.body,
  )
}

// Строка кластера: точка - на связи ли нода, справа работающие поды из тех, что должны
// работать. Красное - падают поды или нода не готова, желтое - не все поды готовы
function HostRow({
  server: s,
  kube,
  showGroup,
  onOpen,
}: {
  server: Server
  kube: KubeInfo
  showGroup?: boolean
  onOpen: () => void
}) {
  const { t } = useI18n()
  const nodes = kube.nodes ?? []
  const pods = kube.pods ?? []
  const nodesReady = nodes.filter((n) => n.ready).length
  const { running, active } = podCounts(pods)
  const bad = pods.filter((p) => !podFinished(p) && podTone(p) === 't-down').length
  // Сроки видны прямо в списке: истёкший токен Flux не меняет ни одного счётчика
  // подов, и без отдельной пометки хост в списке выглядит совершенно здоровым.
  const fluxBroken = (s.last_report?.flux ?? []).filter(
    (f) => !f.ready && !FLUX_TRANSIENT.has(f.reason ?? ''),
  ).length
  const soon = (s.last_report?.kube_expiry ?? []).filter((e) => daysLeft(e.expires) <= expiryHorizon(s)).length
  return (
    <button className="loc-res loc-res-btn hs-host" onClick={onOpen}>
      <span className={`lk-dot ${s.online ? 'lk-dot-ok' : 'lk-dot-bad'}`} title={s.online ? t('на связи') : t('не на связи')} />
      <div className="hs-host-main">
        <div className="hs-host-name">
          <OsIcon os={s.os} />
          <CountryFlag code={s.country} />
          {s.name}
          {showGroup && s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          {!kube.access && <span className="type-chip off">{t('нет доступа')}</span>}
          {bad > 0 && (
            <span className="type-chip off" title={t('поды падают: CrashLoopBackOff, ошибка образа или запуска')}>
              🔥 {bad}
            </span>
          )}
          {kube.access && nodesReady < nodes.length && (
            <span className="type-chip off" title={t('ноды не в Ready')}>
              {t('ноды {r}/{n}', { r: nodesReady, n: nodes.length })}
            </span>
          )}
          {fluxBroken > 0 && (
            <span className="type-chip t-down" title={t('Ресурсы Flux не в Ready — доставка встала')}>
              {t('Flux ⚠ {n}', { n: fluxBroken })}
            </span>
          )}
          {soon > 0 && (
            <span className="type-chip t-degraded" title={t('Сертификаты или токены истекают в ближайшие две недели')}>
              {t('сроки ⏳ {n}', { n: soon })}
            </span>
          )}
        </div>
        <div className="mono muted small">
          {kube.flavor || 'k8s'} {kube.version || '—'}
          {kube.access ? ` · ${t('ноды {r}/{n}', { r: nodesReady, n: nodes.length })}` : ''}
        </div>
      </div>
      <div className="hs-host-count mono">
        {kube.access ? (
          <span className={bad || nodesReady < nodes.length ? 't-down' : running < active ? 't-degraded' : 't-up'}>
            {t('{r}/{n}', { r: running, n: active })}
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </div>
    </button>
  )
}

type KHost = { s: Server; d: KubeInfo }

export function KuberPage({
  onUnauthorized,
  openHostId = null,
  openTab = null,
  onConsumed,
}: {
  onUnauthorized: () => void
  openHostId?: number | null
  openTab?: string | null // вкладка кластера из ссылки алерта (?ktab=expiry)
  onConsumed?: () => void
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [servers, setServers] = useState<Server[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [openId, setOpenId] = useState<number | null>(openHostId)
  // Вкладка из ссылки алерта. Номер кластера из ссылки App забывает сразу, а список
  // кластеров приезжает позже - поэтому вкладку держим здесь до первого открытия.
  const [linkTab, setLinkTab] = useState<{ id: number | null; tab: string | null }>(
    { id: openHostId, tab: openTab },
  )
  useEffect(() => {
    if (openHostId != null) {
      setOpenId(openHostId)
      setLinkTab({ id: openHostId, tab: openTab })
      onConsumed?.()
    }
  }, [openHostId, openTab, onConsumed])
  useUrlCard('kube', openId)
  const [query, setQuery] = useState('')
  // нода кластера без агента, которую заводят в панели, и команда установки после этого
  const [addNode, setAddNode] = useState<{ name: string; group: string; ip: string } | null>(null)
  const [enroll, setEnroll] = useState<ServerEnroll | null>(null)
  const [groupBy, setGroupBy] = useState<'none' | 'group'>(
    () => (localStorage.getItem('kervax_kube_groupby') as 'none' | 'group') || 'group',
  )
  const setGrouping = (g: 'none' | 'group') => {
    setGroupBy(g)
    localStorage.setItem('kervax_kube_groupby', g)
  }

  const load = useCallback(() => {
    listServers()
      .then((s) => {
        setServers(s)
        setErr(null)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        setErr(e instanceof Error ? e.message : t('Ошибка'))
      })
  }, [onUnauthorized, t])

  useEffect(() => {
    load()
    const id = window.setInterval(load, 12000)
    return () => window.clearInterval(id)
  }, [load])

  const allHosts = (servers ?? [])
    .map((s) => ({ s, d: s.last_report?.kube }))
    .filter((x): x is KHost => !!x.d?.present)
  const q = query.trim().toLowerCase()
  const hosts = allHosts
    .filter(({ s, d }) => {
      if (!q) return true
      const hay = [
        s.name,
        s.group_name,
        `${d.flavor ?? ''} ${d.version ?? ''}`,
        ...(d.nodes ?? []).map((n) => n.name),
        ...(d.pods ?? []).flatMap((p) => [p.name, p.ns]),
        ...(d.workloads ?? []).map((w) => w.name),
      ]
        .join(' ')
        .toLowerCase()
      return hay.includes(q)
    })
    .sort((a, b) => a.s.name.localeCompare(b.s.name))

  const groups: { key: string; label: string; items: KHost[] }[] =
    groupBy === 'none'
      ? [{ key: 'all', label: '', items: hosts }]
      : (() => {
          const map = new Map<string, KHost[]>()
          for (const h of hosts) {
            const k = h.s.group_name?.trim() || ''
            if (!map.has(k)) map.set(k, [])
            map.get(k)!.push(h)
          }
          return [...map.keys()]
            .sort((a, b) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)))
            .map((k) => ({ key: k, label: k || t('Без группы'), items: map.get(k)! }))
        })()

  const open = allHosts.find(({ s }) => s.id === openId)
  // сводка по всем кластерам: ноды, поды и то, что встало или скоро истечет
  const noAccess = allHosts.filter(({ d }) => !d.access).length
  const offline = allHosts.filter(({ s }) => !s.online).length
  const nAll = allHosts.flatMap(({ d }) => (d.access ? d.nodes ?? [] : []))
  const nReady = nAll.filter((n) => n.ready).length
  const nPress = nAll.filter((n) => n.ready && (n.pressure ?? []).length > 0).length
  const nUnmon = allHosts.reduce((n, { s }) => n + (s.kube_unmonitored ?? []).length, 0)
  const pAll = allHosts.flatMap(({ d }) => (d.access ? d.pods ?? [] : []))
  const { running: pRun, active: pAct } = podCounts(pAll)
  const pBad = pAll.filter((p) => !podFinished(p) && podTone(p) === 't-down').length
  const fluxN = allHosts.filter(({ s }) => (s.last_report?.flux ?? []).some((f) => !f.ready && !FLUX_TRANSIENT.has(f.reason ?? ''))).length
  const expiredN = allHosts.filter(({ s }) => (s.last_report?.kube_expiry ?? []).some((e) => daysLeft(e.expires) < 0)).length
  const soonN = allHosts.filter(({ s }) =>
    (s.last_report?.kube_expiry ?? []).some((e) => daysLeft(e.expires) >= 0 && daysLeft(e.expires) <= expiryHorizon(s))).length
  const cronN = allHosts.filter(({ s }) =>
    Object.keys(s.kube_cron_issues ?? {}).length > 0 || (s.last_report?.kube?.failed_jobs ?? []).length > 0).length
  const attn = allHosts.filter(({ s }) =>
    (s.last_report?.flux ?? []).some((f) => !f.ready && !FLUX_TRANSIENT.has(f.reason ?? '')) ||
    (s.last_report?.kube_expiry ?? []).some((e) => daysLeft(e.expires) <= expiryHorizon(s)) ||
    Object.keys(s.kube_cron_issues ?? {}).length > 0 ||
    (s.last_report?.kube?.failed_jobs ?? []).length > 0).length

  return (
    <div>
      <div className="checks-head">
        <h2 className="docker-title">
          <KubeLogo /> Kubernetes
        </h2>
        <div className="checks-head-actions">
          {allHosts.length > 1 && (
            <div className="win-switch">
              <button
                className={`win-btn${groupBy === 'group' ? ' win-btn-active' : ''}`}
                onClick={() => setGrouping('group')}
              >
                {t('По группе')}
              </button>
              <button
                className={`win-btn${groupBy === 'none' ? ' win-btn-active' : ''}`}
                onClick={() => setGrouping('none')}
              >
                {t('Без групп')}
              </button>
            </div>
          )}
        </div>
      </div>
      {allHosts.length > 0 && (
        <div className="net-tiles hs-tiles">
          <NetTile
            label={t('Кластеры')}
            value={String(allHosts.length)}
            sub={subOf(
              noAccess > 0 && t('без доступа: {n}', { n: noAccess }),
              offline > 0 && t('не на связи: {n}', { n: offline }),
            ) || t('все на связи')}
            tone={noAccess || offline ? 'warn' : 'ok'}
          />
          <NetTile
            label={t('Ноды готовы')}
            value={String(nReady)}
            unit={t('из {n}', { n: nAll.length })}
            sub={subOf(
              nPress > 0 && t('под давлением: {n}', { n: nPress }),
              nUnmon > 0 && t('без агента: {n}', { n: nUnmon }),
            ) || t('все готовы')}
            tone={nReady < nAll.length ? 'bad' : nPress || nUnmon ? 'warn' : 'ok'}
          />
          <NetTile
            label={t('Поды работают')}
            value={String(pRun)}
            unit={t('из {n}', { n: pAct })}
            sub={subOf(
              pBad > 0 && t('падают: {n}', { n: pBad }),
              pAct - pRun - pBad > 0 && t('не готовы: {n}', { n: pAct - pRun - pBad }),
            ) || t('все работают')}
            tone={pBad ? 'bad' : pRun < pAct ? 'warn' : 'ok'}
          />
          <NetTile
            label={t('Кластеры с замечаниями')}
            value={String(attn)}
            sub={subOf(
              fluxN > 0 && t('Flux встал: {n}', { n: fluxN }),
              expiredN > 0 && t('сроки истекли: {n}', { n: expiredN }),
              soonN > 0 && t('сроки скоро: {n}', { n: soonN }),
              cronN > 0 && t('задания упали: {n}', { n: cronN }),
            ) || t('все в порядке')}
            tone={fluxN || expiredN ? 'bad' : attn ? 'warn' : 'ok'}
          />
        </div>
      )}
      {allHosts.length > 1 && (
        <div className="checks-search-row">
          <input
            className="checks-search"
            placeholder={t('Поиск: хост, группа, нода, под, воркоад…')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button className="ghost search-clear" onClick={() => setQuery('')}>
              {t('Сбросить')}
            </button>
          )}
        </div>
      )}
      {err && <p className="form-error">{err}</p>}
      {servers && allHosts.length === 0 && (
        <div className="card muted">
          {t('Kubernetes не найден ни на одном сервере. Агент определяет кластер сам (k0s/k3s/microk8s/kubeadm); если он есть, но раздел пуст — обновите агент.')}
        </div>
      )}
      {servers && allHosts.length > 0 && hosts.length === 0 && (
        <div className="card muted">{t('Ничего не найдено.')}</div>
      )}
      {hosts.length > 0 && (
        <div className="net-cards hs-groups">
          {groups.map((g) => (
            <div key={g.key} className="chart-card net-card net-card-wide">
              <div className="net-head">
                <div className="chart-cap net-cap">
                  {groupBy === 'none' ? t('Кластеры') : g.label}
                  <span className="net-n">{g.items.length}</span>
                </div>
              </div>
              <div className="loc-results">
                {g.items.map(({ s, d }) => (
                  <HostRow
                    key={s.id}
                    server={s}
                    kube={d}
                    showGroup={groupBy !== 'group'}
                    onOpen={() => setOpenId(s.id)}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
      {open && (
        <KubeHostModal
          key={open.s.id}
          server={open.s}
          kube={open.d}
          canAct={!isViewer && open.d.access}
          initialTab={linkTab.id === open.s.id ? linkTab.tab : null}
          onClose={() => {
            setOpenId(null)
            setLinkTab({ id: null, tab: null })
          }}
          onChanged={load}
          onAddNode={(n) => setAddNode({ name: n.name, group: open.s.group_name || '', ip: n.public ? n.ip : '' })}
        />
      )}
      {/* поверх модалки кластера: порталом и позже нее, иначе форма оказалась бы под ней */}
      {addNode &&
        createPortal(
          <EnrollModal
            servers={servers ?? []}
            groups={[...new Set((servers ?? []).map((x) => x.group_name).filter(Boolean) as string[])]}
            initial={addNode}
            onClose={() => setAddNode(null)}
            onEnrolled={(e) => {
              setEnroll(e)
              setAddNode(null)
              load()
            }}
            onUnauthorized={onUnauthorized}
          />,
          document.body,
        )}
      {enroll && createPortal(<InstallModal enroll={enroll} onClose={() => setEnroll(null)} />, document.body)}
    </div>
  )
}
