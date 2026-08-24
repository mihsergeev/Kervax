import { tr } from './i18n'

// Должна совпадать с MIN_PASSWORD_LEN в backend/app/security.py: интерфейс не
// даёт ввести короче, бэкенд не принимает. Расхождение ловит ops/selfcheck.py —
// иначе форма молча отправляла бы то, что API отвергнет.
export const MIN_PASSWORD_LEN = 12

const TOKEN_KEY = 'kervax_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null) {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token)
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
}

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string> | undefined),
  }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  const res = await fetch(path, { ...options, headers })
  if (res.status === 204) return undefined as T

  const body = await res.json().catch(() => null)
  if (!res.ok) {
    const detail =
      typeof body?.detail === 'string' ? body.detail : tr('Ошибка HTTP {code}', { code: res.status })
    throw new ApiError(detail, res.status)
  }
  return body as T
}

// status: ok | degraded, db: ok | down — при недоступной базе бэкенд отвечает 503
export type Health = { status: string; version: string; db?: string }

// Версию в «О панели» спрашиваем у БЭКЕНДА, а не пишем в вёрстке. Иначе номер
// живёт в четырёх местах сразу и однажды разъезжается — а при частичном деплое
// (обновили фронт, забыли бэкенд) видно как раз то, что интересно.
export const getHealth = () => api<Health>('/api/health')

export type Role = 'admin' | 'editor' | 'viewer'
export type UserOut = {
  id: number
  username: string
  role: Role
  sections?: string[] | null // разрешённые вкладки; пусто/нет = все
  // видимые группы; у серверов и у сайтов свои наборы имён, поэтому списки раздельные
  server_groups?: string[] | null
  site_groups?: string[] | null
}

export function me(): Promise<UserOut> {
  return api<UserOut>('/api/auth/me')
}

// --- управление учётками (только админ) ---
export type PanelUser = {
  id: number
  username: string
  role: Role
  sections?: string[] | null
  server_groups?: string[] | null
  site_groups?: string[] | null
  totp_enabled: boolean
  created_at: string
}

