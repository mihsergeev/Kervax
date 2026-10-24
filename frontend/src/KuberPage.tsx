import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  kubeCommand,
  kubeCommandStatus,
  logsErrorText,
  logsTargetGone,
  kubeExpiryIgnore,
  kubeNodeIgnore,
  listServers,
  podFinished,
  snoozeServerAlert,
  muteServerKeys,
  type FluxState,
  type KubeCommand,
  type KubeCronJob,
  type KubeExpiry,
  type KubeInfo,
  type KubeNodeRef,
  type KubePod,
  type KubeWorkload,
  type MemLimitItem,
  type Server,
  type ServerEnroll,
  cmdState,
  cmdWaitText,
} from './api'
import { useAuth } from './auth'
import { useUrlCard, useUrlParam } from './deeplink'
import { FixSteps } from './FixSteps'
import { helperCmd, hostOf } from './FleetUpdate'
import { KUBE_NAME_RE } from './shell'
import { useI18n } from './i18n'
import { toastError } from './toast'
import { kindLabel, KUBE_WORK_KINDS, RESTARTS_DAY_MIN } from './mutes'
import { OsIcon } from './osIcon'
import { CountryFlag } from './CountryFlag'
import { fmtBytes } from './units'
import { ipHit, memLimChip, shortList, sinceShort, srvIps, srvIssues, srvMuted } from './serverUtils'
import { EnrollModal, fmtRel, InstallModal } from './ServersPage'
import { CardNav, FreshChip, markMuted, OvAreas, OvRows, secInfoOf, type OvRow, type SecDef, type SecInfo, type SecItem } from './cardLayout'
import { NetTile, TileFilterChip, useTileFilter } from './NetLinks'
import { KubeRestartsSec } from './KubeRestarts'
import { LockedLogs, LOGS_LOCKED } from './LogsLocked'

// Вкладка «Кубер»: КОМПАКТНЫЙ список хостов с кластером; клик по хосту → модалка
// с нодами, воркоадами и подами (статусы, рестарты, логи, управление). Агент читает
// kube-api по токену УЗКОГО ServiceAccount (kube-setup.sh) — read + точечный write
// (rollout restart / delete pod), не cluster-admin. Логи не хранятся — разовый tail.

// команда для включения доступа (kube-setup.sh на control-plane ноде): создаёт
// ServiceAccount kervax-agent с узким RBAC (read + rollout restart + delete pod),
// кладёт токен в /etc/kervax/kube.json (root:kervax), перезапускает агент.
const KUBE_ENABLE_CMD = `curl -fsSL ${window.location.origin}/api/agent/kube-setup.sh | sudo bash
sudo systemctl restart kervax-agent`
// то же плейбуком из репо ansible: kube-setup помечен "только вручную" и массовым прогоном не
// ставится, -e kervax_install называет его явно, плейбук ставит и перезапускает агент
const kubeAnsibleCmd = (s: Server) => `${helperCmd([hostOf(s)])} -e kervax_install=kube-setup`

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
// Сроки, за которыми следим: без отметки "не нужен, не следить". Бэкенд отдает только действующие
// отметки: у обновленного секрета другой срок или отпечаток, и он возвращается под присмотр сам
const expKey = (e: KubeExpiry) => `${e.kind}|${e.where}`
// и без секретов, на которые ничего не ссылается (kube_expiry_unused, helper 0.4): не проблема
function liveExpiry(s: Server): KubeExpiry[] {
  const ign = s.kube_expiry_ignored ?? {}
  const unused = new Set(s.kube_expiry_unused ?? [])
  return (s.last_report?.kube_expiry ?? []).filter((e) => !ign[expKey(e)] && !unused.has(expKey(e)))
}
// TLS-секрет, который не указан ни в одном Ingress (helper 0.3): истекший такой скорее забыт, чем
// сломан, и показывается мягче. Нет поля - helper старый или Ingress не прочитались, не знаем
const unusedSecret = (e: KubeExpiry) => e.kind === 'secret-cert' && e.ingress === false
// Удалить секрет панель не может и не должна (она ничего не меняет в кластере): команда для человека
// Имена с ноды: команду даем, только если namespace и секрет похожи на имена Kubernetes
function deleteSecretCmd(e: KubeExpiry): string {
  const i = e.where.indexOf('/')
  const ns = e.where.slice(0, i)
  const name = e.where.slice(i + 1)
  return e.kind === 'secret-cert' && i > 0 && KUBE_NAME_RE.test(ns) && KUBE_NAME_RE.test(name)
    ? `kubectl -n ${ns} delete secret ${name}` : ''
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

type KAction = 'rollout_restart' | 'delete_pod' | 'logs' | 'logs_prev'

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

function EnableBlock({ server }: { server: Server }) {
  const { t } = useI18n()
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="chart-cap">{t('Нет доступа к kube-api')}</div>
      </div>
      <div className="net-note">
        {t('Кластер есть, но у агента нет доступа к kube-api')}
      </div>
      <FixSteps steps={[{ id: 'enable', cmd: KUBE_ENABLE_CMD }, { id: 'ansible', alt: true, cmd: kubeAnsibleCmd(server) }]}
        text={(st) => st.id === 'ansible'
          ? t('через ansible: плейбук поставит kube-setup и перезапустит агент')
          : t('Включить доступ на чтение и точечное управление')}
        foot={t('Скрипт создает ServiceAccount с узким RBAC (не cluster-admin) и кладет токен для агента.')} />
    </div>
  )
}

// Под упавшего Job: <job>-<5 символов>, у индексного <job>-<номер>-<5 символов>; длинное имя
// Kubernetes обрезает до 58 символов. Тот же разбор, что у лога в алерте (app/job_logs.py)
function jobPod(pods: KubePod[], ns: string, name: string): KubePod | null {
  const esc = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const re = new RegExp(`^${esc}-(?:\\d+-)?[a-z0-9]{5}$`)
  const base = `${name}-`
  const mine = pods.filter(
    (p) =>
      p.ns === ns &&
      p.owner === 'Job' &&
      (re.test(p.name) || (base.length > 58 && p.name.length === 63 && p.name.startsWith(base.slice(0, 58)))),
  )
  return mine.find((p) => p.phase === 'Failed') ?? mine[0] ?? null
}

