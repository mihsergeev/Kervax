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

// --- вкладка со старой сборкой ---------------------------------------------------------------
// После выкладки открытая вкладка живет со старым JS, а данные приходят уже от нового бэкенда:
// поле, которое новый бэкенд больше не отдает, роняет старый экран (24.10 файлы сайтов читали
// r.edit.length, а 1.8.28 перестал отдавать edit). Это не ошибка текущей версии: в журнал такое не
// пишем, а упавший экран перезагружаем на новую сборку.

const STALE_MS = 60_000
const RELOAD_KEY = 'kervax_stale_reload'

// путь бандла из index.html сборки vite; у dev-сервера его нет
export function bundleOf(html: string): string | null {
  const m = html.match(/<script\b[^>]*\bsrc="([^"]*\/assets\/index-[^"/]+\.js)"/)
  return m ? m[1] : null
}

// свежая сборка (путь бандла), если вкладка живет со старой; null - та же или не понять
export function staleOf(loaded: string | null, freshHtml: string): string | null {
  const fresh = bundleOf(freshHtml)
  if (!loaded || !fresh) return null
  const path = (u: string) => new URL(u, 'http://x').pathname
  return path(fresh) !== path(loaded) ? path(fresh) : null
}

let staleCheck: { at: number; p: Promise<string | null> } | null = null
export function staleBuild(): Promise<string | null> {
  const now = Date.now()
  if (staleCheck && now - staleCheck.at < STALE_MS) return staleCheck.p
  const el = document.querySelector<HTMLScriptElement>('script[type="module"][src*="/assets/index-"]')
  const loaded = el?.getAttribute('src') ?? null
  const p = !loaded
    ? Promise.resolve(null)
    : fetch('/', { cache: 'no-store' })
      .then((r) => (r.ok ? r.text() : ''))
      .then((html) => staleOf(loaded, html))
      .catch(() => null)
  staleCheck = { at: now, p }
  return p
}

// перезагрузить на свежую сборку - один раз на сборку: если и после перезагрузки ошибка,
// значит это ошибка новой версии, и она идет в журнал как обычно
export function reloadToFresh(fresh: string): boolean {
  try {
    if (sessionStorage.getItem(RELOAD_KEY) === fresh) return false
    sessionStorage.setItem(RELOAD_KEY, fresh)
  } catch {
    return false
  }
  window.location.reload()
  return true
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
  const body = JSON.stringify({
    kind,
    message: msg,
    stack: (stack || '').slice(0, 8000),
    component: (component || '').slice(0, 4000),
    page: uiPage(),
    section: ctx.section,
  })
  const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...uiHeaders() }
  // вкладка со старой сборкой: ошибка не про текущую версию (см. staleBuild)
  void staleBuild().then((fresh) => {
    if (fresh) return
    return fetch('/api/panel-errors/front', { method: 'POST', headers, body, keepalive: true })
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
    const r = ev.reason as { status?: number; method?: string } | undefined
    if (r && typeof r === 'object' && r.status === 401) return
    // отказ на изменяющий запрос и 5xx бэкенд уже записал сам (panel_errors.py) - второй строкой не дублируем
    if (r && typeof r === 'object' && typeof r.status === 'number'
      && (r.status >= 500 || (r.status >= 400 && !!r.method && r.method !== 'GET' && r.method !== 'HEAD'))) return
    const { message, stack } = errText(ev.reason)
    reportFront('rejection', message, stack)
  })
}

export function errorFields(e: unknown): { message: string; stack: string } {
  return errText(e)
}
