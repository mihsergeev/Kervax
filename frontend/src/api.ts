import { currentLang, tr } from './i18n'
import { trBody } from './backendText'
import { noteApiFailure, uiHeaders } from './errorLog'

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
  // страница и раздел: если запрос не удастся, бэкенд запишет в журнал ошибок, где это было
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...uiHeaders(),
    ...(options.headers as Record<string, string> | undefined),
  }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  const method = (options.method || 'GET').toUpperCase()

  let res: Response
  try {
    res = await fetch(path, { ...options, headers })
  } catch (e) {
    noteApiFailure(method, path, 0, e instanceof Error ? e.message : String(e))
    throw e
  }
  if (res.status === 204) return undefined as T

  const body = await res.json().catch(() => null)
  if (!res.ok) {
    const detail =
      typeof body?.detail === 'string' ? body.detail : tr('Ошибка HTTP {code}', { code: res.status })
    // последние неудачные запросы уходят вместе с "Сообщить о проблеме"
    noteApiFailure(method, path, res.status, typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail ?? ''))
    throw new ApiError(detail, res.status)
  }
  // тексты проблем бэкенд пишет по-русски: английской панели переводим то, что умеем
  return (currentLang() === 'en' ? trBody(body) : body) as T
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

// своя учетка: последние входы с адресами, неудачные попытки за сутки, когда менялся пароль
export type MyActivity = {
  logins: { ts: string; action: string; ip: string }[]
  fails_24h: number
  password_changed: string | null
}
export function getMyActivity(): Promise<MyActivity> {
  return api<MyActivity>('/api/auth/me/activity')
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
// Окно "Пользователи": учетки с последним входом и действиями из журнала, сам журнал
export type AuditEntry = { id: number; ts: string; username: string; action: string; target: string; detail: string }
export type UserCard = PanelUser & {
  tg_linked: boolean
  last_login: string | null
  last_ip: string
  fails_24h: number
  actions_30d: number
}
export type UsersOverview = { users: UserCard[]; audit: AuditEntry[]; fails_24h: number; fail_ips_24h: number }
export function getUsersOverview(): Promise<UsersOverview> {
  return api<UsersOverview>('/api/users/overview')
}
// защита входа: правило блокировки, белый список, блокировки и неудачные входы по адресам
export type AllowEntry = { net: string; note: string }
export type LoginBanRow = {
  key: string
  since: string
  until: string
  failures: number
  logins: string
  repeat: boolean
  blocked: number
  allowed: boolean
}
export type LoginFailRow = {
  key: string
  fails: number
  logins: string[]
  last: string
  allowed: string | null
  banned_until: string | null
}
export type LoginGuard = {
  max_failures: number
  ban_minutes: number
  window_minutes: number
  fail_choices: number[]
  ban_choices: number[]
  allow: AllowEntry[]
  my_ip: string
  my_allowed: string | null
  bans: LoginBanRow[]
  recent: LoginFailRow[]
}
export function getLoginGuard(): Promise<LoginGuard> {
  return api<LoginGuard>('/api/login-guard')
}
export function saveLoginGuard(body: { max_failures: number; ban_minutes: number; allow: AllowEntry[] }): Promise<LoginGuard> {
  return api<LoginGuard>('/api/login-guard', { method: 'PUT', body: JSON.stringify(body) })
}
// левые запросы к панели: что сейчас в алерте и кто за сутки слал отказы или всплески
export type PanelReqServer = { id: number; name: string }
export type PanelReqActive = {
  key: string
  who: string
  servers: PanelReqServer[]
  since: string | null
  last_over: string | null
  what: string
  hint: string
  notified: boolean
}
export type PanelReqSource = {
  key: string
  who: string
  servers: PanelReqServer[]
  bad: number
  peak: number
  codes: Record<string, number>
  paths: [string, number][]
  first: string
  last: string
  active: boolean
  hint: string
}
export type PanelRequests = {
  window_min: number
  bad_limit: number
  flood_per_server: number
  typical_per_server: number
  write_floor: number
  active: PanelReqActive[]
  sources: PanelReqSource[]
  more: number
}
export function getPanelRequests(): Promise<PanelRequests> {
  return api<PanelRequests>('/api/panel-requests')
}
export function unbanLogin(key: string): Promise<LoginGuard> {
  return api<LoginGuard>('/api/login-guard/unban', { method: 'POST', body: JSON.stringify({ key }) })
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

export type CheckType = 'http' | 'tcp_port' | 'cert' | 'push' | 'dns'
export type CheckStatus = 'up' | 'degraded' | 'down' | 'unknown'

// разбивка проверки по IP (режим «проверять все адреса домена»)
export type IpResult = {
  ip: string
  status: CheckStatus
  latency_ms: number | null
  message: string
}

// разбивка проверки по дополнительным путям монитора: pending - результата еще нет (путь
// только добавили), skipped - не проверялся, потому что недоступен основной адрес
export type PathResult = {
  path: string
  status: CheckStatus | 'pending' | 'skipped'
  latency_ms: number | null
  message: string
}

export type Check = {
  id: number
  name: string
  type: CheckType
  target: string
  // push: адрес /api/agent/push/<токен>, сигнал ждем каждые push_period секунд
  push_token?: string
  push_period?: number
  push_grace?: number
  push_at?: string | null
  push_fail?: string
  // dns: тип записи, ожидаемое по строке, equal | contains, резолвер; baseline - запомненный ответ
  dns_type?: string
  dns_expect?: string
  dns_match?: 'equal' | 'contains'
  dns_server?: string
  dns_baseline?: string[] | null
  dns_last?: string[] | null
  // от какого сервера зависит: auto - панель находит сама, server - parent_server_id, off - ни от чего.
  // parents - на ком монитор держится на самом деле (why: local, site, ip, manual)
  parent_mode?: ParentMode
  parent_server_id?: number | null
  parents?: CheckParent[]
  note?: string
  note_by?: string
  note_at?: string | null
  known_issue?: KnownIssue | null
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
  // проверять изнутри сервера: сайт закрыт белым списком, и панель до него не
  // дотянется. Какой именно сервер — панель находит сама по домену.
  probe_local: boolean
  probe_server_name?: string | null
  check_all_ips: boolean
  last_ip_results: IpResult[] | null
  // пути того же сайта, которые проверяются вместе с основным адресом (напр. /health у API)
  extra_paths: string[]
  last_path_results: PathResult[] | null
  // фазы последней проверки из панели и проверка изнутри сервера сайта при сбое (CheckPhases.tsx)
  last_phases?: CheckPhases | null
  last_inside?: CheckInside | null
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
  flaky_since?: string | null // с какого момента отвечает через раз (сбои вперемешку с успешными)
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
  flaky?: number // отвечает через раз
  loc_summary?: { id: number; name: string; down: number; total: number }[]
  open_incidents: number
  checks: Check[]
}

// поля, отправляемые при создании/редактировании (бэкенд подставит дефолты)
export type ParentMode = 'auto' | 'server' | 'off'
export interface CheckParent {
  id: number
  name: string
  why: 'local' | 'site' | 'ip' | 'manual'
  silent?: boolean // родитель молчит: алерты монитора ждут его
}

export type CheckForm = {
  name: string
  type: CheckType
  target: string
  push_period?: number
  push_grace?: number
  dns_type?: string
  dns_expect?: string
  dns_match?: 'equal' | 'contains'
  dns_server?: string
  parent_mode?: ParentMode
  parent_server_id?: number | null
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
  probe_local?: boolean
  check_all_ips?: boolean
  check_ssl?: boolean
  check_domain?: boolean
  ssl_warn_days?: number[]
  domain_warn_days?: number[]
  check_locations?: boolean
  location_ids?: number[] | null
  alert_mutes?: string[]
  extra_paths?: string[]
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
  probe_local?: boolean
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

// Итог по одному домену из «Поставить на мониторинг». check_id — монитор (0 — не заведён
// или вне видимости учётки); local/server — проверяет агент этой ноды; reason — почему
// монитор не заведён: monitored — домен уже на мониторинге, invalid — имя не годится
// (подробность в problem); пусто — заведён.
export type AdoptedDomain = {
  domain: string
  check_id: number
  local: boolean
  server: string
  reason: '' | 'monitored' | 'invalid'
  problem: string
}

export type AdoptResult = {
  created: number
  skipped: string[]
  hosts: Record<string, number>
  local: number
  group_name: string // куда легли мониторы (учётке с нарезкой — первая разрешённая группа)
  items: AdoptedDomain[]
}

// Завести мониторы по доменам веб-сервиса. Возвращает обновлённую карту хостов,
// чтобы галочки перерисовались без перезагрузки страницы. local — какие из доменов
// проверять изнутри сервера, где они найдены (ноду панель вычисляет сама).
export function adoptDomains(
  domains: string[],
  group_name = '',
  local: string[] = [],
): Promise<AdoptResult> {
  return api('/api/checks/adopt', {
    method: 'POST',
    body: JSON.stringify({ domains, group_name, local }),
  })
}

// Разовая проверка найденного домена перед постановкой на мониторинг: снаружи (панелью)
// и изнутри сервера, где он найден (агентом). Это не мониторинг — итог живёт 12 часов.
// pending — ещё проверяется; none (изнутри) — проверить было некому. *_kind — что не так
// одним словом: http, dns, tls, refused, timeout, reset, connect, other, offline, no_answer.
export type ProbeStatus = 'pending' | 'up' | 'degraded' | 'down' | 'none'
export type DomainProbe = {
  domain: string
  started_at: string
  ext_status: ProbeStatus
  ext_latency_ms: number | null
  ext_message: string
  ext_kind: string
  ext_code: number
  local_status: ProbeStatus
  local_latency_ms: number | null
  local_message: string
  local_kind: string
  local_code: number
  local_server: string
}
// force=false — домены со свежим итогом не перепроверяются (открытие мастера)
export function probeDomains(domains: string[], force = false): Promise<{ items: DomainProbe[] }> {
  return api('/api/checks/discovered/probe', {
    method: 'POST',
    body: JSON.stringify({ domains, force }),
  })
}
export function domainProbes(): Promise<{ items: DomainProbe[] }> {
  return api('/api/checks/discovered/probes')
}

// --- брендирование (свой логотип) ---
export type Branding = {
  logo: boolean
  title: string
  plate: 'auto' | 'always' | 'never'
  plate_auto: boolean // автоанализ: на ТЁМНОЙ теме нужна плашка
  plate_light: boolean | null // на светлой; null — залит до разделения по темам
  dark_logo: boolean // есть отдельный вариант для тёмной темы
  dark_plate: boolean // у варианта для тёмной темы свой фон
  version: number
}
export function getBranding(): Promise<Branding> {
  return api('/api/branding')
}
// ?v= меняется при замене логотипа: файл кэшируется на неделю, и без версии
// браузер показывал бы старый ещё долго после замены
export function brandingLogoUrl(version: number, dark = false): string {
  return `/api/branding/${dark ? 'logo-dark' : 'logo'}?v=${version}`
}
// data/dark пусты — файлы не меняются, сохраняются только настройки
export function putBranding(body: {
  data?: string
  plate: string
  plate_auto: boolean
  plate_light: boolean | null
  title: string
  dark?: string
  dark_plate?: boolean
  dark_remove?: boolean
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
    probe_local: c.probe_local ?? false,
    check_all_ips: c.check_all_ips ?? false,
    check_ssl: c.check_ssl,
    check_domain: c.check_domain,
    ssl_warn_days: c.ssl_warn_days,
    domain_warn_days: c.domain_warn_days,
    check_locations: c.check_locations,
    location_ids: c.location_ids,
    alert_mutes: c.alert_mutes ?? [],
    extra_paths: c.extra_paths ?? [],
    push_period: c.push_period,
    push_grace: c.push_grace,
    dns_type: c.dns_type,
    dns_expect: c.dns_expect,
    dns_match: c.dns_match,
    dns_server: c.dns_server,
    parent_mode: c.parent_mode ?? 'auto',
    parent_server_id: c.parent_server_id ?? null,
  }
}

export function checksOverview(): Promise<ChecksOverview> {
  return api<ChecksOverview>('/api/checks/overview')
}

export function createCheck(body: CheckForm): Promise<Check> {
  return api<Check>('/api/checks', { method: 'POST', body: JSON.stringify(body) })
}

// Пробная проверка нового монитора до сохранения (backend app/try_probe.py). Части форма
// спрашивает одновременно: панель отвечает за доли секунды, а локации и срок домена дольше.
export type TryPart = 'panel' | 'expiry' | 'locations' | 'inside'
export type TryIn = {
  part: TryPart
  check_id?: number // правка: сам монитор дублем себе не считается
  type: 'http' | 'tcp_port' | 'dns'
  target: string
  port?: number
  timeout_ms?: number
  degraded_ms?: number
  method?: string
  expected_status?: string
  keyword_up?: string
  keyword_down?: string
  auth_method?: string
  auth_user?: string
  auth_pass?: string
  http_headers?: string
  ignore_tls?: boolean
  extra_paths?: string[]
  dns_type?: string
  dns_server?: string
  dns_expect?: string
  dns_match?: 'equal' | 'contains'
}
export type TryResult = {
  status: 'up' | 'degraded' | 'down' | 'pending' | 'none'
  latency_ms: number | null
  message: string
  // вид беды одним словом (http, reset, tls, dns, refused, timeout, connect, other) и код;
  // у проверки изнутри еще no_server, offline, no_answer
  kind: string
  code: number
}
export type TryPanel = TryResult & {
  ip_results: { ip: string; status: string; latency_ms: number | null; message: string }[] | null
  path_results: { path: string; status: string; latency_ms: number | null; message: string }[] | null
  // dns: что ответил резолвер; baseline "new" - монитор запомнит этот ответ, "kept" - сравнили
  // с тем, что он уже запомнил
  answers?: string[] | null
  baseline?: '' | 'new' | 'kept'
}
export type TryLocation = TryResult & { id: number; name: string }
export type TryInside = TryResult & {
  server_id: number | null
  server_name: string
  web: string
  request_id: number | null
  fast: boolean
  deadline: number
}
export type CheckTryOut = {
  panel: TryPanel | null
  ssl_days: number | null
  ssl_message: string
  domain_days: number | null
  domain_message: string
  locations: TryLocation[] | null
  inside: TryInside | null
  group: string
  existing_id: number | null
  existing_name: string
}

export function tryCheck(body: TryIn): Promise<CheckTryOut> {
  return api<CheckTryOut>('/api/checks/try', { method: 'POST', body: JSON.stringify(body) })
}

export function tryCheckInside(requestId: number): Promise<CheckTryOut> {
  return api<CheckTryOut>(`/api/checks/try/${requestId}`)
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

// Итог «Проверить сейчас»: монитор + ответ именно на это нажатие. Сайт, который
// проверяет агент на ноде, отвечает не сразу — тогда run_pending несёт номер запроса,
// и карточка спрашивает runCheckResult, пока не придёт вердикт или run_error.
export type CheckRun = Check & {
  run_pending: number | null
  run_fast: boolean // агент отвечает быстрым путём — секунды, а не интервал отчёта
  run_status: '' | CheckStatus
  run_message: string
  run_latency_ms: number | null
  run_at: string | null
  run_source: 'panel' | 'agent'
  run_server: string
  run_error: string // проверить не удалось: агент молчит, нода не на связи
}

export function runCheck(id: number): Promise<CheckRun> {
  return api<CheckRun>(`/api/checks/${id}/run`, { method: 'POST' })
}

export function runCheckResult(id: number, requestId: number): Promise<CheckRun> {
  return api<CheckRun>(`/api/checks/${id}/run/${requestId}`)
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
  // фазы запроса, мс (среднее по точке); нет - у точки разбивки нет
  ph_dns?: number | null
  ph_connect?: number | null
  ph_tls?: number | null
  ph_wait?: number | null
}
// фазы http-запроса, мс; fail - фаза, на которой запрос оборвался
export type CheckPhases = {
  dns?: number
  connect?: number
  tls?: number
  wait?: number
  body?: number
  fail?: 'dns' | 'connect' | 'tls' | 'wait' | 'body'
}
// проверка изнутри сервера сайта: asked - когда спросили, ts - когда агент ответил
export type CheckInside = {
  asked?: string
  ts?: string
  server_id?: number
  server?: string
  status?: CheckStatus
  latency_ms?: number | null
  message?: string
}

export function checkHistory(
  id: number,
  hours = 24,
  locationId?: number,
  range?: { from: number; to: number }, // unix-сек — произвольный диапазон (зум)
  ip?: string, // график по конкретному IP (режим «все адреса»)
): Promise<{
  check_id: number
  interval_seconds: number
  points: CheckSample[]
  step_seconds?: number // ширина бина — чтобы разложить ленту статуса на всё окно
}> {
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
  tg_buttons: boolean
  tz: string
  // как идет опрос бота (кнопки под алертами): ok | conflict | error | off
  tg_poll: { status: string; error: string; ts: string; bot: string }
  escalation: Escalation
  esc_candidates: { username: string; linked: boolean }[]
  routes: AlertRoutes
  // группы серверов и сайтов - для выбора в чатах по группам
  route_groups: { servers: string[]; sites: string[] }
  // чаты, которые последний раз не приняли сообщение
  routes_state: Record<string, { err: string; at: string }>
}

// Чаты по группам: алерты выбранных групп идут еще и в свои чаты. main: 'all' - основной чат
// получает все, 'rest' - только то, что не ушло в чаты по группам
export type AlertRoute = { name: string; chat: string; server_groups: string[]; site_groups: string[] }
export type AlertRoutes = { main: 'all' | 'rest'; chats: AlertRoute[] }

// Повтор алерта, на который никто не нажал «взял»
export type Escalation = {
  enabled: boolean
  after_min: number
  repeat: number
  chat: string
  users: string[]
  kinds: string[]
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
  tg_buttons: boolean
  tz: string
  escalation?: Escalation
  routes?: AlertRoutes
}): Promise<AlertConfig> {
  return api<AlertConfig>('/api/alerts', { method: 'PUT', body: JSON.stringify(body) })
}

export function testAlertRoute(chat: string): Promise<{ sent: boolean; errors: string[] }> {
  return api('/api/alerts/routes/test', { method: 'POST', body: JSON.stringify({ chat }) })
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

// Окно "Хранение данных": размер базы и из чего она состоит (app/retention.py)
export type RetentionPart = { key: string; bytes: number; rows?: number | null }
export type RetentionGroup = {
  id: 'srv' | 'chk'
  days: number
  oldest?: string | null
  bytes: number
  span_days: number // за сколько дней накоплено лежащее, не больше срока
  per_day: number
  parts: RetentionPart[]
}
export type RetentionOverview = {
  db_bytes: number | null
  estimated: boolean // SQLite без dbstat: размеры таблиц - оценка по числу строк
  groups: RetentionGroup[]
  fixed: { key: string; days: number; bytes?: number | null; rows?: number | null }[]
  other_bytes: number | null
  prune_every: number
}

export function getRetentionOverview(): Promise<RetentionOverview> {
  return api<RetentionOverview>('/api/settings/retention/overview')
}

export function putRetention(body: Retention): Promise<Retention> {
  return api<Retention>('/api/settings/retention', {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

// --- доступ ansible к списку нод ---
// Плагин инвентаря в репо ansible спрашивает ноды у всех панелей сам: одна команда
// обновляет helper'ы везде, без копирования списка хостов из каждой панели.

// История отправленных алертов (под права учетки, как персональная рассылка)
export type AlertEventDto = {
  id: number
  ts: string
  kind: string
  label: string // подпись вида, как в правилах алертов
  target: string // сервер или монитор; пусто у сводок по всему парку
  section: string
  recovery: boolean
  text: string // текст без разметки, как в Telegram
}
export type AlertHistory = {
  events: AlertEventDto[]
  more: boolean // есть еще раньше: before_id = id последнего
  summary: {
    total: number
    fires: number
    recoveries: number
    kinds: { kind: string; label: string; n: number }[] // чаще всего срабатывали, за весь период
    targets: { target: string; section: string; n: number; srv_id: number | null; chk_id: number | null }[]
    items: ReportNoise[] // что шумело: что именно и где, как в отчете
    more_items: number
  }
}
export type AlertHistoryQuery = {
  days?: number
  target?: string
  kind?: string
  q?: string
  only?: 'all' | 'fires' | 'recoveries'
  before_id?: number
}
export function alertHistory(p: AlertHistoryQuery): Promise<AlertHistory> {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== '' && v !== 0) qs.set(k, String(v))
  return api<AlertHistory>(`/api/alert-history?${qs}`)
}

export type AnsibleAccess = { enabled: boolean; created_at: string | null; used_at: string | null }

export function getAnsibleAccess(): Promise<AnsibleAccess> {
  return api<AnsibleAccess>('/api/settings/ansible')
}

// Нода инвентаря ansible для окна: то же, что получает плагин, с подробностями
export type AnsibleNode = {
  id: number
  name: string
  hostname: string
  ips: string[]
  online: boolean
  enabled: boolean
  rollout: boolean // в группе kervax_outdated
  outdated: HelperAdvice[]
  watchdog_off: boolean
  issues: string[] // docker_sock / proxy_old / restic_old
  sock: { name: string; image: string; kind: string }[]
  proxy_old: { name: string; image: string; kind: string; version: string; built: number; age_days: number }[]
  restic: string
  restic_target: string
}

export function getAnsibleNodes(): Promise<{ nodes: AnsibleNode[] }> {
  return api('/api/settings/ansible/nodes')
}

export function issueAnsibleToken(): Promise<{ token: string; created_at: string }> {
  return api('/api/settings/ansible', { method: 'POST' })
}

export function revokeAnsibleToken(): Promise<void> {
  return api('/api/settings/ansible', { method: 'DELETE' })
}

// --- бэкап (конфиг без метрик) ---

export type BackupConfig = { interval_hours: number; keep: number }
export type BackupFileInfo = { name: string; size: number; created_at: string }
// Окно "Бэкап": расписание, последний и следующий автобэкап, файлы на диске и что входит в бэкап
export type BackupOverview = {
  interval_hours: number
  keep: number
  last_auto: string | null
  next_auto: string | null
  files: BackupFileInfo[]
  total_bytes: number
  parts: { key: string; rows: number }[]
}
export function getBackupOverview(): Promise<BackupOverview> {
  return api<BackupOverview>('/api/backup/overview')
}

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

export type ServerDisk = {
  mount: string
  used: number
  total: number
  avail?: number // доступно не-root процессам (агент 2.15+): без резерва ext4
  inodes?: number // всего inode (агент 2.15+; у btrfs нет)
  inodes_used?: number
}
// Прогноз заполнения (планировщик раз в полчаса по истории): только разделы с устойчивым
// ростом, до которых осталось не больше месяца
// since - с какого момента это видно (ISO): для "Что сломано" - "2 дн"
// section и srv - куда вести, если не в карточку сервера: раздел "Кубер" (sec - вкладка кластера),
// "Сервисы" или "Бэкапы" (srv - окно бэкап-сервера, иначе клиента). sub "resolver" у DNS - лежит
// один из резолверов, остальные отвечают
export type ServerProblem = {
  kind: string; level: number; text: string; sec: string; mute: string; since?: string | null
  section?: 'kuber' | 'services' | 'backups' | 'docker'; srv?: boolean; sub?: string
  unit?: string; log?: string[] // юнит в цикле перезапусков и последние строки его лога
}
// Кто ест CPU группами (агент 2.21): процессы с одним именем у одного владельца. cpu - % одного
// ядра (как top), spin - сколько из них давно крутят по ядру вхолостую, spin_age - возраст старшего
// Пишущий на диск по io.stat его cgroup: записано и прочитано за s секунд (около часа, у недавно
// запущенного меньше)
export type DiskWriter = { kind: 'container' | 'pod' | 'unit'; name: string; w: number; r: number; s: number }
// кто качает трафик (агент 2.34): принято и отдано за окно s секунд
export type NetUser = { kind: 'container' | 'pod'; name: string; rx: number; tx: number; s: number }
export type DbHealth = {
  engine: string
  container: string
  role: string // primary | replica
  replicas: number
  lag: number
  xact: number
  xid: number
  problems: { level: number; text: string }[]
  load?: DbLoad | null // что грузит PostgreSQL за последний час (dbstat-setup 0.8)
  my?: MyLoad | null // что грузит MySQL/MariaDB за последний час (dbstat-setup 0.11)
  advice?: DbAdvice[] // советы по настройкам (dbstat-setup 0.9), текст по ключу k рисует фронт
  pgss_fix?: OsFixStep[] // как включить pg_stat_statements: команды под экземпляр (db_health.pgss_fix)
  ch?: ChHealth // ClickHouse (dbstat-setup 0.10)
  rd?: RdHealth // Redis (dbstat-setup 0.10)
}
// ClickHouse: партиция с наибольшим числом кусков и пороги замедления и отказа вставок, мутации,
// реплики и очередь репликации, вставки с прошлого прогона helper'а (win секунд), самые долгие
// запросы за час из query_log (тексты нормализованы, литералы - ?)
export type ChHealth = {
  parts: { max: number; tbl: string; part: string; delay: number; throw: number }
  mut: { n: number; age: number; tbl: string; err: string }
  rep: { n: number; ro: number; delay: number; queue: number; ro_tbl: string; q_n: number; q_age: number; q_err: string }
  detached: number
  merges: number
  merge_max: number
  ins: { win: number; n: number; fail: number; rej: number; del: number }
  qlog: boolean
  qerr: number
  top: { n: number; ms: number; rd: number; mem: number; q: string }[]
  cut: number
}
// Redis: память и политика вытеснения, последний снимок и AOF, вытеснение и отказы в подключении и
// самые тяжелые команды с прошлого прогона (win секунд, us - микросекунды)
export type RdHealth = {
  link: string
  down: number
  mem: number
  maxmem: number
  policy: string
  rdb: string
  rdb_changes: number
  rdb_age: number
  aof: number
  aof_status: string
  stop_writes: string
  win: number
  evicted: number
  rejected: number
  cmds: { c: string; n: number; us: number }[]
}
// Совет по настройкам PostgreSQL: k - что именно, остальное - цифры для текста (байты, штуки)
export type DbAdvice = { k: string } & Record<string, string | number | boolean>
// Запрос из pg_stat_statements: время, вызовы, временные файлы и чтение в пересчете на час, доля
// во времени всех запросов. Пустой q: с db - текст скрыт (пароль, секрет), без db - еще не прочитан
export type DbLoadQuery = { db: string; q: string; calls: number; ms: number; temp: number; rd: number; share: number; avg_ms: number }
// Счетчики в пересчете на час, null - эта версия PostgreSQL их не считает. win - за сколько секунд
// замер (0 - helper только начал копить снимки или счетчики сбросили). pss: on / noext (загружен,
// расширение не создано) / off. qtext - размер файла текстов pg_stat_statements
export type DbLoad = {
  win: number
  pss: string
  qtext: number
  cut: boolean
  temp_b?: number | null
  temp_f?: number | null
  xact?: number | null
  rd?: number | null
  wal_rec?: number | null
  fpi?: number | null
  wal_b?: number | null
  wbf?: number | null
  ck_t?: number | null
  ck_r?: number | null
  ms_all?: number
  pbase?: number // 0 - в снимке часовой давности запросов еще не было (только что включили, dbstat-setup 0.14)
  top: DbLoadQuery[]
}
// MySQL/MariaDB за последний час (db_health.my_load_view): счетчики SHOW GLOBAL STATUS в пересчете на
// час (null - эта версия их не считает), чтения с диска в байтах и доля попаданий в буфер InnoDB, запросы
// из performance_schema (ps - включен ли он)
export type MyLoad = {
  win: number
  ps: string
  cut: boolean
  run: number
  max_used: number
  q?: number | null
  sel?: number | null
  wr?: number | null
  slow?: number | null
  tmp?: number | null
  tmp_disk?: number | null
  req?: number | null
  data_w?: number | null
  redo?: number | null
  log_waits?: number | null
  lock_waits?: number | null
  lock_ms?: number | null
  deadlocks?: number | null
  aborted?: number | null
  refused?: number | null
  full_join?: number | null
  sort_merge?: number | null
  rd?: number | null
  hit?: number | null
  ms_all?: number
  top: MyLoadQuery[]
}
// запрос из performance_schema: в пересчете на час; rows - просмотрено строк, noidx - вызовов без индекса
export type MyLoadQuery = {
  db: string; q: string; calls: number; ms: number; rows: number; tmp: number; noidx: number
  share: number; avg_ms: number; rows_call: number
}
// Готовый compose без docker-сокета от helper'а sockfix-setup (docker_exposure.fixes): у готового -
// разница и команды, у неготового - код причины (why) и подробность
export interface SockFix {
  ok: boolean
  why?: string
  why_detail?: string
  file?: string
  dir?: string
  project?: string
  service?: string
  fixed?: string
  diff?: string
  gid?: number
  nets?: string[]
  sock?: string
  compose?: string
  apply?: string
  check?: string
  rollback?: string
}

// Как обновить старый образ внешнего прокси (docker_exposure.upgrade): target - на что, "" - своя
// сборка; why - почему готовых команд нет (nohelper - нет helper'а sockfix-setup 0.2, иначе код
// helper'а); steps - тексты во фронте по id, команды под ноду, arg - файл:строка или образ
export interface ProxyUpgrade {
  target: string
  why?: string
  steps: { id: string; cmd?: string; arg?: string }[]
}

export type MemLeak = {
  key: string
  kind: 'docker' | 'pod' | 'proc'
  name: string // контейнер, ns/под или "имя@пользователь"
  rate: number // байт в сутки
  eta_days: number | null // на сколько суток хватит свободной памяти при таком росте
  series: number[]
  level: number
  text: string
}
export type CpuGroup = {
  comm: string
  kind?: 'container' | 'pod' | 'unit' | ''
  owner?: string // контейнер, ns/имя пода ("pod <uid8>", если имени агент не знает) или юнит
  n: number
  cpu: number
  spin?: number
  spin_age?: number
}
export type DiskForecastItem = { mount: string; kind: 'space' | 'inode'; pct: number; rate: number; eta_h: number }
export type DiskForecast = { ts: number; items: DiskForecastItem[] }
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
  mem?: number // память работающего контейнера, байты (агент 2.14+, как docker stats)
  cpu?: number // CPU работающего контейнера, % одного ядра (агент 2.21+)
  // у остановленного (агент 2.22+): почему он лежит
  err?: string // State.Error: докер пытался его запустить и не смог
  exit?: number // код выхода
  max_retry?: number // on-failure:N
  oom?: boolean // убит OOM-киллером
}
export type DockerInfo = {
  present: boolean
  access: boolean
  version?: string
  api_version?: string
  compose?: string
  containers?: DockerContainer[]
}
// CPU в милликорах и память в байтах: использование (metrics-server, как kubectl top) или объем
export type KubeUse = { cpu_m: number; mem: number }
export type KubeNode = {
  name: string
  ready: boolean
  roles?: string
  version?: string
  ip?: string
  use?: KubeUse // занято сейчас (агент 2.19+, если в кластере есть metrics-server)
  cap?: KubeUse // объем ноды
  pressure?: string[] // MemoryPressure/DiskPressure/PIDPressure/NetworkUnavailable (агент 2.26)
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
  cred?: KubeCred // откуда под берёт креды БД (без значений; агент ≥1.74)
  // только у подов этой ноды (агент 2.16+): память как в kubectl top и что перезапускать
  mem?: number
  cpu?: number // % одного ядра по cgroup пода (агент 2.21+, только поды этой ноды)
  ctrl?: string // deployment/имя, statefulset/имя, daemonset/имя
  use?: KubeUse // по всему кластеру из metrics-server (агент 2.19+), где бы под ни работал
  u?: string // начало uid: по нему находятся имена подов воркера без доступа к kube-api
  // агент 2.26, только у подов контроллеров: нет лимита памяти, нет ни одной пробы, почему
  // под не встает в расписание
  nolim?: boolean
  noprobe?: boolean
  msg?: string
}
export type KubeFailedJob = { ns: string; name: string; ts?: number; reason?: string }
// Ссылки на креды СУБД-пода (без значений паролей).
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
// CronJob кластера: расписание и прогоны (агент отдает все, панель судит по ним дампы и обычные кроны)
export type KubeCronJob = {
  ns: string; name: string; schedule?: string; suspend?: boolean; image?: string
  last_schedule?: number; last_success?: number; active?: number
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
  cronjobs?: KubeCronJob[]
  failed_jobs?: KubeFailedJob[] // упавшие за неделю разовые Job (агент 2.26)
}

// Один порт на одном адресе по внешней проверке. state: open - отвечает, closed - отказ
// (никто не слушает или REJECT), filtered - тишина (файрвол), unknown - проверить не вышло
export type ExposedPort = {
  ip: string
  port: number
  state: 'open' | 'closed' | 'filtered' | 'unknown'
  what: string // контейнер, сервис кластера или владелец сокета
  name: string // что это обычно за сервис
  risk: boolean // базе, очереди, Docker API и т.п. не место в интернете
  svc?: boolean // служебный порт (Kubernetes, агент, экспортер): снаружи тоже должен быть закрыт
  // 80 и 443 при сайтах ноды за Cloudflare: only - напрямую закрыт, also - открыт и в обход него
  cf?: 'only' | 'also'
  // открыт проверке только потому, что ее адрес в белом списке файрвола ноды: для интернета закрыт
  allow?: boolean
}
export type Exposure = {
  ts: string
  via: string // через какую локацию смотрели ("панель" - напрямую)
  own?: boolean // смотрели с адреса самой панели: открытое может быть открыто только для нее
  // по этим внешним адресам отвечает другой сервер: версия SSH снаружи не та, что у ноды
  foreign?: { ip: string; outside: string; node: string; nat?: boolean }[] // nat - адрес исходящего NAT, там сам шлюз
  ips: string[]
  ports: ExposedPort[]
  new?: ExposedPort[] // открылись с прошлых проверок
  new_ts?: string
  muted?: string[] // порты, отмеченные как открытые намеренно
  via_ips?: string[] // с каких адресов шла проверка (адреса прокси), с 1.4.263
  // сайты ноды, чьи домены смотрят в Cloudflare: первые 10, всего n из проверенных checked
  cf?: { sites: string[]; n: number; checked: number; total: number }
}

// Одно изменение на сервере: kind key/user/sudo/cron/unit, act add/del/mod; alert - изменение
// доступа, о нем приходит алерт
export type ServerChange = { ts: string; kind: string; act: string; text: string; alert: boolean }
export type ServerChanges = {
  log: ServerChange[]
  ack_ts?: string | null
  snap_ts?: number
  keys: number
  users: number
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
  cpu_groups?: CpuGroup[] // кто ест CPU группами с владельцем (агент 2.21+)
  disk_writers?: DiskWriter[] // кто пишет на диск за последний час (агент 2.33+)
  disk_writers_v1?: boolean // cgroup v1: отложенная запись в счетчики не попадает
  net_users?: NetUser[] // кто качает трафик за последний час (агент 2.34+)
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
  kube_expiry?: KubeExpiry[] // сроки PKI/kubeconfig/токенов Flux (хелпер kubeexpiry-setup)
  flux?: FluxState[] | null // состояния Ready ресурсов Flux; null = данных нет
  caps?: Record<string, boolean> // возможности агента: kmsg, proc_full (полный ли /proc)
  extras?: {
    'web-rate'?: WebRate; 'disk-usage'?: DiskUsage; 'disk-health'?: DiskHealth; units?: UnitsBlock
    'backup-runs'?: BackupRuns; 'backup-server'?: BackupServerExtra
  } & Record<
    string,
    unknown
  > // блоки хелперов как есть
  clock?: ClockInfo // статус синхронизации времени (timedatectl)
  clock_skew_sec?: number // сдвиг часов ноды относительно панели, сек (± ; считает бэкенд)
  clock_unix?: number // локальные часы ноды на момент отправки отчёта
  // слушающие сокеты кроме localhost (агент 2.26): по ним панель проверяет, что открыто снаружи
  listen?: { p: string; ip: string; port: number; u?: string }[]
  // давление на ресурсы (агент 2.26): PSI в % времени - mem_some_60, io_full_10 и т.п.;
  // процессы в D-state; открытые файлы против fs.file-max
  psi?: Record<string, number>
  // stuck (агент 2.40) - зависшие, как их видит ядро (hung_task): нет поля - агент старый и не
  // считает; hung - hung_task_detect_count ядра, hung_new - его рост за 10 минут
  dstate?: {
    n: number
    top?: { c: string; n: number }[]
    stuck?: { pid: number; comm: string; wchan?: string; sec: number }[]
    hung?: number
    hung_new?: number
  }
  fds?: { used: number; max: number }
  // TCP за секунду (агент 2.26): отправлено сегментов, из них повторно, переполнения очереди
  // accept и выброшенные из нее, новые соединения, выброшенные conntrack, входящие с ошибкой
  // syn_retrans - повторы SYN (подключения без ответа), агент 2.30+: в потери не входят
  // peers - к кому уходят повторы, когда их много (агент 2.32), peers_total - прирост повторов по всем соединениям
  tcp?: {
    out: number; retrans: number; in_errs: number; listen_ovf: number; listen_drop: number; ct_drop: number; syn_retrans?: number
    peers?: { ip: string; retrans: number; socks?: number; proc?: string; port?: number }[]
    peers_total?: number
  }
  // пакеты (агент 2.28): сколько и каким менеджером; сам список - getServerPackages
  pkgs?: { mgr?: string; n?: number; h?: string; log?: boolean }
  // обновления ОС (агент 2.26, раз в час): updates/security -1 - не apt
  os_upd?: {
    reboot?: boolean
    reboot_ts?: number
    reboot_pkgs?: string[]
    kernel?: string
    kernel_new?: string
    updates: number
    security: number
    upd_ts?: number
    apt_ts?: number
    unatt?: 'on' | 'off' | 'none'
    unatt_ts?: number
    ts: number
  }
  dns?: DnsInfo | null // DNS ноды: резолверы и время их ответа (агент 2.25)
}
// Итоги прогонов бэкапа (helper backup-setup 0.32): сколько restic добавил в репозиторий.
// add - новые данные до сжатия, st - сколько легло в репозиторий, proc - сколько прочитано
export type BackupRun = {
  ts: number; add: number; st: number; proc: number; files: number; fnew: number; fchg: number; dur: number
}
export type BackupRuns = { v: number; ts: number; unit?: string; runs: BackupRun[] }
// Разбор места (helper diskusage-setup): есть, пока раздел заполняется (от 75%). Helper
// ничего не удаляет, у находок - команда для человека.
export type DiskUsageDir = { path: string; bytes: number }
export type DiskUsageFs = {
  mount: string
  size: number
  used: number
  avail: number
  pct: number
  inodes: number
  top_state: 'ok' | 'partial' | 'skipped' | 'none' // none - раздел в разборе только из-за inode
  top_ts: number
  top: DiskUsageDir[] // самые большие каталоги до глубины 3, от 1% раздела
  // inode (helper 0.5+): раздел попадает в разбор и когда кончаются они
  ipct?: number
  iused?: number
  itotal?: number
  itop_state?: '' | 'ok' | 'partial' | 'failed'
  itop_ts?: number
  itop?: { path: string; files: number }[] // где больше всего файлов, глубина 3
}
export type DiskUsageItem = {
  id: string
  bytes: number
  free: number // сколько освободит команда (у журнала - сверх оставляемых 200 МБ)
  mount: string
  path: string
  level: 'safe' | 'careful' | 'manual'
  fix: string // команда без sudo; пусто - автоматом не решается
  count?: number
  name?: string // имя контейнера у его лога
  procs?: { pid: number; comm: string; bytes: number }[] // кто держит удаленные файлы
  kind?: string // у каталогов во временных: restic / dump / archive / other
  fresh?: boolean // в каталоге есть и свежие файлы - им пользуются
}
export type DiskUsage = {
  v: number
  ts: number
  fs: DiskUsageFs[]
  items: DiskUsageItem[]
  fix?: { v: number; allow: string[] } // helper 0.3+: какие действия "Освободить" нода разрешает
}
// Здоровье дисков (helper diskhealth-setup, раз в 2 минуты): программный RAID, SMART,
// пропавшие диски и ошибки ввода-вывода в журнале ядра. Диски и SMART - только на железе.
export type DiskHealthRaid = {
  dev: string
  state: string // active / inactive
  level: string // raid1 / raid5 / ...
  total: number // дисков по схеме массива (0 у raid0/linear)
  active: number
  map: string // [UU] / [U_]
  members: string // рабочие разделы через пробел
  failed: string // сбойные (F)
  spare: string // запасные (S)
  sync: string // recovery=45.2% / resync=... / check=...
}
export type DiskHealthDisk = {
  dev: string
  model: string
  serial: string
  type: string // nvme / sata / sas
  size: number | null
  rota: number | null // 1 - вращающийся
  ok: boolean // false - диск не отвечает или ядро его отключило
  err: string // dead / no_answer / no_namespace_* / state_* / nosmart
  health: string // PASSED / OK / FAILED!
  failing: boolean
  temp: number | null
  wear: number | null // износ, %
  spare: number | null // NVMe: запас, %
  spare_thr: number | null
  media: number | null // NVMe: ошибки носителя
  crit: string // NVMe Critical Warning, 0x00 - норма
  poh: number | null // наработка, часов
  realloc: number | null
  pending: number | null
  uncorr: number | null
  crc: number | null
  grow: Partial<Record<'realloc' | 'pending' | 'uncorr' | 'media' | 'crc', number>> // прирост за сутки
}
export type DiskHealth = {
  v: number
  ts: number
  virt: string // none - железо
  container: boolean
  smart: boolean // smartctl есть
  raid: DiskHealthRaid[]
  disks: DiskHealthDisk[]
  missing: { serial: string; model: string; dev: string; last: number }[]
  io: { count: number; last: string[] }
}
// Упавшие юниты systemd (helper units-setup, раз в минуту): почему упал и последние
// строки лога. fix - на ноде есть спул для кнопок "Перезапустить"/"Сбросить".
export type FailedUnit = {
  unit: string
  desc: string
  type: string // simple / oneshot / forking / notify; у не-сервисов пусто
  result: string // exit-code / signal / timeout / core-dump / start-limit-hit / oom-kill ...
  status: number | null // код выхода или номер сигнала
  since: number // когда упал (unix, часы ноды), 0 - неизвестно
  restarts: number | null
  log: string[]
}
export type UnitsBlock = { v: number; ts: number; units: FailedUnit[]; fix: boolean }
// DNS ноды (агент 2.25): каждый резолвер напрямую раз в минуту, резолв имени не из кэша раз в 5 минут
export type DnsInfo = {
  mode: 'direct' | 'resolved' | 'local' | string // resolved - за заглушкой systemd-resolved, local - свой unbound/dnsmasq
  servers: { addr: string; ms: number; err?: string }[] // ms -1 - ответа нет
  miss_ms: number // системный резолв имени не из кэша, -1 - не вышло
  miss_err?: string
  miss_ts?: number
  ts: number
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
  // epoch mtime лока: самого старого висячего, если такой есть (helper 0.23), иначе свежего,
  // который живой бэкап освежает раз в 5 мин
  lock_ts?: number
  keep_last?: number
  keep_daily?: number
  keep_weekly?: number
  keep_monthly?: number
}
// Кто чистит репозитории бэкап-сервера (helper backupserver-setup 0.23, report.d/backup-server.json)
export type BackupServerLegacy = {
  script: string // старый общий скрипт чистки (/etc/systemd-rest.conf)
  repos: number // сколько репозиториев в нем описано
  where: string // где запланирован: "crontab root", файл cron.d, юнит systemd; пусто - нигде
  schedule: string // расписание cron или OnCalendar
  log: string // куда пишет вывод
  log_ts: number // mtime лога: конец последнего прогона
  missing?: string[] // блоки на удаленные репозитории: скрипт каждый день шлет по ним ошибки (helper 0.24)
}
export type BackupServerRepoExtra = {
  cleaner: '' | 'script' | 'legacy' // свой prune-скрипт, старый общий скрипт, никто
  removed_ts: number // когда из репозитория последний раз пропадали снапшоты (0 - не видели)
  seen_since: number // с какого момента helper за ним следит
  check_ts?: number // недельная проверка целостности нашего prune-скрипта (helper 0.25)
  check_ok?: number // 1 прошла, 0 нашла ошибки, -1 еще не было
  check_part?: number // какую часть данных прочитала проверка: часть check_part из check_parts (0 - не читала)
  check_parts?: number
  recent?: number[] // времена последних снапшотов (helper 0.28), по ним бэкенд считает ритм
  groups?: { ts: number; groups: BackupGroup[] } | null // группы снапшотов, если их больше одной
  prune_err?: string // ошибка последнего неудачного прогона чистки (helper 0.30)
  prune_off?: number // 1 - чистку выключили: строка cron.d закомментирована (helper 0.34)
  prune_off_ts?: number // с какого времени (mtime cron.d)
  prune_off_why?: string // почему, из шапки # kervax-prune-off
  prune_hand_off?: number // 1 - чистка идет, а в crontab root ее строка закомментирована руками
}
// группа снапшотов так, как её видит forget: хост и теги (prune-скрипт, раз в сутки)
// Шаг "как исправить" (рисует FixSteps): on - дата к тексту шага, alt - другой путь вместо шагов выше,
// undo - откат (под чертой "откат", выполнять не по порядку)
export type OsFixStep = { id: string; cmd?: string; arg?: string; on?: string; alt?: boolean; undo?: boolean }

export type BackupGroup = {
  host: string
  tags: string[]
  paths: string[]
  n: number
  first: number
  last: number
}
export type BackupArchiveEntry = { note: string; ts: string; by: string }
// Вкладка "Чистка" сервера бэкапов (бэкенд app/bsrv_cleanup.py): что не так с чисткой и у каждого
// пункта шаги fix - команды под этот сервер и репозитории, тексты во фронте по id (BsrvCleanup.tsx)
export type BsrvCleanupLegacy = {
  script: string
  where: string // где запланирован старый скрипт; пусто - нигде, репозитории на нем никто не чистит
  schedule: string
  log_ts: number
  scheduled: boolean
  repos: { name: string; why: string[]; snaps: number }[] // живые, не в архиве; why - почему старье не уходит
  archived: number // сколько еще на нем репозиториев из архива
  fix: OsFixStep[] // перевести на чистку панели (adopt-legacy)
}
export type BsrvCleanupRot = {
  repo: string
  reasons: string[] // все причины одного репозитория
  // отчего: старый скрипт (запущен / не запущен), старая группа, своя чистка ничего не удаляет,
  // чистки нет, helper старый
  cause: 'legacy' | 'legacy_off' | 'old_group' | 'script' | 'none' | 'unknown'
  hosts: string[] // у old_group: хосты старых групп
  fix: OsFixStep[]
}
export type BsrvCleanup = {
  legacy: BsrvCleanupLegacy | null
  dead: { script: string; scheduled: boolean; repos: string[]; fix: OsFixStep[] } | null // блоки на удаленные репозитории
  rotation: BsrvCleanupRot[]
  prune: { repo: string; kind: string; why: string; fix: OsFixStep[] }[] // своя чистка не работает
  groups: Record<string, Record<string, { forget?: OsFixStep[]; merge?: OsFixStep[] }>> // repo -> старая группа -> шаги
  // чистку выключили руками (helper 0.34): live - бэкапы при этом идут, since - с какого времени,
  // why - причина из шапки cron.d. Решение, а не поломка: в ротацию и алерты не идет
  off?: BsrvCleanupOff[]
  // чистка идет, хотя ее строку в crontab root закомментировали руками (включил helper до 0.34)
  hand?: { repo: string; fix: OsFixStep[] }[]
}
export type BsrvCleanupOff = {
  repo: string
  live: boolean
  since: number
  why: string
  last: number // последний бэкап
  snaps: number
  fix: OsFixStep[] // как включить
}
export type BackupServerExtra = {
  v: number
  ts: number
  restic?: string // самый старый restic из тех, что сервер запускает сам (helper 0.27)
  restic_target?: string // до какой версии его обновляет helper
  legacy: BackupServerLegacy | null
  repos: Record<string, BackupServerRepoExtra>
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
export interface DiskWear {
  dev: string
  serial: string
  wear: number
  rate: number
  src: 'measured' | 'lifetime' | ''
  d90: string | null
  d100: string | null
  soon: boolean
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
  // внешние прокси (caddy-docker-proxy, traefik) с docker-сокетом: взлом прокси - root на хосте
  docker_exposed?: { name: string; image: string; kind: string; fix?: SockFix }[]
  // контейнеры с ключами от хоста сверх положенного по назначению: privileged, опасные
  // capabilities, docker-сокет, корень хоста на запись; muted - заглушен точечно
  docker_risk?: DockerRisk[]
  // что открыто в интернет: внешняя проверка портов раз в 6 часов
  exposure?: Exposure | null
  // что сделать с ОС: конец поддержки, перезагрузка, обновления безопасности (level 2 - уже дыра)
  // key - ключ решения "так и должно быть" в alert_mutes (osupd:<kind>), muted - решение принято
  // fix - шаги на ноде, после которых пункт уходит: id текста, команда, arg для текста, alt - другой путь
  os_advice?: { kind: string; level: number; text: string; key: string; muted?: boolean; fix?: OsFixStep[] }[]
  // версии без поддержки (app/software.py): строка на продукт, 2 - без поддержки, 1 - скоро
  sw_advice?: { key: string; level: number; name: string; what: string; text: string }[] | null
  // GPU NVIDIA (helper gpu-setup): карточка раздела и плашка списка; LLM-сервисы (агент 2.36)
  gpu?: GpuView | null
  gpu_sum?: GpuSum | null
  llm?: LlmSvc[] | null
  // очереди, которые растут и не разбираются: {ключ очереди: {age, d0, muted}} (queue_watch.view)
  queue_watch?: Record<string, { age: number; d0: number; muted: boolean; stall?: boolean; wait?: number }> | null
  // очереди в Redis (helper queues-setup) и экземпляры Redis без известного пароля
  queue_brokers?: ServiceInfo[] | null
  queue_skipped?: string[] | null
  // базы, куда helper очередей не вошел (auth), и таблицы очередей больше миллиона строк (big)
  queue_db_skipped?: { why: string; where: string; rows?: number }[] | null
  // заглушенный всплеск упавших задач: {источник брокера: {perm} | {until}} (ключ мьюта qfail:<источник>)
  queue_fail_mutes?: Record<string, { perm?: boolean; until?: string }> | null
  // воркеры под supervisor и pm2 (helper workers-setup): демоны с программами
  workers?: WorkerDaemon[] | null
  // демоны, которых helper не смог спросить: где и почему словами
  workers_skipped?: { where: string; why: string }[] | null
  // кроны хоста и контейнеров (helper cron-setup): задачи, идущие копии, беда и мьют
  cron?: CronInfo | null
  // ошибки в логах контейнеров и подов (helper logerr-setup): счет за час и сутки, обычный час, всплеск
  log_errors?: LogErrInfo | null
  // пулы PHP-FPM (helper fpm-setup): воркеры, очередь, упоры в pm.max_children, медленные и убитые запросы
  fpm?: FpmInfo | null
  // заглушенные очереди: насовсем или до срока
  queue_mutes?: Record<string, { perm?: boolean; until?: string }> | null
  // изменения на сервере (helper changes-setup): лента между снимками, отметка "это мы"
  changes?: ServerChanges | null
  // недельная проверка восстановления бэкапа (helper backup-setup 0.34)
  backup_verify?: { ts: string | null; files_ok: number; dumps_ok: string[]; level: number; problems: string[] } | null
  // перезапусков за сутки: "docker:<имя>" | "pod:<ns>/<имя>" -> n (5 и больше - предупреждение)
  restarts_day?: Record<string, number> | null
  // что течет памятью: дно (минимум за сутки) растет 4 суток подряд; series - эти минимумы, байты
  mem_leaks?: MemLeak[] | null
  // SSH (helper changes-setup 0.3): как пускает соединение из интернета; open - порт открыт снаружи
  // по внешней проверке (null - не знаем)
  ssh?: {
    ports: number[]; password: boolean; root: string; root_password: boolean; guard: string
    fail: number; accepted_password: number; open: boolean | null
    fail_top?: { ip: string; n: number }[] // откуда перебор, больше всех сверху
  } | null
  // вход по паролю на открытом SSH: key - решение "вход по паролю нужен" (muted), fix - как выключить
  ssh_advice?: { kind: string; level: number; text: string; key?: string; muted?: boolean; fix?: OsFixStep[] }[] | null
  // не переживет перезагрузку: контейнеры без restart policy, юниты без enable
  noboot?: NoBoot | null
  noboot_advice?: { docker: string[]; units: string[] } | null
  // учетки с shell: пароль, ключи, sudo без пароля, последний вход (helper changes-setup 0.5)
  access?: { accounts: AccessAccount[]; seen_since: number } | null
  access_advice?: { stale: string[]; weak: string[] } | null
  // здоровье баз (helper dbstat-setup 0.5): роль, отставание реплики (сек), самая долгая
  // транзакция (сек), возраст XID, найденные проблемы
  db_health?: DbHealth[] | null
  // проверка конфигов веб-серверов (helper webserver-setup 0.23): where - "host" или контейнер
  web_config?: { server: string; where: string; ok: boolean; err: string }[] | null
  // сертификаты nginx (helper webserver-setup 0.24): сколько дней до конца срока
  web_certs?: { where: string; file: string; label: string; days: number }[] | null
  // внешние прокси со сборкой образа старше года (built - unix-секунды); key - решение "оставить как
  // есть" в alert_mutes (proxyold:<контейнер>), muted - решение принято, fix - как обновить
  proxy_outdated?: {
    name: string; image: string; kind: string; version: string; built: number; age_days: number
    key: string; muted?: boolean; fix?: ProxyUpgrade
  }[]
  // нода умеет разобрать раздел по запросу (агент 2.24 + diskusage-setup 0.6)
  disk_analyze?: boolean
  // только что поставлена: установщик еще ставит helper'ы и прокси Docker
  fresh?: boolean
  agent_ip: string
  last_report: ServerReport | null
  last_seen: string | null
  snooze_until: string | null
  alert_snoozes: Record<string, string> | null // {тип: ISO-до} — точечный снуз
  // заметка о сервере: текст, кто и когда писал
  note?: string
  note_by?: string
  note_at?: string | null
  known_issues?: Record<string, KnownIssue> | null // известные проблемы по видам алертов
  // подключение нового сервера (backend app/onboard.py): идет - до когда и куда пойдут алерты
  // (имена чатов, tg/main/chats только в списке серверов), закрыто - null
  onboard?: { until: string; tg?: boolean; main?: boolean; chats?: string[] } | null
  onboard_done_at?: string | null
  // недоступен: что ответил сервер панели в момент алерта (backend app/liveness.py)
  offline_probe?: { state: 'alive' | 'refused' | 'silent' | 'blind'; where?: string; tried?: string[]; was?: string; ts?: string } | null
  // прогноз износа SSD (backend app/disk_wear.py): d90/d100 - даты ISO, src - замер или средняя за жизнь
  wear_forecast?: DiskWear[]
  onboard_by?: string
  first_report_at?: string | null
  online: boolean
  cpu_alert_percent: number
  mem_alert_percent: number
  disk_alert_percent: number
  disk_warn_percent: number
  disk_crit_percent: number
  disk_autofix?: boolean // авто-очистка безопасного при пороге предупреждения (включает админ)
  disk_forecast?: DiskForecast | null
  // что сломано по новым проверкам (диски, прогноз, inode, упавшие юниты) - считает бэкенд
  // с теми же уровнями, что у алертов; level 2-3 - проблема, 1 - предупреждение
  problems?: ServerProblem[]
  kube_cron_issues?: Record<string, string> // CronJob с проблемой: {ns/имя: причина}, считает бэкенд
  temp_alert_c: number
  conntrack_alert_percent: number
  db_conn_alert_percent: number
  web_5xx_alert_percent: number // доля 5xx за 15 минут, % (0 = выкл)
  // пороги сроков кластера и Flux в днях: по сообщению на каждый, [] - выключено
  kube_expiry_warn_days: number[] | null
  disk_temp_alert_c: number
  alert_mutes: string[] | null
  backup_repo_mutes: string[] | null // заглушённые репо бэкап-сервера (по имени)
  // архив бэкап-сервера: репозиторий или "репозиторий|хост" (старая группа) -> кто, когда, зачем
  backup_repo_archive?: Record<string, BackupArchiveEntry> | null
  backup_audit_mutes?: string[] | null // приглушённые находки покрытия (по ключу)
  backup_not_required: boolean // на сервере бэкап не требуется (алерт снят)
  db_dumps_ok: boolean // дампы СУБД настроены отдельно (пункт снят с главной)
  backup_deadline_hour?: number // до какого часа бэкап должен закончиться (окно)
  backup_anytime?: boolean // бэкап в любое время — не уведомлять о выходе за окно
  offline_after_seconds: number
  alert_sustain_seconds: number // сколько держать превышение до алерта, сек
  parent_id?: number | null // за каким сервером стоит (шлюз, VPN): пока тот молчит, про этот не пишем
  // лимит трафика тарифа (app/traffic.py): ГБ (0 - нет), что считает хостер, день начала периода
  traffic_limit_gb?: number
  traffic_count?: 'out' | 'both'
  traffic_cycle_day?: number
  agent_update_note?: string | null // «обновление докачивается: 59% (3.4 из 5.8 МБ)»
  agent_update_error?: string | null // агент отверг обновление: причина его словами
  agent_advice: string[] // чего агенту не хватает в systemd-юните (человекочитаемо)
  agent_fix_command: string | null // команда-фикс для ноды (drop-in) или null
  helper_advice: HelperAdvice[] // устаревшие setup-скрипты (helper'ы) на ноде → переустановить
  // прогон kervax_helpers.yml ноде что-то поменяет (устарел helper или нет watchdog);
  // тот же признак панель отдает ansible, поэтому считается на бэкенде
  helper_rollout?: boolean
  backup_audit?: BackupAudit[] // аудит покрытия бэкапа: что рискует не восстановиться
  custom_backups?: CustomBackup[] // свои бэкапы ноды (настроены без панели) со статусом
  custom_backup_ignored?: string[] | null
  kube_node_ignored?: string[] | null // ноды кластера, где агент не нужен (по имени)
  kube_unmonitored?: KubeNodeRef[] // ноды кластера без агента панели (считает бэкенд)
  bsrv_unmonitored?: string[] // репозитории бэкап-сервера, чьих клиентов нет в панели (считает бэкенд)
  bsrv_rotation?: string[] // что сейчас сказал бы алерт "ротация встала" (считает бэкенд)
  bsrv_cleanup?: BsrvCleanup | null // вкладка "Чистка": пункты с причинами и шагами (считает бэкенд)
  bsrv_restic_old?: string // restic сервера бэкапов старше целевой версии (считает бэкенд)
  bsrv_repo_reasons?: Record<string, string> // репозиторий -> почему он сейчас в алерте backup_repo
  bsrv_cadence?: Record<string, number> // обычный интервал бэкапов репозитория, секунды
  bsrv_prune?: Record<string, string> // репозиторий -> почему не работает его чистка
  alert_since?: Record<string, string> // с какого момента видна проблема каждого вида (ISO)
  docker_since?: Record<string, string> // с какого момента лежит каждый упавший контейнер
  // последний прогон бэкапа добавил в разы больше обычного (считает бэкенд)
  backup_growth?: { level: number; text: string; st: number; usual: number; ts: number } | null
  pod_names?: Record<string, string> // начало uid пода -> ns/имя (для "pod <uid8>" в cpu_groups)
}

// Свой бэкап ноды: cron-задание, systemd-таймер или скрипт с метриками, настроенный без
// панели. Находит helper на ноде, статус считает панель; сама нода при этом не меняется.
// ran — cron задание запускает (видно по журналу), но итога не видно
export type CustomBackupStatus =
  | 'ok' | 'failed' | 'stale' | 'disabled' | 'running' | 'ran' | 'unknown' | 'ignored'
export type CustomBackup = {
  id: string
  kind: 'cron' | 'systemd' | 'metrics'
  name: string
  desc: string
  schedule: string
  engines: string[]
  files: boolean // файловый бэкап ноды (restic/borg/rsync…), а не дамп одной базы
  containers: string[]
  status: CustomBackupStatus
  problem: string
  ok_ts: number // последний успех; у задания без статуса — самый свежий файл бэкапа
  run_ts: number
  next_ts: number
  size_bytes: number
  duration_sec: number
  dir: string
  log: string
  script: string
  unit: string
  result: string
  metrics: string
  run_src: string // journal — старт из журнала cron, log — запись лога, state — отметка скрипта
  state: string // файл-отметка, куда скрипт пишет итог
  evidence: string // чем подтверждён итог: state | restic | files
  fail: string // state | state_old | restic
  dbs: { name: string; ok: number; ts: number; size_bytes: number }[]
  stale_after: number
  ignored: boolean
  scan_stale: boolean // helper давно не обновлял сведения — статус неизвестен
}
export type QueueStat = {
  name: string
  vhost?: string
  ready: number
  unacked?: number
  consumers?: number // потребителей у очереди (агент 2.38)
  oldest?: number // когда стала готовой самая старая задача, unix-секунды (queues-setup 0.4)
}
// Упавшие задачи брокера (queues-setup 0.4): за час и сутки, span - за какое окно известно (у Laravel и
// Symfony счет по id, история копится с установки), top - чаще падали (среди последней сотни), err -
// первая строка последней ошибки; base - обычный час, spike - всплеск сейчас (queue_watch.helper_view)
export type QueueFailed = {
  h1: number
  d1: number
  span: number
  top: { q: string; job: string; n: number }[]
  err?: string
  err_q?: string
  err_job?: string
  base?: number | null
  spike?: boolean
}
// Программа под supervisor или pm2: состояние как у демона (RUNNING, FATAL, BACKOFF, EXITED, STOPPED;
// online, errored, stopped), up - секунд работает, r15/r60 - перезапусков за 15 минут и за час,
// level/why - беда (workers.trouble на бэкенде), mute - заглушена по ключу worker:<key>
export type WorkerProg = {
  name: string
  state: string
  up: number
  since: number
  r15: number
  r60: number
  // плановых перезапусков за час: вышел с кодом 0, проработав минуту и больше (helper 0.3)
  p60?: number
  exit: number | null
  key: string
  // ключ мьюта: у копии программы (numprocs, кластер pm2) - программа целиком
  mkey?: string
  log?: string
  level?: number
  why?: string
  mute?: { perm?: boolean; until?: string }
}
export type WorkerDaemon = { kind: string; label: string; where: string; source: string; ts: number; progs: WorkerProg[] }
// Задача cron (cron_watch.view): где (словами), расписание, последний запуск (unix), запусков за сутки
// (-1 - журнала нет), n - идет копий сейчас, run - самые старые (возраст и CPU в секундах), usual -
// обычная длительность (секунды, 1 - меньше минуты, 0 - неизвестна), lost - вывод доходит до cron, а
// почты нет, order - stderr ушел в cron из-за 2>&1 перед перенаправлением в файл; level/why - беда,
// mute - заглушена по ключу cron:<key>
export type CronJob = {
  where: string
  kind: string
  user: string
  sched: string
  src: string
  cmd: string
  last: number
  runs: number
  n: number
  run: { pid: number; age: number; cpu: number; procs: number }[]
  usual: number
  key: string
  lost?: 'err' | 'out' | 'both'
  order?: boolean
  level?: number
  why?: string
  mute?: { perm?: boolean; until?: string }
}
// Лог контейнера или пода с ошибками за сутки (log_errors.view): h1/d1 - строк с ошибками за час и
// сутки, lines1 - всех строк за час, base - обычный час (null - истории меньше 4 часов), last -
// последняя строка с ошибкой; level/why - всплеск, mute - заглушен по ключу logerr:<key>
export type LogErrSource = {
  key: string
  label: string
  h1: number
  d1: number
  lines1: number
  base: number | null
  last: string
  last_ts: number
  level?: number
  why?: string
  mute?: { perm?: boolean; until?: string }
}
// Пул PHP-FPM (fpm_pools.view): конфиг (listen, pm, max = pm.max_children, slowlog/terminate в
// секундах), сейчас - воркеров, соединений (обслуживаемые и ждущие), в очереди; sat - минут за час,
// когда запросы ждали воркера; reached/slow/tout - упоры в предел, медленные и убитые по таймауту
// за час, *_d - за сутки, *_last - последний пример; level/why - беда, mute - заглушен fpm:<key>
export type FpmPool = {
  key: string
  label: string
  pool: string
  listen: string
  pm: string
  max: number
  children: number
  conns: number
  queue: number
  sat: number
  span: number
  reached: number
  reached_d: number
  slow: number
  slow_d: number
  tout: number
  tout_d: number
  slowlog: number
  terminate: number
  status: number
  slow_last: string
  slow_ts: number
  tout_last: string
  tout_ts: number
  level?: number
  why?: string
  mute?: { perm?: boolean; until?: string }
}
export type FpmInfo = { ts: number; pools: FpmPool[]; skipped: { where: string; why: string }[] }
export type LogErrInfo = { ts: number; logs: number; skipped: number; sources: LogErrSource[] }
export type CronInfo = {
  ts: number
  journal: boolean
  disc24: number // сколько раз за сутки cron хоста выбросил вывод (нет почты)
  daemons: { where: string; kind: string; mta: boolean; jobs: number }[]
  jobs: CronJob[]
}
export type ServiceInfo = {
  kind: string // rabbitmq | celery | sidekiq | rq | bullmq | laravel | yii | symfony | oban ... (queue_watch.SYSTEM_LABEL)
  source?: string // «под ns/name» / «контейнер X»; у очередей в Redis - «контейнер X, Celery, db 1» (часть ключа)
  queues?: QueueStat[] // прислана только верхушка по глубине
  total?: number // всего очередей у инстанса
  label?: string // Celery, Sidekiq... (очереди от helper'а queues-setup)
  engine?: string // redis | pg | mysql
  where?: string // подпись в карточке: «Redis, контейнер X, db 1», «PostgreSQL, на хосте, база app»
  failed?: QueueFailed // упавшие задачи; у laravel_failed (таблица failed_jobs) очередей нет, только они
}
export type DBStat = {
  engine: string // pg | mysql | clickhouse | redis
  container?: string // пусто = движок на хосте, не в docker
  version?: string
  dbs?: { name: string; size: number }[] // у redis size = число ключей
  users?: string[] // логины (имена, без паролей — их панель не видит принципиально)
  // Слоты подключений: занято и предел. У postgres предел — уже за вычетом
  // зарезервированных суперпользователю, то есть тот, в который упрётся приложение.
  // 0 = движок их не отдал (старый helper) — тогда ничего не показываем.
  conn_used?: number
  conn_max?: number
}

// Датируемая сущность кластера: сертификат, kubeconfig или токен Flux. Собирает
// root-хелпер kubeexpiry-setup на самой ноде; сюда приезжает только дата.
export type KubeExpiry = {
  kind: string // cluster-cert | kubeconfig | kubelet-cert | flux-token | secret-cert
  where: string // путь к файлу либо namespace/name
  expires: number // unix-время истечения
  note?: string // имя сертификата или поле секрета
}

// Состояние ресурса Flux. Нужно к тому же классу отказов с другой стороны: срок
// можно проспать, а токен — отозвать, и тогда доставка встаёт без предупреждения.
export type FluxState = {
  kind: string // GitRepository | Kustomization | HelmRelease | …
  where: string // namespace/name
  ready: boolean
  reason?: string
  message?: string
}

// Запросы в минуту по access-логам: считает helper webserver-setup, агент отдаёт
// блок как есть в extras. Наплыв редиректов канал почти не шевелит, а тут виден сразу.
export type WebRate = {
  ts: number
  rpm: number
  e5?: number // ответов 5xx в минуту по всем логам
  logs?: {
    log: string
    name?: string // ns/под для логов kubernetes: «0.log» в панели ни о чём не говорит
    rpm: number
    e5?: number
    e4?: number
    un?: number // строк без распознанного кода ответа: отличает «ошибок ноль» от «не нашли код»
    c5?: Record<string, number> // ответы 5xx по кодам за минуту (хелпер 0.12+)
    sites?: string[]
  }[]
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
  note?: string // одна короткая строка для карточки: без «где» и общих слов
  gap: boolean // true = данных нет в бэкапе; false = есть, но восстановимость под вопросом
  dump_engine?: string // pg/mysql/ch — код движка; пусто = дампить не умеем
  can_dump?: boolean // true = панель снимет дамп сама (docker, локально, из пода); false = причина в detail
  downtime?: string // непусто = включение дампа стоит простоя (Neo4j Community)
  container?: string // имя контейнера или k8s.<ns>.<sts|deploy|ds>.<имя> ("" = нативная установка)
  pods?: string[] // ns/name подов этого экземпляра — где живёт база
  key?: string // "db:RabbitMQ:cont" — устойчивый ключ находки (для точечного приглушения)
  instance?: string // экземпляр: контейнер или контроллер пода k8s.… ("" = нативная установка)
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
  disks: { mount: string; pct: number; ipct?: number }[] | null
  web_rpm: number | null // запросов в минуту по access-логам веб-сервера
  web_5xx: number | null // из них ответов 5xx в минуту
  // разбивка для стека: самые нагруженные логи минуты и ответы 5xx по кодам (с 1.4.62)
  web_top?: { k: string; r: number; e: number }[] | null
  web_codes?: { c: string; n: number }[] | null
  // GPU по картам (загрузка, память и питание в %, температура) и LLM-сервисы (задержки - p95, с)
  gpu?: { g: string; u: number | null; m: number | null; t: number | null; p: number | null }[] | null
  llm?: {
    k: string; run: number | null; wait: number | null; kv: number | null; gen: number | null; req: number | null
    ttft: number | null; tpot: number | null; e2e: number | null
  }[] | null
}

// GPU NVIDIA (app/gpu.py view): карты, процессы, ошибки Xid за сутки, драйвер и карты на шине PCI
export type GpuCardRow = {
  i: number | null; bus: string; name: string; util: number | null; mutil: number | null
  mem: number | null; memt: number | null; temp: number | null; pw: number | null; pwl: number | null
  clk: number | null; clkm: number | null; ps: string; fan: number | null; pm: string
  ecc_u: number | null; ecc_c: number | null; slow: number | null; maxop: number | null
  thermal: boolean; pcap: boolean; brake: boolean; remap: boolean; remap_fail: boolean
  frozen?: number // показания не меняются столько секунд (0 - живые)
}
export type GpuView = {
  helper: boolean
  age?: number
  stale?: boolean
  smi?: boolean
  ok?: boolean
  err?: string
  cause?: string
  driver?: string
  cuda?: string
  kernel?: string
  pci: { bus: string; drv: string; lost?: boolean }[]
  gpus?: GpuCardRow[]
  procs?: { gpu: number | null; pid: number | null; name: string; kind: string; owner: string; mem: number | null }[]
  xid?: { ts: number; bus: string; code: number; text: string; gpu: number | null; hw: boolean; held?: boolean; old?: boolean; desc: string }[]
}
// плашка списка серверов (gpu.summary): память в ГиБ
export type GpuSum = {
  n: number; models: string[]; util: number | null; mem: number | null; memt: number | null; helper: boolean; ok: boolean
  frozen?: number // сколько карт с застывшими показаниями
}
// LLM-сервис (gpu.llm_view): за последние 5 минут, задержки [p50, p95, p99] в секундах
export type LlmSvc = {
  key: string; label: string; engine: string; engine_name: string; kind: string; name: string; where: string
  model?: string | null; port: number; up?: number | null; ok: boolean; err: string
  no_metrics?: boolean | null; win?: number | null
  running?: number | null; waiting?: number | null; kv?: number | null; req_s?: number | null
  prompt_tps?: number | null; gen_tps?: number | null; preempt?: number | null; errors?: number | null
  aborted?: number | null; ttft?: number[] | null; tpot?: number[] | null; e2e?: number[] | null
  base_ttft?: number | null; base_tpot?: number | null
  // всплески задержек за сутки (llm_lat.recent), свежие первыми; req - [меньше, больше] запросов за 5 мин
  lat_recent?: { m: 'ttft' | 'tpot'; from: string; to: string; peak: number; at: string; base: number
    req: [number, number] | null; wait: number; load: boolean }[]
  locked?: boolean // /metrics просит ключ: так настроено, не авария
  pub_port?: number | null // проброшенный порт контейнера на хосте
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
  db_conn_alert_percent?: number
  web_5xx_alert_percent?: number
  kube_expiry_warn_days?: number[]
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
// не переживет перезагрузку (reboot_survive.view); muted - заглушен точечно ("по назначению")
export interface NoBoot {
  docker: { name: string; image: string; policy: string; muted: boolean }[]
  units: { unit: string; started: number; muted: boolean }[]
}

// учетка с shell (access_audit.view): pw - P задан, L заблокирован, NP пустой; last - последний
// вход по SSH, unix-секунды, 0 - не было; stale - доступ есть, а входов давно нет
export interface AccessAccount {
  name: string
  uid: number
  pw: string
  last: number
  keys: number
  weak: string[]
  nopasswd: boolean
  access: boolean
  stale: boolean
  muted: boolean
  fix?: OsFixStep[] // у заброшенной: как отключить
}

// контейнер с ключами от хоста: flags - priv, caps, sock, host (docker_risk.view)
export interface DockerRisk {
  name: string
  image: string
  flags: string[]
  caps: string[]
  text: string
  muted: boolean
  fix?: RiskFix // у незаглушенных: шаги, общие у контейнеров одного проекта
}
// как исправить (docker_risk.fixes): тексты шагов во фронте по id, args - подстановки
export type RiskStep = { id: string; cmd?: string; alt?: boolean; undo?: boolean }
export type RiskFix = { key: string; names: string[]; args: Record<string, string>; steps: RiskStep[] }

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
    disk_autofix?: boolean // авто-очистка диска (меняет только админ)
    parent_id?: number | null // за каким сервером стоит, null - ни за кем
    traffic_limit_gb?: number // лимит трафика тарифа, ГБ (0 - без лимита)
    traffic_count?: 'out' | 'both'
    traffic_cycle_day?: number
  },
): Promise<Server> {
  return api<Server>(`/api/servers/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
}
export function deleteServer(id: number): Promise<void> {
  return api<void>(`/api/servers/${id}`, { method: 'DELETE' })
}
// Изменения доступа сделали свои: строка уходит с главной
export function ackChanges(id: number): Promise<Server> {
  return api<Server>(`/api/servers/${id}/changes/ack`, { method: 'POST' })
}
// Проверить снаружи, что открыто в интернет, не дожидаясь планового прогона
export function scanExposure(id: number): Promise<Server> {
  return api<Server>(`/api/servers/${id}/exposure/scan`, { method: 'POST' })
}
// Быстрый снуз алертов сервера на N часов (0 = снять).
export function snoozeServer(id: number, hours: number): Promise<Server> {
  return api<Server>(`/api/servers/${id}/snooze`, {
    method: 'POST',
    body: JSON.stringify({ hours }),
  })
}
// Связь с другими серверами (агент 2.31): агент пробует открыть TCP-соединение с адресом
export type NetLink = {
  id: number
  target_server_id: number | null
  host: string
  port: number
  note: string
  name: string
  target_name: string | null
  loss: number | null // доля неудачных попыток за 5 минут, %
  rtt: number | null // время подключения, мс
  err: string
  fresh: boolean
  last_ts: string | null
  held: boolean // сервер, с которым связь, сам молчит
  // мини-график за час: по точке на 2 минуты [время подключения, худшая доля неудач] или null
  spark?: ([number | null, number] | null)[] | null
}
export type NetLinksOut = {
  links: NetLink[]
  targets: { id: number; name: string; ip: string; local_ip: string }[]
  max: number
  agent_ok: boolean
}
export type LinkPoint = { ts: string; loss: number; rtt: number | null; rtt_max: number | null }
export const getLinks = (id: number) => api<NetLinksOut>(`/api/servers/${id}/links`)
export const addLink = (
  id: number,
  body: { target_server_id: number | null; host: string; port: number; note: string },
) => api<{ id: number }>(`/api/servers/${id}/links`, { method: 'POST', body: JSON.stringify(body) })
export const deleteLink = (id: number, linkId: number) =>
  api<void>(`/api/servers/${id}/links/${linkId}`, { method: 'DELETE' })
export const getLinkHistory = (id: number, linkId: number, hours: number) =>
  api<{ points: LinkPoint[]; step: number }>(`/api/servers/${id}/links/${linkId}/history?hours=${hours}`)
// Трафик по настоящим сетевым картам: сутки, сводка за период тарифа, прошлые периоды
export type TrafficSum = {
  start: string
  end: string // не включая
  rx: number
  tx: number
  used: number
  count: 'out' | 'both'
  limit: number // байты, 0 - без лимита
  forecast: number | null
  days: number
  left: number
  full: boolean // данные с самого начала периода
}
export type TrafficOut = {
  days: { day: string; rx: number; tx: number }[]
  periods: { start: string; rx: number; tx: number }[]
  sum: TrafficSum | null
  limit_gb: number
  count: 'out' | 'both'
  cycle_day: number
  ifaces: string[]
  agent_ok: boolean
  since: string | null
}
export const getTraffic = (id: number) => api<TrafficOut>(`/api/servers/${id}/traffic`)
// версии ПО сервера и сроки поддержки (app/software.py)
export type SoftwareItem = {
  key: string
  p: string
  name: string
  cycle: string
  ver: string
  eol: string | boolean
  state: 'eol' | 'soon' | 'ok'
  left: number | null
  text: string
  // у контейнера еще образ и папка compose (пусто - агент ее не прислал или не из compose)
  where: { kind: 'container' | 'host'; name: string; src: string; ver: string; image?: string; dir?: string }[]
  // у кончившихся и скорых: ключ решения в alert_mutes и как исправить
  mkey?: string
  muted?: boolean
  fix?: SwFixBlock[]
}
// способ уйти с версии (software.fix): шаги для первого места, остальные места с тем же образом -
// others; args - подстановки в тексты шагов и шапку (продукт, from, to, контейнер, образ, папка
// compose dir, files, svc, compose: yes/no/unknown, пакеты)
export type SwFixBlock = {
  name: string
  kind: 'container' | 'host'
  mode: string
  others: string[]
  args: Record<string, string>
  steps: { id: string; cmd?: string; alt?: boolean; undo?: boolean }[]
}
export type SoftwareOut = {
  items: SoftwareItem[]
  unknown: { name: string; image: string; p: string; pname: string }[]
  old: { name: string; image: string; built: number; age_days: number }[]
  built: string | null
  source: string | null
  agent_ok: boolean
  pkgs: boolean
}
export const getSoftware = (id: number) => api<SoftwareOut>(`/api/servers/${id}/software`)
// кто качает трафик и кто пишет на диск по часам (app/usage.py): у net в паре [принято, отдано],
// у disk [записано, прочитано]; дни недели - в часовом поясе браузера
export type UsageName = {
  name: string
  kind: string
  d24: [number, number]
  week: [number, number]
  days: [number, number][]
}
export type UsageOut = { metric: 'net' | 'disk'; days: string[]; names: UsageName[]; since: string | null }
export const getUsage = (id: number, metric: 'net' | 'disk') =>
  api<UsageOut>(`/api/servers/${id}/usage?metric=${metric}&tz=${new Date().getTimezoneOffset()}`)
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
    // "Освободить" из разбора места; контейнер - только у лога контейнера
    | { action: 'disk_fix'; mode: 'preview' | 'run'; fix: string; container?: string }
    // "Разобрать" раздел ниже 75% (helper diskusage-setup 0.6)
    | { action: 'disk_fix'; mode: 'run'; fix: 'analyze'; mount: string }
    // упавший юнит systemd: перезапустить или сбросить отметку (helper units-setup)
    | { action: 'unit_fix'; mode: 'restart' | 'reset'; unit: string }
    // журнал юнита, всей системы (_all) или ядра (_kernel): helper units-setup 0.4, агент 2.29
    | { action: 'unit_journal'; unit: string; since_min: number; lines: number; prio: 'info' | 'warning' | 'err' }
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
// нода кластера без агента панели: public - адрес можно подставить в "IP сервера"
export type KubeNodeRef = { name: string; ip: string; public: boolean }

// "агент не нужен" для ноды кластера (чужая, временная) - или вернуть ее в подсказку
export function kubeNodeIgnore(id: number, node: string, ignored: boolean): Promise<Server> {
  return api<Server>(`/api/servers/${id}/kube/node-ignore`, {
    method: 'POST',
    body: JSON.stringify({ node, ignored }),
  })
}

export function backupAuditMute(id: number, key: string, muted: boolean): Promise<Server> {
  return api<Server>(`/api/servers/${id}/backup/audit-mute`, {
    method: 'POST',
    body: JSON.stringify({ key, muted }),
  })
}

// не отслеживать найденный на ноде бэкап («это не бэкап», «старый скрипт») — или вернуть
export function setCustomBackupIgnored(id: number, job: string, ignored: boolean): Promise<Server> {
  return api<Server>(`/api/servers/${id}/backup/custom-ignore`, {
    method: 'POST',
    body: JSON.stringify({ id: job, ignored }),
  })
}

// архив: репозиторий (host пустой) или одна старая группа его снапшотов
export function backupRepoArchive(id: number, repo: string, host: string, note: string, archived: boolean): Promise<Server> {
  return api<Server>(`/api/servers/${id}/backup/repo-archive`, {
    method: 'POST',
    body: JSON.stringify({ repo, host, note, archived }),
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
// Ошибки 5xx одного лога за окно: где, сколько, какие коды и пути. Сюда ведёт алерт.
export type WebErrorRow = {
  key: string // ключ лога: заглушить 5xx именно его - web_5xx:<key>
  log: string
  label: string // домены, под kubernetes или контейнер
  errors: number
  minutes: number
  peak: number // максимум ошибок в минуту
  first_ts: string
  last_ts: string
  codes: Record<string, number> // {"502": 30} - с хелпером 0.12
  paths: { p: string; n: number }[] // частые пути с ошибками - с хелпером 0.12
  hosts?: { h: string; n: number }[] // по доменам, если лог общий и в формате есть $host (helper 0.21)
  lines: string[] // последние строки с 5xx (хелпер 0.19), секреты замаскированы на ноде
  lines_n: number // сколько строк сохранено за окно
  // почему 502/504 (helper 0.26): до какого upstream nginx не достучался и как, за окно;
  // c - контейнер docker по адресу или upstream ingress-nginx
  ups?: { u: string; k: string; n: number; c?: string }[]
  gone?: boolean // лога больше нет в отчете: ошибки за окно - история (сайты разнесли по логам)
}
// Все строки с 5xx одного лога за окно - для .txt
export function serverWebErrorLines(id: number, key: string, hours = 24): Promise<string[]> {
  return api<string[]>(`/api/servers/${id}/web-errors/lines?key=${encodeURIComponent(key)}&hours=${hours}`)
}
export function serverWebErrors(id: number, hours = 24): Promise<WebErrorRow[]> {
  return api<WebErrorRow[]>(`/api/servers/${id}/web-errors?hours=${hours}`)
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
  monitors: boolean // false - только для внешней проверки портов
  // куда выходит в интернет по последней проверке: адрес и ISO-код страны, '' - не определилось;
  // exit_at null - еще не проверяли
  exit_ip: string
  exit_country: string
  exit_at: string | null
  created_at: string
}

// Сайт лежит по внешней проверке, а его домен обслуживает известная панели нода.
// Почти всегда это белый список: снаружи рвут соединение, а сайт цел.
export type LocalProbeSuggestion = {
  check_id: number
  name: string
  host: string
  message: string
  // local — проверять изнутри ноды, которая держит домен;
  // code  — сайт отвечает «не пущу» (403): он жив, довольно считать код нормой
  kind: 'local' | 'code'
  code: number
  server_id: number
  server_name: string
}

// Объект, по которому не сработает ни один алерт. Читается только админом:
// область действия правил — часть настроек алертов.
export type MuteWarning = {
  kind: 'server' | 'site'
  id: number
  name: string
  group: string
  reason_code: 'group_out' | 'no_group' | 'no_rules'
  fixable: boolean
}

export function alertCoverage(): Promise<{ items: MuteWarning[] }> {
  return api('/api/alerts/coverage')
}

// Дописать немые объекты в область уже включённых правил алертов.
export function fixAlertCoverage(
  items: { kind: string; id: number }[],
): Promise<{ rules: number; items: MuteWarning[] }> {
  return api('/api/alerts/coverage/apply', {
    method: 'POST',
    body: JSON.stringify({ items }),
  })
}

export function localProbeSuggestions(): Promise<{ items: LocalProbeSuggestion[] }> {
  return api('/api/checks/local-probe-suggestions')
}

export function applyLocalProbe(ids: number[]): Promise<{ updated: number }> {
  return api('/api/checks/local-probe-apply', {
    method: 'POST',
    body: JSON.stringify({ check_ids: ids }),
  })
}

export function listLocations(): Promise<Location[]> {
  return api<Location[]>('/api/locations')
}

export function createLocation(body: {
  name: string
  url: string
  enabled?: boolean
  monitors?: boolean
  exit_ip?: string
  exit_country?: string
}): Promise<Location> {
  return api<Location>('/api/locations', { method: 'POST', body: JSON.stringify(body) })
}

export function updateLocation(
  id: number,
  body: Partial<{ name: string; url: string; enabled: boolean; monitors: boolean; exit_ip: string; exit_country: string }>,
): Promise<Location> {
  return api<Location>(`/api/locations/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export function deleteLocation(id: number): Promise<void> {
  return api<void>(`/api/locations/${id}`, { method: 'DELETE' })
}

export type ProxyTestResult = {
  ok: boolean
  message: string
  latency_ms: number | null
  exit_ip: string
  exit_country: string
}

// Проверка сохраненной локации: заодно запоминает, где она выходит в интернет
export function testSavedLocation(id: number): Promise<ProxyTestResult> {
  return api<ProxyTestResult>(`/api/locations/${id}/test`, { method: 'POST' })
}

// Окно локаций: как каждая проверяет, через какую смотрят порты нод, где какой сайт недоступен
export type LocSiteCell = { status: string; message: string; fails: number }
export type LocationStat = {
  id: number
  checks: number
  up: number
  degraded: number
  down: number
  up_pct: number | null
  avg_ms: number | null
  last_at: string | null
  broken: boolean
  error: string
}
export type LocationsOverview = {
  locations: LocationStat[]
  ports_location_id: number | null
  sites: { check_id: number; name: string; group: string; main: LocSiteCell; cells: Record<string, LocSiteCell> }[]
}

export function getLocationsOverview(): Promise<LocationsOverview> {
  return api<LocationsOverview>('/api/locations/overview')
}

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

// Новый адрес push-монитора: старый перестает приниматься сразу
export function rotatePushToken(id: number): Promise<Check> {
  return api<Check>(`/api/checks/${id}/push-token`, { method: 'POST' })
}

// Заметка к серверу или монитору; пустая строка убирает ее
export function setServerNote(id: number, text: string): Promise<Server> {
  return api<Server>(`/api/servers/${id}/note`, { method: 'PUT', body: JSON.stringify({ text }) })
}

export function setCheckNote(id: number, text: string): Promise<Check> {
  return api<Check>(`/api/checks/${id}/note`, { method: 'PUT', body: JSON.stringify({ text }) })
}

// Известная проблема (backend app/known.py): про беду знают, алерт молчит, пока не стало хуже;
// ушла сама - отметка закрывается, вышел срок - алерт приходит как обычно. worse - стало хуже
// отмеченного, алерт ушел.
export type KnownIssue = {
  text: string
  by?: string
  at?: string
  until?: string | null
  worse?: boolean
  onboard?: boolean // поставлена при закрытии подключения: "было при подключении"
}

export function finishOnboard(id: number): Promise<Server> {
  return api<Server>(`/api/servers/${id}/onboard/done`, { method: 'POST' })
}

export function setServerKnown(id: number, kind: string, text: string, days: number): Promise<Server> {
  return api<Server>(`/api/servers/${id}/known/${encodeURIComponent(kind)}`, {
    method: 'PUT',
    body: JSON.stringify({ text, days }),
  })
}

export function dropServerKnown(id: number, kind: string): Promise<Server> {
  return api<Server>(`/api/servers/${id}/known/${encodeURIComponent(kind)}`, { method: 'DELETE' })
}

export function setCheckKnown(id: number, text: string, days: number): Promise<Check> {
  return api<Check>(`/api/checks/${id}/known`, { method: 'PUT', body: JSON.stringify({ text, days }) })
}

export function dropCheckKnown(id: number): Promise<Check> {
  return api<Check>(`/api/checks/${id}/known`, { method: 'DELETE' })
}

// Окно обслуживания (backend app/maint.py): алерты по выбранным серверам и мониторам молчат,
// после окна - сверка. repeat: "" разово, daily каждый день, weekly по weekdays (0 = пн).
export type MaintIn = {
  title: string
  server_ids: number[]
  check_ids: number[]
  starts_at: string
  ends_at: string
  repeat: '' | 'daily' | 'weekly'
  weekdays: number[]
  verify: boolean
  note: string
}

export type MaintWindow = MaintIn & {
  id: number
  created_by: string
  active: boolean
  cur_end: string | null
  next_start: string | null
  finished: boolean
}

export const listMaint = () => api<MaintWindow[]>('/api/maintenance')

export function createMaint(body: MaintIn): Promise<MaintWindow> {
  return api<MaintWindow>('/api/maintenance', { method: 'POST', body: JSON.stringify(body) })
}

export function updateMaint(id: number, body: MaintIn): Promise<MaintWindow> {
  return api<MaintWindow>(`/api/maintenance/${id}`, { method: 'PUT', body: JSON.stringify(body) })
}

export function deleteMaint(id: number): Promise<void> {
  return api<void>(`/api/maintenance/${id}`, { method: 'DELETE' })
}

// DNS-монитор: принять текущий ответ как правильный и сразу перепроверить
export function acceptDns(id: number): Promise<CheckRun> {
  return api<CheckRun>(`/api/checks/${id}/dns-accept`, { method: 'POST' })
}

// Отчет за период (backend app/api/report.py): только в панели, в Telegram не уходит
export interface ReportSite {
  id: number
  name: string
  group: string
  uptime: number | null
  incidents: number
  downtime_s: number
  longest_s: number
  last_at: string | null
  open: boolean
}
export interface ReportServer {
  id: number
  name: string
  group: string
  offline_s: number
  gaps: number
  longest_s: number
  alerts: number
  things: { what: string; n: number }[] // что именно было, самое частое первым
}
// что шумело: что именно (какой юнит, под, репозиторий - из текста алерта) и где
export interface ReportNoise {
  target: string
  section: string
  kind: string
  label: string // вид алерта: подсказка, сама строка - what
  what: string
  note: string // коротко почему: код выхода, причина, кто ел CPU
  n: number
  days: number // в скольких днях периода было
  first: string
  last: string
  srv_id: number | null
  chk_id: number | null
}
export interface ReportTotals {
  uptime: number | null
  incidents: number
  downtime_s: number
}
export interface Report {
  days: number
  since: string
  until: string
  prev_ok: boolean
  // дни периода в часовом поясе алертов; per_day ниже - по ним же
  day_labels: string[]
  sites?: ReportTotals & {
    total: number
    affected: number
    prev: ReportTotals | null
    items: ReportSite[]
    more: number
    per_day: number[] // секунд простоя за день
  }
  servers?: {
    total: number
    with_gaps: number
    offline_s: number
    prev_offline_s: number
    prev_ok?: boolean // прошлый период целиком есть в хранении метрик
    panel_s: number
    panel_gaps: number
    items: ReportServer[]
    more: number
    per_day: number[] // секунд недоступности за день
  }
  noise: {
    fires: number
    prev_fires: number
    prev_ok?: boolean
    per_day: number[] // алертов за день
    items: ReportNoise[]
    more: number
  }
  // окно за период одной строкой: сколько раз было, start/end - последнее вхождение
  maint: { title: string; repeat: string; count: number; start: string; end: string; servers: number; checks: number }[]
}
export function getReport(days: number): Promise<Report> {
  return api<Report>(`/api/report?days=${days}`)
}

// --- поиск по парку (backend app/search.py) ---
export type SearchItem = {
  title: string
  // подпись: данные (сервер, образ, адреса), слова к ним фронт подставляет сам
  sub: string[]
  id: number
  section: string
  sec: string
  tag: string
  hint_k: string
  hint_v: string
}
export type SearchResult = { q: string; groups: { kind: string; total: number; items: SearchItem[] }[] }
export function search(q: string): Promise<SearchResult> {
  return api<SearchResult>(`/api/search?q=${encodeURIComponent(q)}`)
}

// --- хронология сервера (backend app/timeline.py) ---
export type TimelineEvent = {
  ts: string
  cat: string
  kind: string
  text: string
  obj?: string
  n?: number
  user?: string
  end?: string
  recovery?: boolean
  alert?: boolean
  // прогон пакетов (kind "pkgs"): что именно поставили, обновили и удалили
  data?: { a: string; p: string; o: string; n: string }[]
}
export type TimelineResult = { since: string; until: string; events: TimelineEvent[]; cats: Record<string, number> }
export function getServerTimeline(id: number, days: number): Promise<TimelineResult> {
  return api<TimelineResult>(`/api/servers/${id}/timeline?days=${days}`)
}

// --- пакеты сервера (backend app/packages.py, агент 2.28) ---
export type ServerPackages = { mgr: string; n: number; ts: string | null; items: [string, string][]; total: number }
export function getServerPackages(id: number, q: string): Promise<ServerPackages> {
  return api<ServerPackages>(`/api/servers/${id}/packages?q=${encodeURIComponent(q)}`)
}

// --- важные уязвимости (backend app/vulns.py) ---
export type VulnCve = { id: string; prio: string; cvss: number; kev: boolean }
export type VulnFinding = {
  src: string
  pkgs: [string, string][]
  fixed: string
  pro: boolean
  kernel: boolean
  cves: VulnCve[]
}
export type VulnSummary = { pkgs: number; cves: number; critical: number; kev: number; pro: number; kernel: boolean }
export type ServerVulns = {
  release: string
  os: string
  eol: string
  pkgs: number
  pkgs_ts: string | null
  feed_ts: string | null
  feed_gen: string
  feed_err: string
  pro_on: boolean
  found: VulnFinding[]
  summary: VulnSummary
}
export function getServerVulns(id: number): Promise<ServerVulns> {
  return api<ServerVulns>(`/api/servers/${id}/vulns`)
}
export type FleetVulnServer = ServerVulns & { id: number; name: string; group: string }
export type FleetVulns = {
  servers: FleetVulnServer[]
  feeds: { release: string; ts: string | null; gen: string; err: string }[]
}
export function getFleetVulns(): Promise<FleetVulns> {
  return api<FleetVulns>('/api/vulns')
}

// --- карта зависимостей (backend api/depmap.py) ---
export type DepMapServer = {
  id: number
  name: string
  group: string
  parent_id: number | null
  online: boolean
  enabled: boolean
  last_seen: string | null
}
// mode: off - отвязан руками, ни от какого сервера не зависит
export type DepMapCheck = {
  id: number
  name: string
  type: string
  target: string
  status: string
  mode: string
  parents: { id: number; why: string }[]
}
export type DepMap = { servers: DepMapServer[]; checks: DepMapCheck[] }
export function getDepMap(): Promise<DepMap> {
  return api<DepMap>('/api/depmap')
}

// --- журнал ошибок панели (backend app/panel_errors.py) ---
export type PanelErrorKind = 'server' | 'api' | 'command' | 'front' | 'report'
export type PanelErrorBrief = {
  id: number
  kind: PanelErrorKind
  last_at: string
  count: number
  action: string
  status: number | null
  message: string
  server_name: string
  page: string
}
export type PanelErrorRow = PanelErrorBrief & {
  first_at: string
  username: string
  section: string
  server_id: number | null
  detail: string
  version: string
  extra: {
    errors?: PanelErrorBrief[]
    client_log?: { ts: string; method: string; path: string; status: number; detail: string }[]
    params?: Record<string, unknown>
    cmd_id?: number
    job_id?: number
  }
  resolved: boolean
  resolved_at: string | null
  resolved_by: string
  reopened: boolean
}
export type PanelErrorsOut = {
  items: PanelErrorRow[]
  more: boolean
  counts: { open: number; by_kind: Partial<Record<PanelErrorKind, number>>; day: number; resolved: number }
  keep_days: number
  max_rows: number
}
export type PanelErrorsQuery = { state: 'open' | 'resolved' | 'all'; kind: string; q: string; days: number }
export function getPanelErrors(p: PanelErrorsQuery): Promise<PanelErrorsOut> {
  const qs = new URLSearchParams({ state: p.state, kind: p.kind, q: p.q, days: String(p.days) })
  return api<PanelErrorsOut>(`/api/panel-errors?${qs}`)
}
export function getPanelErrorCount(): Promise<{ open: number; reports: number }> {
  return api<{ open: number; reports: number }>('/api/panel-errors/count')
}
export function resolvePanelErrors(ids: number[], resolved: boolean): Promise<{ ok: boolean; changed: number }> {
  return api('/api/panel-errors/resolve', { method: 'POST', body: JSON.stringify({ ids, resolved }) })
}
export function sendProblemReport(body: {
  text: string
  page: string
  section: string
  client_log: { ts: string; method: string; path: string; status: number; detail: string }[]
}): Promise<{ id: number; attached: number; client_log: number }> {
  return api('/api/panel-errors/report', { method: 'POST', body: JSON.stringify(body) })
}