// метка времени в начале строки (логи идут с timestamps) в коротком хвосте только мешает
const LOG_TS = /^\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d) /

// Конец лога упавшего Job прямо в списке: раздел открывают из алерта, чтобы узнать, почему
// упал. Весь лог - модалкой, как у пода
function JobLog({ server, pod, onOpen }: { server: Server; pod: KubePod; onOpen: () => void }) {
  const { t } = useI18n()
  const [text, setText] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    runAndWait(server.id, pod.ns, 'pod', pod.name, 'logs', { tail: 40 })
      .then((r) => {
        if (!alive) return
        if (r.status === 'done')
          setText((r.result ?? '').split('\n').map((l) => l.replace(LOG_TS, '')).filter((l) => l.trim()).slice(-25).join('\n'))
        else if (logsTargetGone(r)) setErr(t('Под уже удален, его лога больше нет'))
        else setErr(logsErrorText(r.result) || t('не удалось получить логи'))
      })
      .catch(() => alive && setErr(t('не удалось получить логи')))
    return () => {
      alive = false
    }
  }, [server.id, pod.ns, pod.name, t])
  return (
    <div className="job-log">
      <div className="job-log-head small">
        <span className="muted mono">
          {t('под {pod}, конец лога', { pod: pod.name })}
          {pod.reason ? ` (${pod.reason})` : ''}
        </span>
        <button className="ghost small" onClick={onOpen}>
          {t('Весь лог')}
        </button>
      </div>
      {err ? (
        <div className="form-error small">{err}</div>
      ) : text == null ? (
        <div className="muted small">{t('Запрашиваю логи у агента…')}</div>
      ) : text === '' ? (
        <div className="muted small">{t('в лог ничего не написал')}</div>
      ) : (
        <pre className="web-err-lines mono">{text}</pre>
      )}
    </div>
  )
}

// CronJob из ссылки алерта (?kube=id&sec=cron&cj=ns/имя&log=1): адрес снят при запуске, пока
// список кластеров грузится, панель успевает переписать строку без него. Берется один раз:
// первая открытая карточка кластера его забывает
let linkCj = new URLSearchParams(window.location.search).get('cj')
const linkCjLog = new URLSearchParams(window.location.search).get('log') === '1'

// Job последнего запуска по расписанию называется <cronjob>-<минуты unix>: так его видно и у
// старого агента, который не присылает last
function cronLastJob(c: KubeCronJob): string {
  return c.last?.job || (c.last_schedule ? `${c.name}-${Math.floor(c.last_schedule / 60)}` : '')
}

// Почему упал или не запускается: над логом последнего запуска. Строка лога из отчета - только
// тому, кому можно читать логи
function CronCause({ cj, issue, canLogs }: { cj: KubeCronJob; issue?: string; canLogs: boolean }) {
  const { t } = useI18n()
  const ago = (ts?: number) => (ts ? sinceShort(new Date(ts * 1000).toISOString(), t) || t('только что') : '')
  const last = cj.last
  const job = cronLastJob(cj)
  const st = !last?.st ? '' : last.st === 'ok' ? t('успешно') : last.st === 'failed' ? t('упал') : t('идет')
  const cause = [last?.reason, last?.pr && last.pr !== 'Error' && last.pr !== last.reason ? last.pr : '',
    last?.x != null && last.x !== 0 ? t('код {x}', { x: last.x }) : ''].filter(Boolean).join(', ')
  const evFresh = !!cj.ev && cj.ev.t > (cj.last_schedule ?? 0)
  const facts: string[] = []
  if (cj.suspend) facts.push(t('приостановлен (suspend: true)'))
  if (cj.policy) facts.push(`concurrencyPolicy ${cj.policy}`)
  if (cj.deadline) facts.push(`startingDeadlineSeconds ${cj.deadline}`)
  if (cj.active && cj.active_job) facts.push(t('идет Job {job}', { job: cj.active_job }))
  return (
    <div className="cron-cause">
      {issue && <div className="net-prob">{issue}</div>}
      {job ? (
        <div className="small mono">
          Job {job}
          {(last?.t || cj.last_schedule) ? ` · ${t('запуск {a} назад', { a: ago(last?.t || cj.last_schedule) })}` : ''}
          {st ? ` · ${st}` : last?.gone ? ` · ${t('чем кончился, неизвестно')}` : ''}
          {cause && !(issue ?? '').includes(cause) ? `: ${cause}` : ''}
        </div>
      ) : (
        <div className="small muted">{t('Запусков еще не было')}</div>
      )}
      {last?.msg && last.st === 'failed' && <div className="small muted mono">{last.msg}</div>}
      {last?.log && canLogs && (
        <div className="small mono">
          {t('строка лога:')} {last.log}
        </div>
      )}
      {cj.ev && (
        <div className={`small mono${evFresh && cj.ev.w ? ' t-down' : ' muted'}`}>
          {t('событие {r} {a} назад', { r: cj.ev.r, a: ago(cj.ev.t) })}
          {cj.ev.m ? `: ${cj.ev.m}` : ''}
        </div>
      )}
      {facts.length > 0 && <div className="small muted mono">{facts.join(' · ')}</div>}
    </div>
  )
}

