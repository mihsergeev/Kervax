// Ошибки интерфейса в журнал ошибок панели (backend app/panel_errors.py) и последние неудачные
// запросы к API для "Сообщить о проблеме". Раньше человек, у которого что-то сломалось, мог
// только прислать скриншот: теперь ошибка доходит сама, с адресом страницы и разделом.
//
// Частоту держим здесь: одна и та же ошибка - не чаще раза в SAME_MS, всего не больше MAX_BURST
// за BURST_MS с вкладки (зациклившийся эффект не должен засыпать журнал). Бэкенд ограничивает
// еще раз со своей стороны.

const TOKEN_KEY = 'kervax_token' // тот же ключ, что в api.ts (импорт оттуда дал бы цикл)
const SAME_MS = 60_000
const BURST_MS = 5 * 60_000
const MAX_BURST = 10
const LOG_MAX = 20
const RECENT_MS = 15 * 60_000 // как RECENT_MIN на бэкенде

// раздел панели (Сайты, Серверы...) - App кладет сюда текущий
const ctx = { section: '' }
export function setUiSection(section: string): void {
  ctx.section = section
}
export function uiSection(): string {
  return ctx.section
}
// адрес страницы с параметрами открытой карточки (?server=12&sec=backup)
export function uiPage(): string {
  return (window.location.pathname + window.location.search).slice(0, 300)
}
// заголовки к каждому запросу: бэкенд пишет их в журнал, если запрос не удался. По X-Kervax-Page
// он же отличает запрос интерфейса панели от скрипта: 422 первого - ошибка панели и идет в журнал
export function uiHeaders(): Record<string, string> {
  const h: Record<string, string> = { 'X-Kervax-Page': uiPage() }
  if (ctx.section) h['X-Kervax-Section'] = ctx.section
  return h
}

// --- последние неудачные запросы -------------------------------------------------------------

export type ClientLogItem = { ts: string; method: string; path: string; status: number; detail: string }
const log: ClientLogItem[] = []

export function noteApiFailure(method: string, path: string, status: number, detail: string): void {
  if (status === 401) return // протухший вход - не проблема интерфейса
  log.push({ ts: new Date().toISOString(), method, path: path.slice(0, 300), status, detail: detail.slice(0, 500) })
  if (log.length > LOG_MAX) log.splice(0, log.length - LOG_MAX)
}

export function recentClientLog(): ClientLogItem[] {
  const edge = Date.now() - RECENT_MS
  return log.filter((x) => Date.parse(x.ts) >= edge)
}

// --- ошибки интерфейса -----------------------------------------------------------------------

const seen = new Map<string, number>()
let burst: number[] = []

// шум браузера, по которому нечего чинить
const IGNORED = [/^ResizeObserver loop/, /^Script error\.?$/]

export function reportFront(kind: 'error' | 'rejection' | 'render', message: string, stack = '', component = ''): boolean {
  const token = (() => {
    try {
      return localStorage.getItem(TOKEN_KEY)
    } catch {
      return null
    }
  })()
  if (!token) return false
  const msg = (message || '').slice(0, 2000)
  if (IGNORED.some((r) => r.test(msg))) return false
  const now = Date.now()
  const key = `${kind}|${msg}|${(stack || '').split('\n').slice(0, 2).join('|')}`
  const last = seen.get(key)
  if (last != null && now - last < SAME_MS) return false
  burst = burst.filter((x) => now - x < BURST_MS)
  if (burst.length >= MAX_BURST) return false
  seen.set(key, now)
  if (seen.size > 200) seen.clear()
  burst.push(now)
  void fetch('/api/panel-errors/front', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...uiHeaders() },
    body: JSON.stringify({
      kind,
      message: msg,
      stack: (stack || '').slice(0, 8000),
      component: (component || '').slice(0, 4000),
      page: uiPage(),
      section: ctx.section,
    }),
    keepalive: true,
  }).catch(() => undefined)
  return true
}

function errText(e: unknown): { message: string; stack: string } {
  if (e instanceof Error) return { message: `${e.name}: ${e.message}`, stack: e.stack || '' }
  if (typeof e === 'string') return { message: e, stack: '' }
  try {
    return { message: JSON.stringify(e).slice(0, 500), stack: '' }
  } catch {
    return { message: String(e), stack: '' }
  }
}

let installed = false
export function installErrorHandlers(): void {
  if (installed) return
  installed = true
  window.addEventListener('error', (ev) => {
    const { message, stack } = ev.error ? errText(ev.error) : { message: ev.message, stack: '' }
    reportFront('error', message, stack || (ev.filename ? `at ${ev.filename}:${ev.lineno}:${ev.colno}` : ''))
  })
  window.addEventListener('unhandledrejection', (ev) => {
    const r = ev.reason as { status?: number } | undefined
    if (r && typeof r === 'object' && r.status === 401) return
    const { message, stack } = errText(ev.reason)
    reportFront('rejection', message, stack)
  })
}

export function errorFields(e: unknown): { message: string; stack: string } {
  return errText(e)
}