// Правка роли и границ доступа существующей учётки (пароль — отдельным вызовом).
export function updateUser(
  id: number,
  body: { role?: Role; sections?: string[]; server_groups?: string[]; site_groups?: string[] },
): Promise<PanelUser> {
  return api<PanelUser>(`/api/users/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
}
// Группы, реально заведённые в панели — РАЗДЕЛЬНО по видам: имена у серверов и
// у сайтов свои, и общий список означал бы «доступ к чему-то одноимённому».
export type AccessGroups = { servers: string[]; sites: string[] }
export function listAccessGroups(): Promise<AccessGroups> {
  return api<AccessGroups>('/api/users/groups')
}
export function listUsers(): Promise<PanelUser[]> {
  return api<PanelUser[]>('/api/users')
}

export function createUser(body: {
  username: string
  password: string
  role: Role
  sections?: string[]
  server_groups?: string[]
  site_groups?: string[]
}): Promise<PanelUser> {
  return api<PanelUser>('/api/users', { method: 'POST', body: JSON.stringify(body) })
}

export function resetUserPassword(id: number, new_password: string): Promise<void> {
  return api(`/api/users/${id}/password`, {
    method: 'POST',
    body: JSON.stringify({ new_password }),
  })
}

export function deleteUser(id: number): Promise<void> {
  return api(`/api/users/${id}`, { method: 'DELETE' })
}

// Смена пароля: возвращает новый токен (старые инвалидируются на бэкенде).
export async function changePassword(
  current_password: string,
  new_password: string,
): Promise<void> {
  const r = await api<{ access_token: string }>('/api/auth/password', {
    method: 'POST',
    body: JSON.stringify({ current_password, new_password }),
  })
  setToken(r.access_token)
}

export type TwoFAStatus = { enabled: boolean }
export type TwoFASetup = { secret: string; otpauth_uri: string }

export function get2FA(): Promise<TwoFAStatus> {
  return api<TwoFAStatus>('/api/auth/2fa')
}

export function setup2FA(): Promise<TwoFASetup> {
  return api<TwoFASetup>('/api/auth/2fa/setup', { method: 'POST' })
}

export function enable2FA(otp: string): Promise<TwoFAStatus> {
  return api<TwoFAStatus>('/api/auth/2fa/enable', {
    method: 'POST',
    body: JSON.stringify({ otp }),
  })
}

export function disable2FA(otp: string): Promise<TwoFAStatus> {
  return api<TwoFAStatus>('/api/auth/2fa/disable', {
    method: 'POST',
    body: JSON.stringify({ otp }),
  })
}

// --- мониторы сайтов/сервисов ---

export type CheckType = 'http' | 'tcp_port' | 'cert'
export type CheckStatus = 'up' | 'degraded' | 'down' | 'unknown'

// разбивка проверки по IP (режим «проверять все адреса домена»)
export type IpResult = {
  ip: string
  status: CheckStatus
  latency_ms: number | null
  message: string
}

export type Check = {
  id: number
  name: string
  type: CheckType
  target: string
  port: number
  enabled: boolean
  group_name: string
  sort_order: number
  interval_seconds: number
  timeout_ms: number
  degraded_ms: number
  retries: number
  alert_after_failures: number
  degraded_after_failures: number
  method: string
  expected_status: string
  keyword_up: string
  keyword_down: string
  auth_method: string
  auth_user: string
  auth_pass: string
  http_headers: string
  ignore_tls: boolean
  check_all_ips: boolean
  last_ip_results: IpResult[] | null
  check_ssl: boolean
  check_domain: boolean
  ssl_warn_days: number[]
  domain_warn_days: number[]
  ssl_days: number | null
  domain_days: number | null
  ssl_message: string
  domain_message: string
  expiry_checked_at: string | null
  check_locations: boolean
  location_ids: number[] | null
  last_status: CheckStatus
  last_message: string
  last_latency_ms: number | null
  last_value: number | null
  last_checked_at: string | null
  snooze_until: string | null
  created_at: string
  updated_at: string
  uptime_24h: number | null
  beats: CheckStatus[] | null // последние N снимков — для мини-ленты статуса в списке
  loc_down?: string[] // локации, из которых сайт сейчас не отвечает
  alert_mutes: string[] | null // заглушённые типы алертов этого монитора
}

export type ChecksOverview = {
  total: number
  up: number
  degraded: number
  down: number
  unknown: number
  disabled: number
  partial: number // доступен с основной проверки, но не из части локаций
  loc_summary?: { id: number; name: string; down: number; total: number }[]
  open_incidents: number
  checks: Check[]
}

// поля, отправляемые при создании/редактировании (бэкенд подставит дефолты)
export type CheckForm = {
  name: string
  type: CheckType
  target: string
  group_name?: string
  port?: number
  enabled?: boolean
  interval_seconds?: number
  timeout_ms?: number
  degraded_ms?: number
  retries?: number
  alert_after_failures?: number
  degraded_after_failures?: number
  method?: string
  expected_status?: string
  keyword_up?: string
  keyword_down?: string
  auth_method?: string
  auth_user?: string
  auth_pass?: string
  http_headers?: string
  ignore_tls?: boolean
  check_all_ips?: boolean
  check_ssl?: boolean
  check_domain?: boolean
  ssl_warn_days?: number[]
  domain_warn_days?: number[]
  check_locations?: boolean
  location_ids?: number[] | null
  alert_mutes?: string[]
}

// Поля для массового применения ко всем мониторам (только переданные меняются).
export type CheckBulk = {
  interval_seconds?: number
  degraded_ms?: number
  retries?: number
  alert_after_failures?: number
  degraded_after_failures?: number
  expected_status?: string
  check_ssl?: boolean
  check_domain?: boolean
  check_locations?: boolean
  check_all_ips?: boolean
  ssl_warn_days?: number[]
  domain_warn_days?: number[]
}

// ids не задан → применить ко ВСЕМ мониторам; ids=[…] → только к выбранным.
export function bulkUpdateChecks(
  fields: CheckBulk,
  ids?: number[],
): Promise<{ updated: number }> {
  const body = ids ? { ...fields, ids } : fields
  return api('/api/checks/bulk', { method: 'PATCH', body: JSON.stringify(body) })
}

// Массовое создание мониторов из списка.
export function importChecks(items: CheckForm[]): Promise<{ updated: number }> {
  return api('/api/checks/import', { method: 'POST', body: JSON.stringify({ items }) })
}

// Домены, уже стоящие на мониторинге: «Сервисы» рисуют по ним галочку и не дают
// завести дубль. Значение — id монитора, 0 = мониторится в невидимой учётке группе.
export type KnownHosts = {
  hosts: Record<string, number>
  groups: string[]
  ignored: string[]
}
export function knownHosts(): Promise<KnownHosts> {
  return api('/api/checks/known-hosts')
}

// Завести мониторы по доменам веб-сервиса. Возвращает обновлённую карту хостов,
// чтобы галочки перерисовались без перезагрузки страницы.
export function adoptDomains(
  domains: string[],
  group_name = '',
): Promise<{ created: number; skipped: string[]; hosts: Record<string, number> }> {
  return api('/api/checks/adopt', {
    method: 'POST',
    body: JSON.stringify({ domains, group_name }),
  })
}

// --- брендирование (свой логотип) ---
export type Branding = {
  logo: boolean
  title: string
  plate: 'auto' | 'always' | 'never'
  plate_auto: boolean
  version: number
}
export function getBranding(): Promise<Branding> {
  return api('/api/branding')
}
// ?v= меняется при замене логотипа: файл кэшируется на неделю, и без версии
// браузер показывал бы старый ещё долго после замены
export function brandingLogoUrl(version: number): string {
  return `/api/branding/logo?v=${version}`
}
export function putBranding(body: {
  data: string
  plate: string
  plate_auto: boolean
  title: string
}): Promise<Branding> {
  return api('/api/branding', { method: 'PUT', body: JSON.stringify(body) })
}
export function deleteBranding(): Promise<Branding> {
  return api('/api/branding', { method: 'DELETE' })
}

// --- персональные алерты в Telegram (своя учётка) ---
export type TelegramState = {
  chat_id: string
  own_token: boolean
  alerts: boolean
  bot: string
  ready: boolean
}
export function getTelegram(): Promise<TelegramState> {
  return api('/api/auth/telegram')
}
export function linkTelegram(): Promise<{ code: string; bot: string }> {
  return api('/api/auth/telegram/link', { method: 'POST' })
}
export function confirmTelegram(): Promise<TelegramState> {
  return api('/api/auth/telegram/confirm', { method: 'POST' })
}
export function testTelegram(): Promise<TelegramState> {
  return api('/api/auth/telegram/test', { method: 'POST' })
}
export function deleteTelegram(): Promise<TelegramState> {
  return api('/api/auth/telegram', { method: 'DELETE' })
}
export function updateTelegram(body: {
  alerts?: boolean
  chat_id?: string
  token?: string
}): Promise<TelegramState> {
  return api('/api/auth/telegram', { method: 'PATCH', body: JSON.stringify(body) })
}

// Домены, найденные агентами на веб-серверах парка (для мастера в «Сайтах»).
export type Discovered = {
  domains: { domain: string; servers: string[] }[]
  hosts: Record<string, number>
  groups: string[]
  ignored: string[]
}
export function discoveredDomains(): Promise<Discovered> {
  return api('/api/checks/discovered')
}

// Пометить домены «мониторить не нужно» (или вернуть их в предложения).
export function ignoreDomains(domains: string[], ignore = true): Promise<{ updated: number }> {
  return api('/api/checks/discovered/ignore', {
    method: 'POST',
    body: JSON.stringify({ domains, ignore }),
  })
}

// Массовое удаление мониторов (с историей и инцидентами).
export function bulkDeleteChecks(ids: number[]): Promise<{ updated: number }> {
  return api('/api/checks/bulk-delete', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  })
}

// Ручной порядок мониторов + перенос между группами (group_name).
// order — полный список в новом порядке отображения.
export function reorderChecks(
  order: { id: number; group_name?: string }[],
): Promise<{ updated: number }> {
  return api('/api/checks/reorder', { method: 'POST', body: JSON.stringify({ order }) })
}

// Полный монитор → редактируемая форма (для «Изменить» из списка и из детали).
export function checkToForm(c: Check): CheckForm {
  return {
    name: c.name,
    type: c.type,
    target: c.target,
    group_name: c.group_name,
    port: c.port,
    enabled: c.enabled,
    interval_seconds: c.interval_seconds,
    timeout_ms: c.timeout_ms,
    degraded_ms: c.degraded_ms,
    retries: c.retries,
    alert_after_failures: c.alert_after_failures,
    degraded_after_failures: c.degraded_after_failures,
    method: c.method,
    expected_status: c.expected_status,
    keyword_up: c.keyword_up,
    keyword_down: c.keyword_down,
    auth_method: c.auth_method ?? '',
    auth_user: c.auth_user ?? '',
    auth_pass: c.auth_pass ?? '',
    http_headers: c.http_headers ?? '',
    ignore_tls: c.ignore_tls ?? false,
    check_all_ips: c.check_all_ips ?? false,
    check_ssl: c.check_ssl,
    check_domain: c.check_domain,
    ssl_warn_days: c.ssl_warn_days,
    domain_warn_days: c.domain_warn_days,
    check_locations: c.check_locations,
    location_ids: c.location_ids,
    alert_mutes: c.alert_mutes ?? [],
  }
}

export function checksOverview(): Promise<ChecksOverview> {
  return api<ChecksOverview>('/api/checks/overview')
}

export function createCheck(body: CheckForm): Promise<Check> {
  return api<Check>('/api/checks', { method: 'POST', body: JSON.stringify(body) })
}

export function updateCheck(id: number, body: Partial<CheckForm>): Promise<Check> {
  return api<Check>(`/api/checks/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export function deleteCheck(id: number): Promise<void> {
  return api<void>(`/api/checks/${id}`, { method: 'DELETE' })
}

export function runCheck(id: number): Promise<Check> {
  return api<Check>(`/api/checks/${id}/run`, { method: 'POST' })
}

// Быстрый снуз алертов монитора на N часов (0 = снять).
export function snoozeCheck(id: number, hours: number): Promise<Check> {
  return api<Check>(`/api/checks/${id}/snooze`, {
    method: 'POST',
    body: JSON.stringify({ hours }),
  })
}

export type CheckSample = {
  ts: string
  status: CheckStatus
  latency_ms: number | null
  value: number | null
  message: string
}

export function checkHistory(
  id: number,
  hours = 24,
  locationId?: number,
  range?: { from: number; to: number }, // unix-сек — произвольный диапазон (зум)
  ip?: string, // график по конкретному IP (режим «все адреса»)
): Promise<{ check_id: number; interval_seconds: number; points: CheckSample[] }> {
  const q = locationId != null ? `&location_id=${locationId}` : ''
  const r = range ? `&from_ts=${range.from}&to_ts=${range.to}` : ''
  const ipq = ip ? `&ip=${encodeURIComponent(ip)}` : ''
  return api(`/api/checks/${id}/history?hours=${hours}${q}${r}${ipq}`)
}

export type Uptime = { day: number | null; week: number | null; month: number | null }

export function checkUptime(id: number): Promise<Uptime> {
  return api<Uptime>(`/api/checks/${id}/uptime`)
}

export function checkLog(
  id: number,
  failed = false,
  limit = 100,
): Promise<CheckSample[]> {
  return api<CheckSample[]>(
    `/api/checks/${id}/log?failed=${failed ? 1 : 0}&limit=${limit}`,
  )
}

export type Incident = {
  id: number
  check_id: number
  check_name: string
  status: CheckStatus
  started_at: string
  ended_at: string | null
  last_message: string
  notified?: boolean // ушёл ли алерт (false = сбой короче порога)
  alert_after?: number // порог монитора: столько неудачных проверок подряд
  interval_seconds?: number
}

export function listIncidents(limit = 50, checkId?: number): Promise<Incident[]> {
  const q = checkId != null ? `&check_id=${checkId}` : ''
  return api<Incident[]>(`/api/checks/incidents?limit=${limit}${q}`)
}

// --- алерты ---

export type AlertConfig = {
  telegram_token: string
  telegram_chat: string
  telegram_api: string
  webhook: string
  flood_threshold: number
  enabled: boolean
  muted: boolean
}

export function getAlerts(): Promise<AlertConfig> {
  return api<AlertConfig>('/api/alerts')
}

export function putAlerts(body: {
  telegram_token: string
  telegram_chat: string
  telegram_api: string
  webhook: string
  flood_threshold: number
  muted: boolean
}): Promise<AlertConfig> {
  return api<AlertConfig>('/api/alerts', { method: 'PUT', body: JSON.stringify(body) })
}

export function testAlerts(): Promise<{ sent: boolean; errors: string[] }> {
  return api('/api/alerts/test', { method: 'POST' })
}

// --- правила серверных алертов (текст / вкл / область применения) ---

export type ServerAlertRule = {
  enabled: boolean
  text: string
  scope_type: 'all' | 'groups' | 'servers' | 'checks'
  scope: (string | number)[]
}
export type ServerAlertKind = { key: string; label: string; default_text: string }
export type ServerAlertRules = {
  rules: Record<string, ServerAlertRule>
  kinds: ServerAlertKind[]
}

export function getServerAlertRules(): Promise<ServerAlertRules> {
  return api<ServerAlertRules>('/api/alerts/server-rules')
}
export function putServerAlertRules(
  rules: Record<string, ServerAlertRule>,
): Promise<ServerAlertRules> {
  return api<ServerAlertRules>('/api/alerts/server-rules', {
    method: 'PUT',
    body: JSON.stringify({ rules }),
  })
}
export function getSiteAlertRules(): Promise<ServerAlertRules> {
  return api<ServerAlertRules>('/api/alerts/site-rules')
}
export function putSiteAlertRules(
  rules: Record<string, ServerAlertRule>,
): Promise<ServerAlertRules> {
  return api<ServerAlertRules>('/api/alerts/site-rules', {
    method: 'PUT',
    body: JSON.stringify({ rules }),
  })
}

// --- хранение данных (retention) ---

export type Retention = { server_days: number; sample_days: number }

export function getRetention(): Promise<Retention> {
  return api<Retention>('/api/settings/retention')
}

export function putRetention(body: Retention): Promise<Retention> {
  return api<Retention>('/api/settings/retention', {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

// --- бэкап (конфиг без метрик) ---

export type BackupConfig = { interval_hours: number; keep: number }
export type BackupFileInfo = { name: string; size: number; created_at: string }

export function getBackupConfig(): Promise<BackupConfig> {
  return api<BackupConfig>('/api/backup/config')
}
export function putBackupConfig(b: BackupConfig): Promise<BackupConfig> {
  return api<BackupConfig>('/api/backup/config', {
    method: 'PUT',
    body: JSON.stringify(b),
  })
}
export function exportBackup(): Promise<unknown> {
  return api<unknown>('/api/backup/export')
}
export function listBackups(): Promise<BackupFileInfo[]> {
  return api<BackupFileInfo[]>('/api/backup/list')
}
export function getBackupFile(name: string): Promise<unknown> {
  return api<unknown>(`/api/backup/file/${encodeURIComponent(name)}`)
}
export function runBackup(): Promise<BackupFileInfo> {
  return api<BackupFileInfo>('/api/backup/run', { method: 'POST' })
}
export function restoreBackup(
  data: unknown,
): Promise<{ restored: Record<string, number> }> {
  return api('/api/backup/restore', { method: 'POST', body: JSON.stringify(data) })
}

// --- серверы (агент-push) ---

export type ServerDisk = { mount: string; used: number; total: number }
export type ProcStat = {
  pid: number
  comm: string
  cpu: number
  rss: number
  cmdline?: string
  user?: string
  shared?: number // общая память (RssShmem), байты
  threads?: number
  state?: string
}

export type DockerContainer = {
  name: string
  image: string
  state: string // running / exited / paused / restarting / created
  status: string // «Up 3 hours (healthy)»
  restarts?: number // RestartCount демона
  policy?: string // restart-policy: no/always/unless-stopped/on-failure
  health?: string // healthy/unhealthy/starting
}
export type DockerInfo = {
  present: boolean
  access: boolean
  version?: string
  api_version?: string
  compose?: string
  containers?: DockerContainer[]
}
export type KubeNode = {
  name: string
  ready: boolean
  roles?: string
  version?: string
  ip?: string
}
export type KubeWorkload = {
  ns: string
  kind: string // Deployment/StatefulSet/DaemonSet
  name: string
  ready: number
  desired: number
}
export type KubePod = {
  ns: string
  name: string
  phase: string
  ready: boolean
  restarts: number
  node?: string
  reason?: string
  owner?: string // kind контроллера (Job/ReplicaSet/StatefulSet/DaemonSet/Node); есть с агента 1.39
  image?: string // образ СУБД-контейнера (только у СУБД-подов)
  cred?: KubeCred // откуда под берёт креды БД — для автоподстановки секрета в манифест дампа (агент ≥1.74)
}
// Ссылки на креды СУБД-пода (без значений паролей): панель воспроизведёт их в CronJob-дампе.
export type KubeCred = {
  env_from?: string[] // secretRef из envFrom (весь секрет оптом)
  env?: KubeEnvRef[] // кред-переменные: имя + источник (secretKeyRef или plain-значение user/database)
}
export type KubeEnvRef = {
  name: string
  value?: string // plain — только НЕсекретные (user/database)
  secret?: string // secretKeyRef.name
  key?: string // secretKeyRef.key
}
// «Завершённый» под — отработавшая запись Job/CronJob (Succeeded или Failed от Job),
// а не живой падающий воркоад. Такие поды — история: их не нужно чинить, только удалить
// для очистки. Failed-под без owner (агент <1.39) распознать нельзя — оставляем как есть.
export function podFinished(p: KubePod): boolean {
  if (p.phase === 'Succeeded') return true
  if (p.owner === 'Job' && (p.phase === 'Failed' || p.phase === 'Succeeded')) return true
  return false
}
export type KubeInfo = {
  present: boolean
  access: boolean
  flavor?: string // k0s/k3s/microk8s/kubeadm/kubernetes
  version?: string
  namespaces?: number
  nodes?: KubeNode[]
  workloads?: KubeWorkload[]
  pods?: KubePod[]
}

export type ServerReport = {
  hostname?: string
  os?: string
  agent_version?: string
  cpu_model?: string
  is_vm?: boolean
  virt?: string
  uptime_seconds?: number
  cpu_percent?: number
  mem_used?: number
  mem_total?: number
  swap_used?: number
  swap_total?: number
  load?: number[]
  disks?: ServerDisk[]
  net_rx?: number
  net_tx?: number
  disk_read?: number
  disk_write?: number
  disk_read_iops?: number
  disk_write_iops?: number
  cpu_cores?: number
  cpu_cores_pct?: number[]
  cpu_freq?: number
  cpu_temp?: number
  cpu_throttle?: number
  oom_kill?: number
  mem_cached?: number
  mem_free?: number
  swap_in?: number
  swap_out?: number
  mem_slab?: number
  mem_dirty?: number
  mem_writeback?: number
  net_ifaces?: { if: string; rx: number; tx: number; errs: number; drops: number }[]
  disk_devs?: { dev: string; util: number; await: number; temp: number | null }[]
  top_cpu?: ProcStat[]
  top_mem?: ProcStat[]
  conntrack_count?: number
  conntrack_max?: number
  sock_used?: number
  sock_tcp?: number
  sock_tcp_tw?: number
  sock_udp?: number
  docker?: DockerInfo
  kube?: KubeInfo
  backup?: BackupInfo
  backup_server?: BackupServerInfo
  setup_versions?: Record<string, number> // версии setup-скриптов на ноде: {backup-setup:1,…}
  services?: ServiceInfo[] // прикладные метрики сервисов (очереди RabbitMQ и т.п.)
  web_services?: WebService[] // веб-серверы/прокси (nginx/envoy/…) + сайты, что обслуживают
  db_stats?: DBStat[] // инвентарь СУБД: базы с размерами, логины, версия (хелпер dbstat-setup)
  caps?: Record<string, boolean> // возможности агента: kmsg, proc_full (полный ли /proc)
  clock?: ClockInfo // статус синхронизации времени (timedatectl)
  clock_skew_sec?: number // сдвиг часов ноды относительно панели, сек (± ; считает бэкенд)
}
export type ClockInfo = {
  synced: boolean // NTPSynchronized=yes
  ntp: boolean // синхронизация включена
  service?: string // активный демон времени (systemd-timesyncd/chronyd/…)
}
export type BackupInfo = {
  present: boolean
  restic_found: boolean
  restic_version?: string
  configured: boolean
  timer_enabled: boolean
  timer_active: boolean
  service_result?: string
  metric_present: boolean
  dumps?: DumpStat[] // включённые дампы СУБД: состояние (файлы/место/когда/сколько хранить)
  ts_source?: string // "systemd" = времени из prom-метрики нет, взято из юнита (старый runner)
  success?: number // 1/0 из метрики (отсутствует — метрики нет)
  skipped?: number
  last_backup_ts?: number // epoch последнего бэкапа (конец restic-фазы)
  duration_sec?: number // длительность restic (без дампов)
  full_duration_sec?: number // полная длительность запуска: дампы + restic
  started_ts?: number // epoch старта последнего запуска (с дампами)
  notes?: string[]
  // управление (Фаза 2): доступно, если на ноде установлен helper (backup-setup.sh)
  manageable?: boolean
  mode?: string // include/exclude
  schedule?: string // HH:MM
  includes?: string[]
  excludes?: string[]
  repo_dest?: string // куда бэкапится (rest://host:port/name БЕЗ пароля); агент ≥1.43
  helper_version?: number // версия backup-helper на ноде (панель флагует старые)
}
// текущая версия backup-helper'ов: ноды с меньшей → «переустановить helper»
export const CURRENT_BACKUP_HELPER = 1
export type BackupCreds = {
  repo_url: string
  repopass: string
  cacert_file: string // путь к серту НА НОДЕ (для команд, что бегут на ней же)
  cacert_pem?: string // сам серт — нужен, когда восстанавливаешься на другой машине
  repo_local?: string
  source?: string // client / backup-server
  server_name?: string
}
// данные для восстановления (repo URL + пароль) — читаются с ноды по запросу, НЕ хранятся
export function backupCredentials(serverId: number): Promise<BackupCreds> {
  return api<BackupCreds>(`/api/servers/${serverId}/backup/credentials`, { method: 'POST' })
}
// --- сейф доступов: панель хранит ТОЛЬКО шифротекст, ключ живёт в браузере ---
export type VaultMetaDto = {
  salt: string
  iterations: number
  verifier_nonce: string
  verifier: string
}
export type VaultItemDto = {
  repo: string
  server_id: number | null
  server_name: string
  nonce: string
  ciphertext: string
  updated_at: string
}
export function vaultMeta(): Promise<VaultMetaDto> {
  return api<VaultMetaDto>('/api/vault/meta')
}
export function vaultSetMeta(m: VaultMetaDto): Promise<VaultMetaDto> {
  return api<VaultMetaDto>('/api/vault/meta', { method: 'PUT', body: JSON.stringify(m) })
}
export function vaultList(): Promise<VaultItemDto[]> {
  return api<VaultItemDto[]>('/api/vault')
}
export function vaultSave(items: Omit<VaultItemDto, 'updated_at'>[]): Promise<{ saved: number }> {
  return api<{ saved: number }>('/api/vault', { method: 'PUT', body: JSON.stringify(items) })
}
export function vaultDelete(repo: string): Promise<{ deleted: string }> {
  return api<{ deleted: string }>(`/api/vault/${encodeURIComponent(repo)}`, { method: 'DELETE' })
}
// забыт пароль: сейф стирается целиком (расшифровать его нельзя) и собирается заново
export function vaultReset(): Promise<{ deleted: number }> {
  return api<{ deleted: number }>('/api/vault/reset', { method: 'POST' })
}
// журналируем сам факт расшифровки: доступ к ключам от бэкапов не должен быть невидим
export function vaultOpened(repo = ''): Promise<{ ok: boolean }> {
  return api<{ ok: boolean }>(`/api/vault/opened?repo=${encodeURIComponent(repo)}`, { method: 'POST' })
}

export type RepoStat = {
  // состояние РОТАЦИИ (метрики prune-скрипта, backupserver-setup 0.19+):
  // «бэкап снялся» и «старое вычищается» — разные вещи, вторую раньше никто не мерил
  rotation_ts?: number // когда чистка отрабатывала в последний раз
  rotation_ok?: number // -1 нет данных, 0/1 — отработали ли команды без ошибки
  rotation_removed?: number // снапшотов удалено за прогон (-1 нет данных)
  oldest_snapshot?: number // время самого старого снапшота
  name: string
  valid: boolean // есть config = валидный restic-репо
  size_bytes?: number
  snapshots: number
  last_activity?: number // epoch
  locked?: boolean
  lock_ts?: number // epoch mtime свежего лока: живой бэкап освежает его раз в 5 мин
  keep_last?: number
  keep_daily?: number
  keep_weekly?: number
  keep_monthly?: number
}
export type BackupServerInfo = {
  present: boolean
  running: boolean
  version?: string
  helper_version?: number // версия backupserver-helper (панель флагует старые)
  tls_front?: boolean // поднят self-signed TLS-фронт (caddy) → клиенты могут ходить по HTTPS
  tls_port?: number // порт TLS-фронта (обычно 64101); HTTP rest-server остаётся на 64100
  // место на томе С РЕПОЗИТОРИЯМИ (df по каталогу данных, снимает root-helper):
  // репозитории часто на отдельном диске, и общая метрика диска ноды про заполнение
  // хранилища бэкапов ничего не говорит
  data_dir?: string
  disk_total?: number
  disk_used?: number
  disk_free?: number
  repos?: RepoStat[]
}
export type Server = {
  id: number
  name: string
  group_name: string
  enabled: boolean
  hostname: string
  os: string
  agent_version: string
  target_agent_version: string
  local_ip: string
  external_ip: string
  country?: string // ISO-код страны по IP (офлайн-таблица на бэкенде)
  queue_alert_depth?: number // с какой глубины очереди RabbitMQ алертить (0 = выкл)
  queue_alert_over?: Record<string, number> // переопределения по очередям
  // контейнеры, по которым алерт уже ушёл: {имя: 'down'|'loop'}. Считать это на
  // фронте нельзя — crash-loop определяется по приросту RestartCount в окне,
  // а история есть только на бэкенде
  docker_alerts?: Record<string, string>
  agent_ip: string
  last_report: ServerReport | null
  last_seen: string | null
  snooze_until: string | null
  alert_snoozes: Record<string, string> | null // {тип: ISO-до} — точечный снуз
  online: boolean
  cpu_alert_percent: number
  mem_alert_percent: number
  disk_alert_percent: number
  disk_warn_percent: number
  disk_crit_percent: number
  temp_alert_c: number
  conntrack_alert_percent: number
  disk_temp_alert_c: number
  alert_mutes: string[] | null
  backup_repo_mutes: string[] | null // заглушённые репо бэкап-сервера (по имени)
  backup_audit_mutes?: string[] | null // приглушённые находки покрытия (по ключу)
  backup_not_required: boolean // на сервере бэкап не требуется (алерт снят)
  db_dumps_ok: boolean // дампы СУБД настроены отдельно (пункт снят с главной)
  backup_deadline_hour?: number // до какого часа бэкап должен закончиться (окно)
  backup_anytime?: boolean // бэкап в любое время — не уведомлять о выходе за окно
  offline_after_seconds: number
  alert_sustain_seconds: number // сколько держать превышение до алерта, сек
  agent_advice: string[] // чего агенту не хватает в systemd-юните (человекочитаемо)
  agent_fix_command: string | null // команда-фикс для ноды (drop-in) или null
  helper_advice: HelperAdvice[] // устаревшие setup-скрипты (helper'ы) на ноде → переустановить
  backup_audit?: BackupAudit[] // аудит покрытия бэкапа: что рискует не восстановиться
}
export type QueueStat = {
  name: string
  vhost?: string
  ready: number
  unacked?: number
}
export type ServiceInfo = {
  kind: string // rabbitmq
  source?: string // «под ns/name» / «контейнер X»
  queues?: QueueStat[] // прислана только верхушка по глубине
  total?: number // всего очередей у инстанса
}
export type DBStat = {
  engine: string // pg | mysql | clickhouse | redis
  container?: string // пусто = движок на хосте, не в docker
  version?: string
  dbs?: { name: string; size: number }[] // у redis size = число ключей
  users?: string[] // логины (имена, без паролей — их панель не видит принципиально)
}

export type WebService = {
  kind: string // nginx / ingress-nginx / Envoy / Traefik / HAProxy / Caddy / Apache
  source?: string // где найден (напр. «kubernetes»)
  sites?: string[] // домены/хосты, которые обслуживает (из k8s Ingress или nginx -T)
}
export type DumpStat = {
  engine: string
  container?: string // контейнер с этой базой ("" = нативная установка / helper < v8)
  files: number
  size_bytes: number
  last_ts: number
  keep: number
  min_free_pct?: number // порог свободного места; 0 = защита выключена
  dir?: string // каталог дампов (может быть нестандартным)
  skipped?: boolean // последний прогон пропущен из-за нехватки места
  skip_ts?: number
  skip_free_pct?: number // сколько было свободно при пропуске
  enabled_ts?: number // когда дамп включён (mtime скрипта) — для grace «файлов ещё нет»
}
export type BackupAudit = {
  kind: string // mount / bind / db
  subject: string // путь или имя контейнера
  detail: string
  gap: boolean // true = данных нет в бэкапе; false = есть, но восстановимость под вопросом
  dump_engine?: string // pg/mysql/ch — код движка; пусто = дампить не умеем
  can_dump?: boolean // true = панель снимет дамп сама; false = только предложит манифест
  downtime?: string // непусто = включение дампа стоит простоя (Neo4j Community)
  container?: string // имя контейнера с базой ("" = нативная установка)
  pods?: string[] // ns/name подов с этой СУБД (для генерации манифеста CronJob)
  key?: string // "db:RabbitMQ:cont" — устойчивый ключ находки (для точечного приглушения)
  instance?: string // имя контейнера с этой базой ("" = под/нативная установка)
  muted?: boolean // приглушена вручную: не считается проблемой и не идёт на главную
}
export type HelperAdvice = {
  name: string // backup-setup / backupserver-setup / kube-setup
  label: string // человекочитаемо
  installed: string | null // null = helper до версионирования
  current: string
}
export type ServerEnroll = { server: Server; token: string; install_cmd: string }
export type ServerMetric = {
  ts: string
  cpu_percent: number | null
  mem_percent: number | null
  disk_percent: number | null
  load1: number | null
  net_rx: number | null
  net_tx: number | null
  disk_read: number | null
  disk_write: number | null
  disk_read_iops: number | null
  disk_write_iops: number | null
  cpu_cores_pct: number[] | null
  cpu_freq: number | null
  cpu_temp: number | null
  cpu_throttle: number | null
  oom_kill: number | null
  cpu_user: number | null
  cpu_system: number | null
  cpu_iowait: number | null
  cpu_irq: number | null
  mem_cache: number | null
  mem_free: number | null
  swap_in: number | null
  swap_out: number | null
  mem_slab: number | null
  mem_dirty: number | null
  mem_writeback: number | null
  net_ifaces: { if: string; rx: number; tx: number; errs: number; drops: number }[] | null
  disk_devs: { dev: string; util: number; await: number; temp: number | null }[] | null
  conntrack_count: number | null
  conntrack_max: number | null
  sock_used: number | null
  sock_tcp: number | null
  sock_tcp_tw: number | null
  sock_udp: number | null
  disks: { mount: string; pct: number }[] | null
}
export type ServerForm = {
  name: string
  group_name?: string
  agent_ip?: string
  cpu_alert_percent?: number
  mem_alert_percent?: number
  disk_alert_percent?: number
  disk_warn_percent?: number
  disk_crit_percent?: number
  temp_alert_c?: number
  conntrack_alert_percent?: number
  disk_temp_alert_c?: number
  alert_mutes?: string[]
  offline_after_seconds?: number
  alert_sustain_seconds?: number
}

export function listServers(): Promise<Server[]> {
  return api<Server[]>('/api/servers')
}
export function createServer(body: ServerForm): Promise<ServerEnroll> {
  return api<ServerEnroll>('/api/servers', { method: 'POST', body: JSON.stringify(body) })
}
export function updateServer(
  id: number,
  body: Partial<ServerForm> & {
    enabled?: boolean
    backup_not_required?: boolean
    backup_deadline_hour?: number
    backup_anytime?: boolean
    db_dumps_ok?: boolean
    queue_alert_depth?: number // порог глубины очереди RabbitMQ (0 = выкл)
    queue_alert_over?: Record<string, number> // порог по конкретной очереди
  },
): Promise<Server> {
  return api<Server>(`/api/servers/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
}
export function deleteServer(id: number): Promise<void> {
  return api<void>(`/api/servers/${id}`, { method: 'DELETE' })
}
// Быстрый снуз алертов сервера на N часов (0 = снять).
export function snoozeServer(id: number, hours: number): Promise<Server> {
  return api<Server>(`/api/servers/${id}/snooze`, {
    method: 'POST',
    body: JSON.stringify({ hours }),
  })
}
export type OomEvent = { ts: string; victim: string; count: number }
export function serverOomEvents(id: number, limit = 50): Promise<OomEvent[]> {
  return api<OomEvent[]>(`/api/servers/${id}/oom-events?limit=${limit}`)
}
export type DockerCommand = {
  id: number
  container: string
  action: string
  status: string // pending / running / done / error
  ok: boolean | null
  result: string
}
export function dockerCommand(
  serverId: number,
  container: string,
  action: 'restart' | 'stop' | 'start' | 'logs',
  opts: { tail?: number; since?: number } = {},
): Promise<DockerCommand> {
  return api<DockerCommand>(`/api/servers/${serverId}/docker/command`, {
    method: 'POST',
    body: JSON.stringify({ container, action, tail: opts.tail ?? 200, since: opts.since ?? 0 }),
  })
}
export function dockerCommandStatus(serverId: number, cmdId: number): Promise<DockerCommand> {
  return api<DockerCommand>(`/api/servers/${serverId}/docker/command/${cmdId}`)
}
export type KubeCommand = {
  id: number
  ns: string
  kind: string
  name: string
  action: string
  status: string // pending / running / done / error
  ok: boolean | null
  result: string
}
export function kubeCommand(
  serverId: number,
  ns: string,
  kind: 'deployment' | 'statefulset' | 'daemonset' | 'pod',
  name: string,
  action: 'rollout_restart' | 'delete_pod' | 'logs',
  opts: { tail?: number; since?: number } = {},
): Promise<KubeCommand> {
  return api<KubeCommand>(`/api/servers/${serverId}/kube/command`, {
    method: 'POST',
    body: JSON.stringify({ ns, kind, name, action, tail: opts.tail ?? 400, since: opts.since ?? 0 }),
  })
}
export function kubeCommandStatus(serverId: number, cmdId: number): Promise<KubeCommand> {
  return api<KubeCommand>(`/api/servers/${serverId}/kube/command/${cmdId}`)
}
export type BackupCommand = {
  id: number
  action: string
  status: string // pending / running / done / error
  ok: boolean | null
  result: string
}
export function backupCommand(
  serverId: number,
  body:
    | { action: 'set_paths'; mode: 'include' | 'exclude'; paths: string[] }
    | { action: 'set_schedule'; schedule: string }
    | { action: 'run_now' }
    | { action: 'restic_update' }
    | { action: 'update_image' }
    | { action: 'timesync' }
    | { action: 'dump_remove'; engine: string; container: string }
    | {
        action: 'dump_setup'
        engine: string
        container: string
        dump_dir?: string
        dump_keep?: number
        dump_minfree?: number
      },
): Promise<BackupCommand> {
  return api<BackupCommand>(`/api/servers/${serverId}/backup/command`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
export function backupCommandStatus(serverId: number, cmdId: number): Promise<BackupCommand> {
  return api<BackupCommand>(`/api/servers/${serverId}/backup/command/${cmdId}`)
}
// --- «Настроить бэкап»: оркестрация (провижининг репо + бэкап клиента) ---
export type BackupSetupStep = { step: string; ok: boolean; detail: string }
export type BackupSetupJob = {
  id: number
  status: string // running / done / error
  steps: BackupSetupStep[]
  message: string
}
export type BackupSetupBody = {
  backup_server_id: number
  mode: 'include' | 'exclude'
  paths: string[]
  schedule: string
  keep_last: number
  keep_daily: number
  keep_weekly: number
  keep_monthly: number
  tls: boolean
}
export function backupSetup(serverId: number, body: BackupSetupBody): Promise<BackupSetupJob> {
  return api<BackupSetupJob>(`/api/servers/${serverId}/backup/setup`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
export function backupSetupStatus(serverId: number, jobId: number): Promise<BackupSetupJob> {
  return api<BackupSetupJob>(`/api/servers/${serverId}/backup/setup/${jobId}`)
}
// развернуть rest-server на ноде с нуля (docker/restic/htpasswd + append-only контейнер).
// Прогресс поллится тем же backupSetupStatus.
export function deployBackupServer(
  serverId: number,
  body: { port: number; tls: boolean },
): Promise<BackupSetupJob> {
  return api<BackupSetupJob>(`/api/servers/${serverId}/backup-server/deploy`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
// миграция существующего HTTP rest-server на HTTPS (self-signed TLS-фронт :64101)
export function enableBackupTls(serverId: number): Promise<BackupSetupJob> {
  return api<BackupSetupJob>(`/api/servers/${serverId}/backup-server/enable-tls`, {
    method: 'POST',
  })
}
// точечно приглушить ОДИН тип алерта сервера на N часов (0 = снять)
export function snoozeServerAlert(
  id: number,
  kind: string,
  hours: number,
): Promise<Server> {
  return api<Server>(`/api/servers/${id}/snooze-alert`, {
    method: 'POST',
    body: JSON.stringify({ kind, hours }),
  })
}
export function rotateServerToken(id: number): Promise<ServerEnroll> {
  return api<ServerEnroll>(`/api/servers/${id}/rotate`, { method: 'POST' })
}
// заглушить/включить конкретный репо бэкап-сервера (разовые/неактуальные)
// приглушить ОДНУ находку покрытия: «эту базу бэкапить не нужно», остальные остаются
export function backupAuditMute(id: number, key: string, muted: boolean): Promise<Server> {
  return api<Server>(`/api/servers/${id}/backup/audit-mute`, {
    method: 'POST',
    body: JSON.stringify({ key, muted }),
  })
}

export function backupRepoMute(id: number, repo: string, muted: boolean): Promise<Server> {
  return api<Server>(`/api/servers/${id}/backup/repo-mute`, {
    method: 'POST',
    body: JSON.stringify({ repo, muted }),
  })
}

// --- управляемое обновление агентов (подписанные релизы) ---
// Доступная (подписанная) версия, которую раздаёт панель ('' если релиза нет).
export function getAgentRelease(): Promise<{
  version: string
  setup_versions?: Record<string, number>
  problem?: string // релиз собран криво: раздаётся не тот бинарь, что подписан
}> {
  return api<{ version: string; problem?: string }>('/api/servers/agent-release')
}
// Выставить target-версию: server_ids=undefined → все включённые (иначе canary/подмножество).
export function agentUpdate(version: string, serverIds?: number[]): Promise<Server[]> {
  return api<Server[]>('/api/servers/agent-update', {
    method: 'POST',
    body: JSON.stringify({ version, server_ids: serverIds ?? null }),
  })
}
// Снять target-версию (остановить раскатку).
export function agentUpdateCancel(serverIds?: number[]): Promise<Server[]> {
  return api<Server[]>('/api/servers/agent-update/cancel', {
    method: 'POST',
    body: JSON.stringify({ server_ids: serverIds ?? null }),
  })
}
export function serverMetrics(
  id: number,
  hours = 6,
  range?: { from: number; to: number }, // unix-секунды — произвольный диапазон (зум)
): Promise<ServerMetric[]> {
  const q = range
    ? `from_ts=${range.from}&to_ts=${range.to}`
    : `hours=${hours}`
  return api<ServerMetric[]>(`/api/servers/${id}/metrics?${q}`)
}

// --- локации (прокси) ---

export type Location = {
  id: number
  name: string
  url: string
  enabled: boolean
  created_at: string
}

export function listLocations(): Promise<Location[]> {
  return api<Location[]>('/api/locations')
}

export function createLocation(body: {
  name: string
  url: string
  enabled?: boolean
}): Promise<Location> {
  return api<Location>('/api/locations', { method: 'POST', body: JSON.stringify(body) })
}

export function updateLocation(
  id: number,
  body: Partial<{ name: string; url: string; enabled: boolean }>,
): Promise<Location> {
  return api<Location>(`/api/locations/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export function deleteLocation(id: number): Promise<void> {
  return api<void>(`/api/locations/${id}`, { method: 'DELETE' })
}

export type ProxyTestResult = { ok: boolean; message: string; latency_ms: number | null }

// Проверка доступности прокси локации (идёт ли через него трафик).
export function testProxy(url: string): Promise<ProxyTestResult> {
  return api<ProxyTestResult>('/api/locations/test', {
    method: 'POST',
    body: JSON.stringify({ url }),
  })
}

export type LocationResult = {
  location_id: number
  name: string
  enabled: boolean
  direct: boolean
  status: CheckStatus
  latency_ms: number | null
  message: string
  checked_at: string
}

export function checkLocations(id: number): Promise<LocationResult[]> {
  return api<LocationResult[]>(`/api/checks/${id}/locations`)
}