// Лог последнего запуска CronJob тем же окном, что у подов, с причиной наверху. Пода нет (Job
// вычищен по failedJobsHistoryLimit или ttlSecondsAfterFinished) - так и пишем, и что известно
// из событий. Учетке только для просмотра лог - по галке, причина видна всем
function CronRunModal({
  server,
  cj,
  pods,
  issue,
  onClose,
  onGone,
}: {
  server: Server
  cj: KubeCronJob
  pods: KubePod[]
  issue?: string
  onClose: () => void
  onGone?: () => void
}) {
  const { t } = useI18n()
  const { canLogs } = useAuth()
  const last = cj.last
  const job = cronLastJob(cj)
  const found = (last?.pod ? pods.find((p) => p.ns === cj.ns && p.name === last.pod) : null) ?? (job ? jobPod(pods, cj.ns, job) : null)
  const podName = found?.name ?? (last?.gone ? '' : last?.pod ?? '')
  const pod: KubePod | null = podName
    ? found ?? { ns: cj.ns, name: podName, phase: '', ready: false, restarts: 0, owner: 'Job' }
    : null
  const gone = t('Лог не сохранился: Job удален по истории')
  const note = <CronCause cj={cj} issue={issue} canLogs={canLogs} />
  if (pod && canLogs) return <PodLogsModal server={server} pod={pod} note={note} goneText={gone} onClose={onClose} onGone={onGone} />
  const keep: string[] = []
  if (cj.fail_keep != null) keep.push(t('упавших Job хранится: {n} (failedJobsHistoryLimit)', { n: cj.fail_keep }))
  if (cj.ttl != null) keep.push(t('Job удаляется через {n} с после конца (ttlSecondsAfterFinished)', { n: cj.ttl }))
  return createPortal(
    <div className="modal-backdrop">
      <div className="card modal logs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3 className="mono">
            {cj.ns}/{cj.name} <span className="muted small">· <CountryFlag code={server.country} /> {server.name}</span>
          </h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        {note}
        {!job ? null : !canLogs ? (
          <div className="logs-empty muted">{t(LOGS_LOCKED)}</div>
        ) : last?.gone || !pod ? (
          <div className="logs-empty muted">
            {last?.gone || !last ? gone : t('Пода этого Job в отчете нет, лог не найти')}
            {keep.length > 0 && <div className="small">{keep.join(' · ')}</div>}
          </div>
        ) : null}
      </div>
    </div>,
    document.body,
  )
}

const MUTE_FOR = [
  { h: 1, l: '1 час' },
  { h: 24, l: '1 день' },
  { h: 24 * 7, l: '1 неделя' },
  // -1 - насовсем: не снуз (он всегда со сроком), а alert_mutes
  { h: -1, l: 'постоянно' },
]

// Заглушить алерты сервера: kinds на hours часов, -1 - постоянно, 0 - снять
async function muteKinds(s: Server, kinds: string[], hours: number) {
  if (hours < 0) {
    await muteServerKeys(s.id, kinds)
    return
  }
  if (hours === 0 && kinds.some((k) => (s.alert_mutes ?? []).includes(k)))
    await muteServerKeys(s.id, [], kinds)
  for (const k of kinds) if (hours > 0 || s.alert_snoozes?.[k]) await snoozeServerAlert(s.id, k, hours)
}

// Алерты подов, Job и CronJob кластера на время: идет разработка, поды пересоздаются, Job
// падают. Выбор срока - по кнопке в шапке, заглушенное - чипами под шапкой, видно из любого
// раздела. Все четыре с одним сроком - одним чипом
function KubeMuteBar({
  server: s,
  open,
  canAct,
  onClose,
  onChanged,
}: {
  server: Server
  open: boolean
  canAct: boolean
  onClose: () => void
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const now = Date.now()
  const perm = new Set(s.alert_mutes ?? [])
  const active = KUBE_WORK_KINDS.flatMap((k) => {
    if (perm.has(k)) return [{ k, until: null as string | null }]
    const u = s.alert_snoozes?.[k]
    return u && new Date(u).getTime() > now ? [{ k, until: u }] : []
  })
  if (!(open && canAct) && active.length === 0) return null
  const run = async (kinds: string[], hours: number) => {
    setBusy(true)
    setErr(null)
    try {
      await muteKinds(s, kinds, hours)
      onChanged()
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }
  const fmt = (u: string) =>
    new Date(u).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const when = (u: string | null) => (u ? t('до {t}', { t: fmt(u) }) : t('постоянно'))
  const short = (k: string) => kindLabel(k, t).replace(/^Kubernetes: /, '')
  const same =
    active.length === KUBE_WORK_KINDS.length &&
    active.every((x) => (x.until ?? '').slice(0, 16) === (active[0].until ?? '').slice(0, 16))
  const chips = same ? [{ ks: KUBE_WORK_KINDS, label: t('поды, Job и CronJob'), until: active[0].until }]
    : active.map((x) => ({ ks: [x.k], label: short(x.k), until: x.until }))
  return (
    <div className="kube-mute">
      {open && canAct && (
        <div className="web-err-mute small">
          <span className="muted">🔕 {t('не алертить о подах, Job и CronJob этого кластера:')}</span>
          {MUTE_FOR.map(({ h, l }) => (
            <button key={h} className="ghost small" disabled={busy} onClick={() => run(KUBE_WORK_KINDS, h)}>
              {t(l)}
            </button>
          ))}
          <button className="ghost small" disabled={busy} onClick={onClose}>
            {t('Отмена')}
          </button>
        </div>
      )}
      {chips.length > 0 && (
        <div className="snooze-active-list">
          {chips.map((c) => (
            <span key={c.ks.join()} className="snooze-active-chip">
              🔕 {t('не алертят: {what}', { what: c.label })} · {when(c.until)}
              {canAct && (
                <button className="chip-x" disabled={busy} title={t('Снять')} onClick={() => run(c.ks, 0)}>
                  ✕
                </button>
              )}
            </span>
          ))}
        </div>
      )}
      {err && <p className="form-error">{err}</p>}
    </div>
  )
}

type LogRange = 'tail' | 'hour' | 'day'

// Логи пода (on-demand tail через агент). Аналог docker-логов.
function PodLogsModal({
  server,
  pod,
  prev = false,
  note,
  goneText,
  onClose,
  onGone,
}: {
  server: Server
  pod: KubePod
  prev?: boolean // лог прошлого запуска (kubectl logs --previous): почему упал
  note?: ReactNode // над логом: почему упал CronJob (CronRunModal)
  goneText?: string // пода уже нет: свой текст вместо "пересоздан" (Job удален по истории)
  onClose: () => void
  onGone?: () => void // пода уже нет (пересоздан): обновить список
}) {
  const { t } = useI18n()
  // через ref: onChanged родителя меняется на каждом его обновлении и перезапускал бы логи
  const gone = useRef(onGone)
  useEffect(() => {
    gone.current = onGone
  }, [onGone])
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
        const res = await runAndWait(server.id, pod.ns, 'pod', pod.name, prev ? 'logs_prev' : 'logs', opts)
        if (res.status === 'done') setLogs(res.result ?? '')
        else if (logsTargetGone(res)) {
          // нажали в списке, загруженном до пересоздания пода: это не сбой, список устарел
          setErr(goneText ?? t('Под уже пересоздан или удален, список обновлен'))
          gone.current?.()
        } else setErr(cmdState(res) === 'wait' ? cmdWaitText() : logsErrorText(res.result) || t('не удалось получить логи'))
      } catch (e) {
        setErr(e instanceof Error ? e.message : t('ошибка'))
      } finally {
        setLoading(false)
      }
    },
    [server.id, pod.ns, pod.name, prev, goneText, t],
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
    a.download = `${pod.name}${prev ? '_previous' : ''}_${stamp}.txt`
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
            {pod.name} <span className="muted small">· {pod.ns}{prev ? ` · ${t('прошлый запуск')}` : ''} · <CountryFlag code={server.country} /> {server.name}</span>
          </h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        {note}
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
      if (cmdState(res) !== 'ok') setErr(cmdState(res) === 'wait' ? cmdWaitText() : res.result || t('не удалось'))
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('ошибка'))
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
  memlim,
}: {
  serverId: number
  p: KubePod
  day?: number // перезапусков за сутки (restarts_day)
  memlim?: MemLimitItem // уперся в лимит памяти и тормозит (mem_limits.bad ноды, где он работает)
  canAct: boolean
  onLogs?: () => void // нет - логи закрыты учетке (только для просмотра без галки): кнопка приглушена
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
      if (cmdState(res) !== 'ok') setErr(cmdState(res) === 'wait' ? cmdWaitText() : res.result || t('не удалось'))
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('ошибка'))
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
          {memLimChip(memlim, t) && (
            <span className="type-chip t-degraded" title={memLimChip(memlim, t)!.title}>
              🧠 {memLimChip(memlim, t)!.label}
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
        {onLogs ? (
          <button className="ghost icon-btn" onClick={onLogs} title={t('Логи')}>
            📄
          </button>
        ) : (
          <LockedLogs className="ghost icon-btn">📄</LockedLogs>
        )}
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
  { id: 'restarts', label: 'Перезапуски', group: 'Нагрузка' },
  { id: 'finished', label: 'Завершенные', group: 'Нагрузка' },
  { id: 'workloads', label: 'Ворклоады', group: 'Нагрузка' },
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
  memlim,
}: {
  server: Server
  kube: KubeInfo
  canAct: boolean
  memlim?: Record<string, MemLimitItem> // поды у лимита памяти по всем нодам: ns/под -> что с ним
  initialTab?: string | null // из ссылки алерта: ?kube=id&ktab=expiry, или ?kube=id&sec=expiry
  onClose: () => void
  onChanged: () => void
  onAddNode: (n: KubeNodeRef) => void // завести ноду кластера в панели (форма добавления сервера)
}) {
  const { t } = useI18n()
  // логи - тоже команда агенту (POST): учетке только для просмотра - если админ разрешил галкой
  const { canLogs, isViewer } = useAuth()
  const [logsPod, setLogsPod] = useState<KubePod | null>(null)
  // лог прошлого запуска пода из раздела "Перезапуски"
  const [prevPod, setPrevPod] = useState<KubePod | null>(null)
  // CronJob из ссылки алерта подсвечен в списке, с &log=1 сразу открыт лог его последнего запуска
  const [cronLink] = useState<string | null>(() => {
    const k = linkCj
    linkCj = null
    return k
  })
  const [cronOpen, setCronOpen] = useState<string | null>(null)
  // открываем после первого кадра: окно, смонтированное вместе с карточкой, легло бы под нее
  useEffect(() => {
    if (cronLink && linkCjLog) setCronOpen(cronLink)
  }, [cronLink])
  const [muteOpen, setMuteOpen] = useState(false)
  const [jobMuting, setJobMuting] = useState(false)
  const [jobBusy, setJobBusy] = useState(false)
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
  const jobSnooze = s.alert_snoozes?.kube_job
  const jobMuted = (s.alert_mutes ?? []).includes('kube_job') || (!!jobSnooze && new Date(jobSnooze).getTime() > Date.now())
  const muteJobs = async (hours: number) => {
    setJobBusy(true)
    try {
      await muteKinds(s, ['kube_job'], hours)
      setJobMuting(false)
      onChanged()
    } catch (e) {
      toastError(e)
    } finally {
      setJobBusy(false)
    }
  }
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
  // Отмеченные "не нужен, не следить" - отдельно, свернутой строкой внизу раздела
  // Секреты, на которые ничего не ссылается (helper 0.4) - тоже отдельно: не проблема, можно удалить
  const expIgn = s.kube_expiry_ignored ?? {}
  const expUnused = new Set(s.kube_expiry_unused ?? [])
  const expiryAll: KubeExpiry[] = (s.last_report?.kube_expiry ?? []).slice().sort((a, b) => a.expires - b.expires)
  const expiry = expiryAll.filter((e) => !expIgn[expKey(e)] && !expUnused.has(expKey(e)))
  const expiryOff = expiryAll.filter((e) => expIgn[expKey(e)])
  const expiryUnused = expiryAll.filter((e) => !expIgn[expKey(e)] && expUnused.has(expKey(e)))
  const [expOffOpen, setExpOffOpen] = useState(false)
  const [expUnusedOpen, setExpUnusedOpen] = useState(false)
  const [expBusy, setExpBusy] = useState<string | null>(null)
  const [expErr, setExpErr] = useState<{ key: string; msg: string } | null>(null)
  const [expCopied, setExpCopied] = useState('')
  // отметку ставит любой, кроме наблюдателя: доступ панели к kube-api для нее не нужен
  const canMark = !isViewer
  // лог упавшего Job - то же чтение логов пода: нужен доступ панели к kube-api
  const jobLogs = canLogs && kube.access
  const setExpIgnored = (e: KubeExpiry, ignored: boolean) => {
    const key = expKey(e)
    setExpBusy(key)
    setExpErr(null)
    kubeExpiryIgnore(s.id, e.kind, e.where, ignored)
      .then(() => onChanged())
      .catch((x) => setExpErr({ key, msg: x instanceof Error ? x.message : t('Ошибка') }))
      .finally(() => setExpBusy(null))
  }
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
    // поды, которые перезапускались за сутки: сюда ведет алерт о частых перезапусках
    restarts: kube.access && (s.kube_restarts?.pods.length ?? 0) > 0,
    finished: kube.access && doneCount > 0,
    workloads: kube.access,
    cron: kube.access && (cronjobs.length > 0 || failedJobs.length > 0),
    nodes: kube.access,
    expiry: expiryAll.length > 0,
    flux: flux.length > 0,
  }
  const secs = KUBE_SECTIONS.filter((x) => shown[x.id])
  // раздел пропал (последний завершенный под подчистили) - на сводку, а не на пустое место
  const cur = secs.find((x) => x.id === active) ?? secs[0]
  useUrlParam('sec', cur.id === 'overview' ? null : cur.id)
  // открытый лог CronJob - в адресе, ссылкой можно поделиться
  const cronShown = cur.id === 'cron' ? cronjobs.find((c) => `${c.ns}/${c.name}` === cronOpen) ?? null : null
  useUrlParam('cj', cronShown ? cronOpen : null)
  useUrlParam('log', cronShown ? '1' : null)
  useEffect(() => {
    if (cronLink && cur.id === 'cron') document.getElementById(`cj-${cronLink}`)?.scrollIntoView({ block: 'center' })
  }, [cronLink, cur.id])

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
    restarts: t('перезапускались за сутки: {n}', { n: s.kube_restarts?.pods.length ?? 0 }),
    finished: t('завершенных подов: {n}, упали {m}', { n: doneCount, m: doneFailed }),
    workloads: workloads.length
      ? t('в полном составе {a} из {b}', { a: workloads.length - wlPartial.length, b: workloads.length })
      : t('Ворклоадов нет'),
    cron: cronBad
      ? t('заданий: {n}, с проблемой {m}', { n: cronjobs.length, m: cronBad })
      : t('заданий: {n}, все отрабатывают', { n: cronjobs.length }),
    nodes: t('готовы {a} из {b}', { a: nodesReady, b: nodes.length }),
    expiry: next
      ? t('ближайший: {what}, {d}', { what: t(EXPIRY_KIND[next.kind] ?? next.kind), d: daysText(nextDays) })
      : expiryUnused.length ? t('не используется, можно удалить: {n}', { n: expiryUnused.length })
      : expiryOff.length ? t('не следим: {n}', { n: expiryOff.length }) : '',
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
  markMuted(info, srvMuted(s, t).filter((i) => i.section === 'kuber')
    .map((i) => ({ sec: KUBE_SEC_IDS.has(i.sec ?? '') ? i.sec! : 'overview', text: i.text })), t)
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
      {!kube.access && <EnableBlock server={s} />}
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
          <button
            className="srv-tile-btn"
            onClick={() => (shown.restarts ? go('restarts') : (go('pods'), setSort('restarts')))}
            title={t('к разделу')}
          >
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
            {canAct && (
              <button className="ghost icon-btn" onClick={() => setMuteOpen((v) => !v)} title={t('Заглушить алерты подов и Job')}>
                🔕
              </button>
            )}
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
              <span className="srv-meta-item"><span className="srv-meta-k">{t('namespace')}</span> {kube.namespaces ?? 0}</span>
              <span className="srv-meta-item"><span className="srv-meta-k">{t('нод')}</span> {nodes.length}</span>
              <span className="srv-meta-item"><span className="srv-meta-k">{t('подов')}</span> {podsActive}</span>
            </>
          )}
          <span className="srv-meta-item">
            <span className="srv-meta-k">{t('обновлено')}</span> {fmtRel(s.last_seen)}
          </span>
        </div>
        <KubeMuteBar server={s} open={muteOpen} canAct={canAct} onClose={() => setMuteOpen(false)} onChanged={onChanged} />
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
                            onLogs={canLogs ? () => setLogsPod(p) : undefined}
                            onChanged={onChanged}
                            day={s.restarts_day?.[`pod:${p.ns}/${p.name}`] ?? 0}
                            memlim={memlim?.[`${p.ns}/${p.name}`]}
                          />
                        ))}
                </div>
              </div>
            )}

            {cur.id === 'restarts' && s.kube_restarts && (
              <KubeRestartsSec
                server={s}
                data={s.kube_restarts}
                flavor={kube.flavor}
                canAct={canAct}
                onPrevLogs={canLogs ? (p) => setPrevPod({ ns: p.ns, name: p.name, phase: '', ready: false, restarts: p.n }) : undefined}
                onChanged={onChanged}
              />
            )}

            {cur.id === 'workloads' && (
              <div className="loc-results docker-clist">
                {workloads.length === 0
                  ? empty(t('Ворклоадов нет'))
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
                {/* заглушенное видно чипом под шапкой, здесь только кнопка */}
                {failedJobs.length > 0 && canAct && !jobMuted && (
                  <div className="web-err-mute small">
                    {jobMuting ? (
                      <>
                        <span className="muted">🔕 {t('не алертить об упавших Job:')}</span>
                        {MUTE_FOR.map(({ h, l }) => (
                          <button key={h} className="ghost small" disabled={jobBusy} onClick={() => muteJobs(h)}>
                            {t(l)}
                          </button>
                        ))}
                        <button className="ghost small" disabled={jobBusy} onClick={() => setJobMuting(false)}>
                          {t('Отмена')}
                        </button>
                      </>
                    ) : (
                      <button className="ghost small" onClick={() => setJobMuting(true)}>
                        🔕 {t('не алертить об упавших Job')}
                      </button>
                    )}
                  </div>
                )}
                <div className="loc-results docker-clist">
                  {failedJobs.map((j, i) => {
                    const pod = jobPod(pods, j.ns, j.name)
                    return (
                      <div className="loc-res docker-row job-row t-down" key={`job/${j.ns}/${j.name}/${j.ts ?? 0}`}>
                        <div className="job-main">
                          <span className="lk-dot lk-dot-bad" />
                          <div className="docker-c-main">
                            <div className="docker-c-name mono">
                              {j.ns}/{j.name}
                              <span className="type-chip">Job</span>
                            </div>
                            <div className="docker-c-img mono muted small">
                              {j.ts ? t('упал {a} назад', { a: sinceShort(new Date(j.ts * 1000).toISOString(), t) || t('только что') }) : ''}
                              {!pod && ` · ${t('пода уже нет, лога тоже')}`}
                            </div>
                          </div>
                          <div className="docker-c-status mono small t-down">{j.reason || 'Failed'}</div>
                        </div>
                        {/* лог сразу у трех свежих: дальше - старое, его открывают руками */}
                        {pod && jobLogs && s.online && i < 3 && <JobLog server={s} pod={pod} onOpen={() => setLogsPod(pod)} />}
                        {pod && jobLogs && (!s.online || i >= 3) && (
                          <div className="job-log">
                            <button className="ghost small" onClick={() => setLogsPod(pod)}>
                              {t('Лог пода {pod}', { pod: pod.name })}
                            </button>
                          </div>
                        )}
                        {pod && kube.access && !canLogs && (
                          <div className="job-log">
                            <LockedLogs className="ghost small">{t('Лог пода {pod}', { pod: pod.name })}</LockedLogs>
                          </div>
                        )}
                      </div>
                    )
                  })}
                  {cronjobs
                    .filter((c) => !ql || `${c.ns}/${c.name} ${c.image ?? ''}`.toLowerCase().includes(ql))
                    .map((c) => {
                      const key = `${c.ns}/${c.name}`
                      const issue = cronIssues[key]
                      const ago = (ts?: number) => (ts ? sinceShort(new Date(ts * 1000).toISOString(), t) || t('только что') : '')
                      const tone = issue ? (c.suspend ? 't-degraded' : 't-down') : c.suspend ? '' : 't-up'
                      const ran = !!cronLastJob(c)
                      // у проблемного - кнопкой словами: за ней идут из алерта; у остальных - значком
                      const open = () => setCronOpen(key)
                      const logBtn = !ran || !kube.access ? null : issue ? (
                        <button className="ghost small" onClick={open}>
                          {t('Лог последнего запуска')}
                        </button>
                      ) : (
                        <button className="ghost icon-btn" onClick={open} title={t('Лог последнего запуска')}>
                          📄
                        </button>
                      )
                      return (
                        <div
                          className={`loc-res docker-row job-row cron-row ${tone}${key === cronLink ? ' cj-link' : ''}`}
                          key={key}
                          id={`cj-${key}`}
                        >
                          <div className="job-main">
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
                            {!issue && logBtn && <div className="docker-actions">{logBtn}</div>}
                          </div>
                          {issue && logBtn && <div className="job-log">{logBtn}</div>}
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
                {(() => {
                  // off - отмечен "не нужен, не следить": серый, без алерта, с "следить снова";
                  // spare - на секрет ничего не ссылается (helper 0.4): серый, без алерта, с командой удаления
                  const row = (e: KubeExpiry, off: boolean, spare = false) => {
                    const key = expKey(e)
                    const d = daysLeft(e.expires)
                    const unused = unusedSecret(e)
                    // истекший секрет, который не указан ни в одном Ingress, скорее забыт: желтым, не красным
                    const tone = off || spare ? 't-muted' : unused && d < 0 ? 't-degraded' : expiryTone(d, horizon)
                    const mark = expIgn[key]
                    const cmd = off || spare || (unused && d < 0) ? deleteSecretCmd(e) : ''
                    return (
                      <div className={`loc-res docker-row ${tone}`} key={e.kind + '/' + e.where + '/' + (e.note ?? '')}>
                        <span className={`lk-dot${off || spare ? '' : DOT[tone] ?? ' lk-dot-ok'}`} />
                        <div className="docker-c-main">
                          <div className="docker-c-name mono">
                            {e.where}
                            <span className="type-chip">{t(EXPIRY_KIND[e.kind] ?? e.kind)}</span>
                            {mark && (
                              <span className="type-chip muted"
                                title={t('отметил {who}, {when}', { who: mark.by || '?', when: mark.at ? new Date(mark.at).toLocaleString() : '?' })}>
                                {t('не следим')}
                              </span>
                            )}
                          </div>
                          <div className="docker-c-img mono muted small">
                            {new Date(e.expires * 1000).toLocaleDateString()}
                            {e.note ? ` · ${e.note}` : ''}
                          </div>
                          {spare ? (
                            <div className="muted small kube-exp-unused">{t('не используется, можно удалить')}</div>
                          ) : (
                            unused && <div className="muted small kube-exp-unused">{t('не используется ни одним ingress')}</div>
                          )}
                          {expErr && expErr.key === key && <div className="form-error small">{expErr.msg}</div>}
                          {cmd && (
                            <div className="kube-exp-cmd small">
                              <span className="muted">{t('удалить из кластера вручную:')}</span>
                              <code className="mono">{cmd}</code>
                              <button className="linklike" onClick={() => {
                                navigator.clipboard?.writeText(cmd)
                                setExpCopied(key)
                                window.setTimeout(() => setExpCopied(''), 1500)
                              }}>
                                {expCopied === key ? t('скопировано') : t('копировать')}
                              </button>
                            </div>
                          )}
                        </div>
                        <div className="kube-exp-side">
                          <div className={`docker-c-status mono small ${tone}`}>{daysText(d)}</div>
                          {canMark && !spare && (
                            <button className="linklike small" disabled={expBusy === key} onClick={() => setExpIgnored(e, !off)}
                              title={off ? undefined : t('Уберет пункт из проблем и алертов. Обновят секрет - панель снова будет за ним следить')}>
                              {off ? t('следить снова') : t('не нужен, не следить')}
                            </button>
                          )}
                        </div>
                      </div>
                    )
                  }
                  return (
                    <>
                      {expiry.length > 0 && <div className="loc-results docker-clist">{expiry.map((e) => row(e, false))}</div>}
                      {expiryUnused.length > 0 && (
                        <>
                          <button className="linklike kube-exp-off muted small" onClick={() => setExpUnusedOpen(!expUnusedOpen)}>
                            {t('не используется, можно удалить: {n}', { n: expiryUnused.length })}, {expUnusedOpen ? t('скрыть') : t('показать')}
                          </button>
                          {expUnusedOpen && <div className="loc-results docker-clist">{expiryUnused.map((e) => row(e, false, true))}</div>}
                        </>
                      )}
                      {expiryOff.length > 0 && (
                        <>
                          <button className="linklike kube-exp-off muted small" onClick={() => setExpOffOpen(!expOffOpen)}>
                            {t('не следим: {n}', { n: expiryOff.length })}, {expOffOpen ? t('скрыть') : t('показать')}
                          </button>
                          {expOffOpen && <div className="loc-results docker-clist">{expiryOff.map((e) => row(e, true))}</div>}
                        </>
                      )}
                    </>
                  )
                })()}
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
      {logsPod && <PodLogsModal server={s} pod={logsPod} onClose={() => setLogsPod(null)} onGone={onChanged} />}
      {prevPod && <PodLogsModal server={s} pod={prevPod} prev onClose={() => setPrevPod(null)} onGone={onChanged} />}
      {cronShown && (
        <CronRunModal
          server={s}
          cj={cronShown}
          pods={pods}
          issue={cronIssues[`${cronShown.ns}/${cronShown.name}`]}
          onClose={() => setCronOpen(null)}
          onGone={onChanged}
        />
      )}
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
  hint,
  onOpen,
}: {
  server: Server
  kube: KubeInfo
  showGroup?: boolean
  hint?: string | null // что из нажатой плитки нашлось в кластере
  onOpen: () => void
}) {
  const { t } = useI18n()
  const nodes = kube.nodes ?? []
  const pods = kube.pods ?? []
  const nodesReady = nodes.filter((n) => n.ready).length
  const { running, active } = podCounts(pods)
  // в чипах имена, а не голые числа: что именно падает и что истекает
  const badPods = pods.filter((p) => !podFinished(p) && podTone(p) === 't-down')
  const bad = badPods.length
  // Сроки видны прямо в списке: истёкший токен Flux не меняет ни одного счётчика
  // подов, и без отдельной пометки хост в списке выглядит совершенно здоровым.
  const fluxBad = (s.last_report?.flux ?? []).filter(
    (f) => !f.ready && !FLUX_TRANSIENT.has(f.reason ?? ''),
  )
  const fluxBroken = fluxBad.length
  const soonList = liveExpiry(s).filter((e) => daysLeft(e.expires) <= expiryHorizon(s))
  const soon = soonList.length
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
            <span className="type-chip off chip-names" title={t('поды падают: CrashLoopBackOff, ошибка образа или запуска')}>
              🔥 {shortList(badPods.map((p) => p.name), t, 2)}
            </span>
          )}
          {kube.access && nodesReady < nodes.length && (
            <span className="type-chip off" title={t('ноды не в Ready')}>
              {t('ноды {r}/{n}', { r: nodesReady, n: nodes.length })}
            </span>
          )}
          {fluxBroken > 0 && (
            <span className="type-chip t-down chip-names" title={t('Ресурсы Flux не в Ready — доставка встала')}>
              Flux ⚠ {shortList(fluxBad.map((f) => f.where.split('/').pop() || f.where), t, 2)}
            </span>
          )}
          {soon > 0 && (
            <span className="type-chip t-degraded chip-names" title={t('Истекает за {n} дн. и раньше: {list}', { n: expiryHorizon(s), list: soonList.map((e) => e.where).join(', ') })}>
              ⏳ {shortList(soonList.map((e) => t(EXPIRY_KIND[e.kind] ?? e.kind)), t, 2)}
            </span>
          )}
        </div>
        <div className="mono muted small">
          {kube.flavor || 'k8s'} {kube.version || '—'}
          {kube.access ? ` · ${t('ноды {r}/{n}', { r: nodesReady, n: nodes.length })}` : ''}
        </div>
        {hint && <div className="hs-host-hit">{hint}</div>}
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
type KFilter = 'nodes' | 'pods' | 'attn'

// Что из нажатой плитки нашлось в кластере - строка под его именем; null - кластер под плитку
// не подходит. Условия те же, что у чисел в плитках: кластеры на связи, ноды и поды - только
// при доступе к API, поэтому имена по строкам сходятся с числами плитки
function kubeHit(f: KFilter, { s, d }: KHost, t: (k: string, p?: Record<string, string | number>) => string): string | null {
  if (!s.online) return null
  const list = (xs: string[]) => ({ list: shortList(xs, t, 8) })
  if (f === 'nodes') {
    const nodes = d.access ? d.nodes ?? [] : []
    const down = nodes.filter((n) => !n.ready).map((n) => n.name)
    const press = nodes
      .filter((n) => n.ready && (n.pressure ?? []).length > 0)
      .map((n) => `${n.name} (${(n.pressure ?? []).join(', ')})`)
    const unmon = (s.kube_unmonitored ?? []).map((n) => n.name)
    return subOf(
      down.length > 0 && t('не готовы: {list}', list(down)),
      press.length > 0 && t('под давлением: {list}', list(press)),
      unmon.length > 0 && t('без агента: {list}', list(unmon)),
    ) || null
  }
  if (f === 'pods') {
    const active = (d.access ? d.pods ?? [] : []).filter((p) => !podFinished(p))
    const bad = active.filter((p) => podTone(p) === 't-down').map((p) => p.name)
    const slow = active
      .filter((p) => !(p.phase === 'Running' && p.ready) && podTone(p) !== 't-down')
      .map((p) => p.name)
    return subOf(
      bad.length > 0 && t('падают: {list}', list(bad)),
      slow.length > 0 && t('не готовы: {list}', list(slow)),
    ) || null
  }
  const exp = liveExpiry(s)
  const what = (e: KubeExpiry) => `${t(EXPIRY_KIND[e.kind] ?? e.kind)} ${e.where}`
  const flux = (s.last_report?.flux ?? []).filter((x) => !x.ready && !FLUX_TRANSIENT.has(x.reason ?? '')).map((x) => x.where)
  const expired = exp.filter((e) => daysLeft(e.expires) < 0).map(what)
  const soon = exp.filter((e) => daysLeft(e.expires) >= 0 && daysLeft(e.expires) <= expiryHorizon(s)).map(what)
  const jobs = [...new Set([...Object.keys(s.kube_cron_issues ?? {}), ...(d.failed_jobs ?? []).map((j) => `${j.ns}/${j.name}`)])]
  return subOf(
    flux.length > 0 && t('Flux встал: {list}', list(flux)),
    expired.length > 0 && t('сроки истекли: {list}', list(expired)),
    soon.length > 0 && t('сроки скоро: {list}', list(soon)),
    jobs.length > 0 && t('задания упали: {list}', list(jobs)),
  ) || null
}

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
  const { tile, setTile, tileProps } = useTileFilter<KFilter>()
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

  // поды, упершиеся в лимит памяти, по всем нодам: агент ноды знает свои поды, а список подов
  // кластера - у той ноды, где kube-api
  const memlimPods: Record<string, MemLimitItem> = {}
  for (const x of (servers ?? []).flatMap((sv) => sv.mem_limits?.bad ?? [])) if (x.kind === 'pod') memlimPods[x.v.name] = x
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
      // адреса самой ноды и узлов кластера
      return hay.includes(q) || ipHit([...srvIps(s), ...(d.nodes ?? []).map((n) => n.ip)], q)
    })
    .map((h) => ({ ...h, hint: tile ? kubeHit(tile, h, t) : null }))
    .filter((h) => !tile || h.hint)
    .sort((a, b) => a.s.name.localeCompare(b.s.name))

  const groups: { key: string; label: string; items: typeof hosts }[] =
    groupBy === 'none'
      ? [{ key: 'all', label: '', items: hosts }]
      : (() => {
          const map = new Map<string, typeof hosts>()
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
  // Сводка: ноды, поды и то, что встало или скоро истечет. Считаем по кластерам на связи: у
  // молчащих данные старые, и "все работают" по ним было бы неправдой. Молчащие названы в первой
  // плитке по именам. Ноды и поды - по кластерам с доступом к API, замечания - по всем на связи
  // (сроки и Flux приносят helper'ы); когда считать не по чему, плитки нет
  const live = allHosts.filter(({ s }) => s.online)
  const names = (xs: KHost[]) => shortList(xs.map(({ s }) => s.name), t, 2)
  const offHosts = allHosts.filter(({ s }) => !s.online)
  const noAccHosts = live.filter(({ d }) => !d.access)
  const nAll = live.flatMap(({ d }) => (d.access ? d.nodes ?? [] : []))
  const nReady = nAll.filter((n) => n.ready).length
  const nPress = nAll.filter((n) => n.ready && (n.pressure ?? []).length > 0).length
  const nUnmon = live.reduce((n, { s }) => n + (s.kube_unmonitored ?? []).length, 0)
  const pAll = live.flatMap(({ d }) => (d.access ? d.pods ?? [] : []))
  const { running: pRun, active: pAct } = podCounts(pAll)
  const pBad = pAll.filter((p) => !podFinished(p) && podTone(p) === 't-down').length
  const fluxHosts = live.filter(({ s }) => (s.last_report?.flux ?? []).some((f) => !f.ready && !FLUX_TRANSIENT.has(f.reason ?? '')))
  const expiredHosts = live.filter(({ s }) => liveExpiry(s).some((e) => daysLeft(e.expires) < 0))
  const soonHosts = live.filter(({ s }) =>
    liveExpiry(s).some((e) => daysLeft(e.expires) >= 0 && daysLeft(e.expires) <= expiryHorizon(s)))
  const cronHosts = live.filter(({ s }) =>
    Object.keys(s.kube_cron_issues ?? {}).length > 0 || (s.last_report?.kube?.failed_jobs ?? []).length > 0)
  const attn = live.filter(({ s }) =>
    (s.last_report?.flux ?? []).some((f) => !f.ready && !FLUX_TRANSIENT.has(f.reason ?? '')) ||
    liveExpiry(s).some((e) => daysLeft(e.expires) <= expiryHorizon(s)) ||
    Object.keys(s.kube_cron_issues ?? {}).length > 0 ||
    (s.last_report?.kube?.failed_jobs ?? []).length > 0).length
  // нажатая плитка: подпись чипа над списком и что сказать, если под нее ничего не попало
  const filterLabel: Record<KFilter, string> = {
    nodes: t('ноды не готовы или с замечаниями'), pods: t('поды не работают'), attn: t('кластеры с замечаниями'),
  }
  const filterEmpty: Record<KFilter, string> = {
    nodes: t('Все ноды готовы.'), pods: t('Все поды работают.'), attn: t('Замечаний нет.'),
  }

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
              noAccHosts.length > 0 && t('без доступа: {list}', { list: names(noAccHosts) }),
              offHosts.length > 0 && t('не на связи: {list}', { list: names(offHosts) }),
            ) || t('все на связи')}
            tone={noAccHosts.length || offHosts.length ? 'warn' : 'ok'}
            {...tileProps(null)}
          />
          {live.some(({ d }) => d.access) && <>
          <NetTile
            label={t('Ноды готовы')}
            value={String(nReady)}
            unit={t('из {n}', { n: nAll.length })}
            sub={subOf(
              nPress > 0 && t('под давлением: {n}', { n: nPress }),
              nUnmon > 0 && t('без агента: {n}', { n: nUnmon }),
            ) || t('все готовы')}
            tone={nReady < nAll.length ? 'bad' : nPress || nUnmon ? 'warn' : 'ok'}
            {...tileProps('nodes')}
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
            {...tileProps('pods')}
          />
          </>}
          {live.length > 0 && <>
          <NetTile
            label={t('Кластеры с замечаниями')}
            value={String(attn)}
            sub={subOf(
              fluxHosts.length > 0 && t('Flux встал: {list}', { list: names(fluxHosts) }),
              expiredHosts.length > 0 && t('сроки истекли: {list}', { list: names(expiredHosts) }),
              soonHosts.length > 0 && t('сроки скоро: {list}', { list: names(soonHosts) }),
              cronHosts.length > 0 && t('задания упали: {list}', { list: names(cronHosts) }),
            ) || t('все в порядке')}
            tone={fluxHosts.length || expiredHosts.length ? 'bad' : attn ? 'warn' : 'ok'}
            {...tileProps('attn')}
          />
          </>}
        </div>
      )}
      {allHosts.length > 1 && (
        <div className="checks-search-row">
          <input
            className="checks-search"
            placeholder={t('Поиск: хост, IP, группа, нода, под, ворклоад…')}
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
      {tile && <TileFilterChip label={filterLabel[tile]} onClear={() => setTile(null)} />}
      {err && <p className="form-error">{err}</p>}
      {servers && allHosts.length === 0 && (
        <div className="card muted">
          {t('Kubernetes не найден ни на одном сервере. Агент определяет кластер сам (k0s/k3s/microk8s/kubeadm); если он есть, но раздел пуст — обновите агент.')}
        </div>
      )}
      {servers && allHosts.length > 0 && hosts.length === 0 && (
        <div className="card muted">{tile && !q ? filterEmpty[tile] : t('Ничего не найдено.')}</div>
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
                {g.items.map(({ s, d, hint }) => (
                  <HostRow
                    key={s.id}
                    server={s}
                    kube={d}
                    showGroup={groupBy !== 'group'}
                    hint={hint}
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
          memlim={memlimPods}
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
