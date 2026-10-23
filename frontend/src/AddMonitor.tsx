import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  ApiError,
  getSiteAlertRules,
  listLocations,
  tryCheck,
  tryCheckInside,
  type CheckForm,
  type Location,
  type CheckTryOut,
  type ServerAlertRules,
  type TryIn,
  type TryInside,
  type TryLocation,
  type TryPanel,
  type TryResult,
} from './api'
import { useI18n } from './i18n'
import { EXPIRY_WARN_DAYS, MON_ALERT_KINDS } from './checkUtils'

// Форма нового монитора: адрес, пробная проверка и выбор "откуда проверять" по ее итогам.
// Старая форма спрашивала тип, адрес и способ проверки вслепую: увидит ли панель сайт,
// человек узнавал от красного монитора и алерта. Здесь адрес проверяется до сохранения - с
// панели, изнутри сервера, где нашелся домен, и из локаций (backend app/try_probe.py), а
// рабочий способ выбран сам.
//
// DNS-монитор проверяется так же, до сохранения: видно, что резолвер отвечает сейчас, и что
// монитор с этим сделает - запомнит ответ или сравнит с ожидаемым. Push никуда не ходит, у него
// проверять нечего: только как часто ждать сигнал.
//
// Правка монитора - та же форма (editing): адрес и настройки берутся из монитора, проверка
// запускается сама при открытии, но сохранить можно и без нее. По итогам проверки форма в
// правке ничего не меняет сама, только подсказывает: человек открыл ее поменять что-то свое.

type Kind = 'addr' | 'dns' | 'push'
type Where = 'panel' | 'local' | 'locations'
type Preset = 'n1' | 'n3' | 'm5' | 'own'
type Slot<T> = { st: 'wait' } | { st: 'ok'; v: T } | { st: 'err'; msg: string }
type Addr = { type: 'http' | 'tcp_port' | 'dns' | 'push'; target: string; port: number }
type Tried = Addr & {
  auth: string // настройки входа, с которыми шла проверка (authKey)
  ask: string // dns: какую запись спросили
  dns: string // dns: резолвер и ожидаемый ответ, с которыми шла проверка (dnsKey)
  panel: Slot<CheckTryOut>
  exp: Slot<CheckTryOut> | null
  locs: Slot<TryLocation[]> | null
  inside: Slot<TryInside> | null
}

type Props = {
  base: CheckForm
  // правка существующего монитора: base - его настройки, checkId - он сам (не дубль себе)
  editing?: boolean
  checkId?: number
  groups: string[]
  busy: boolean
  error: string | null
  // поля, которые бэкенд не принял (422): подсвечиваем
  errorFields?: string[]
  onSubmit: (form: CheckForm) => void
  onCancel: () => void
  onOpenCheck?: (id: number) => void
  onUnauthorized: () => void
}

const WAIT = { st: 'wait' } as const
const SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i
const DNS_TYPES = ['A', 'AAAA', 'CNAME', 'MX', 'NS', 'TXT']
// как часто скрипт обязан постучаться: пресеты закрывают почти все кроны, остальное - минутами
const PUSH_PERIODS: [number, string][] = [
  [300, '5 мин'],
  [900, '15 мин'],
  [3600, '1 ч'],
  [6 * 3600, '6 ч'],
  [86400, '1 сутки'],
  [7 * 86400, '1 неделя'],
]

// имя для DNS как его сохранит панель (checks._dns_defaults): без схемы, пути, порта и точки
function dnsName(raw: string): string {
  let v = raw.trim().toLowerCase().replace(SCHEME, '').split(/[/?#]/)[0]
  if ((v.match(/:/g) ?? []).length === 1) v = v.split(':')[0]
  return v.replace(/\.+$/, '')
}

// резолвер и ожидаемый ответ: поменяли - прежняя проверка уже не про них
const dnsKey = (f: CheckForm) =>
  JSON.stringify([(f.dns_server ?? '').trim(), (f.dns_expect ?? '').trim(), f.dns_match ?? 'equal'])

// допуск push по умолчанию - десятая часть периода, но не меньше 2 минут (checks.push_limits)
// поля в тонкой настройке: не принял бэкенд - раскрываем ее
const ADV_FIELDS = [
  'push_grace', 'interval_seconds', 'dns_server', 'degraded_ms', 'retries', 'degraded_after_failures',
  'expected_status', 'keyword_up', 'keyword_down', 'ssl_warn_days', 'domain_warn_days', 'extra_paths',
  'http_headers', 'location_ids', 'alert_mutes',
]

const pushGrace = (f: CheckForm) => f.push_grace || Math.max(120, Math.floor((f.push_period || 3600) / 10))

// "db.example.com:5432" - порт, остальное - сайт; со схемой в начале - всегда сайт
function parseAddr(raw: string): Addr {
  const v = raw.trim()
  if (SCHEME.test(v)) return { type: 'http', target: v, port: 0 }
  const m = v.match(/^(\[[0-9a-f:.]+\]|[^\s/:[\]]+):(\d{1,5})$/i)
  const port = m ? Number(m[2]) : 0
  if (m && port > 0 && port <= 65535) return { type: 'tcp_port', target: m[1].replace(/^\[|\]$/g, ''), port }
  return { type: 'http', target: v, port: 0 }
}

// адрес, как он встанет в заголовок алерта (collector._display_host): без схемы и query
function displayHost(a: Addr): string {
  if (a.type === 'tcp_port') return a.port ? `${a.target}:${a.port}` : a.target
  try {
    const u = new URL(SCHEME.test(a.target) ? a.target : `https://${a.target}`)
    return (u.host + u.pathname).replace(/\/+$/, '')
  } catch {
    return a.target
  }
}

// как checks.status_matches: "200-399,401"
function statusOk(code: number, spec: string): boolean {
  return (spec.trim() || '200-399').split(',').some((part) => {
    const [lo, hi] = part.split('-').map((x) => Number(x.trim()))
    return hi ? code >= lo && code <= hi : code === lo
  })
}

// заголовки пишут как в curl, по строке "Имя: значение"; монитор хранит их JSON-объектом
function headersJson(text: string): { json: string; bad: string[] } {
  const obj: Record<string, string> = {}
  const bad: string[] = []
  for (const line of text.split('\n')) {
    const v = line.trim()
    if (!v) continue
    const i = v.indexOf(':')
    if (i <= 0) bad.push(v)
    else obj[v.slice(0, i).trim()] = v.slice(i + 1).trim()
  }
  return { json: Object.keys(obj).length ? JSON.stringify(obj) : '', bad }
}

function headersText(json: string): string {
  try {
    const v = JSON.parse(json || '{}')
    return Object.entries(v).map(([k, x]) => `${k}: ${x}`).join('\n')
  } catch {
    return ''
  }
}

// адрес монитора в поле формы: у порта - хост:порт, IPv6 в скобках
function addrOf(f: CheckForm): string {
  if (f.type === 'push') return ''
  if (f.type !== 'tcp_port' || !f.port) return f.target
  return f.target.includes(':') ? `[${f.target}]:${f.port}` : `${f.target}:${f.port}`
}

// тонкую настройку в правке раскрываем сразу, если в ней уже что-то свое
function advTouched(f: CheckForm, authShown: boolean): boolean {
  const days = (v: number[] | undefined, d: number[]) => (v ?? d).join(',') !== d.join(',')
  if (f.type === 'push') return !!f.push_grace || (f.alert_mutes ?? []).length > 0
  if (f.type === 'dns')
    return (f.interval_seconds ?? 300) !== 300 || !!f.dns_server || (f.alert_mutes ?? []).length > 0
  return (
    (f.interval_seconds ?? 60) !== 60 ||
    (f.degraded_ms ?? 2000) !== 2000 ||
    !['', '200-399', '200-399,401'].includes(f.expected_status ?? '') ||
    !!f.keyword_up ||
    !!f.keyword_down ||
    (f.extra_paths ?? []).length > 0 ||
    (!!f.http_headers && !authShown) ||
    !!f.ignore_tls ||
    (f.retries ?? 3) !== 3 ||
    (f.degraded_after_failures ?? 10) !== 10 ||
    days(f.ssl_warn_days, EXPIRY_WARN_DAYS) ||
    days(f.domain_warn_days, EXPIRY_WARN_DAYS) ||
    (f.alert_mutes ?? []).length > 0 ||
    f.location_ids != null
  )
}

// что из формы меняет ответ сайта на вход: код-норма, логин, заголовки
const authKey = (f: CheckForm) =>
  JSON.stringify([f.expected_status ?? '', f.auth_method ?? '', f.auth_user ?? '', f.auth_pass ?? '', f.http_headers ?? ''])

const done = <T,>(s: Slot<T> | null | undefined): T | undefined => (s && s.st === 'ok' ? s.v : undefined)

export function AddMonitor({
  base,
  editing = false,
  checkId,
  groups,
  busy,
  error,
  errorFields,
  onSubmit,
  onCancel,
  onOpenCheck,
  onUnauthorized,
}: Props) {
  const { t } = useI18n()
  const [kind, setKind] = useState<Kind>(base.type === 'push' ? 'push' : base.type === 'dns' ? 'dns' : 'addr')
  const [addr, setAddr] = useState(() => (editing ? addrOf(base) : ''))
  const [form, setForm] = useState<CheckForm>(base)
  const set = (patch: Partial<CheckForm>) => setForm((f) => ({ ...f, ...patch }))
  const [tried, setTried] = useState<Tried | null>(null)
  const runRef = useRef(0)
  const unauthRef = useRef(onUnauthorized)
  unauthRef.current = onUnauthorized
  const [where, setWhere] = useState<Where>(() =>
    !editing ? 'panel' : base.probe_local ? 'local' : base.check_locations ? 'locations' : 'panel',
  )
  // в правке способ уже выбран человеком, и проверка его не переключает
  const [whereTouched, setWhereTouched] = useState(editing)
  const [name, setName] = useState<string | null>(editing ? base.name : null) // null = из адреса
  const [group, setGroup] = useState<string | null>(editing ? base.group_name ?? '' : null) // null = как у мониторов домена
  const ownFails = base.alert_after_failures ?? 3
  const [preset, setPreset] = useState<Preset>(() => {
    if (!editing) return 'n3'
    const every = base.interval_seconds ?? 60
    return ownFails === 1 ? 'n1' : ownFails === 3 ? 'n3' : ownFails === Math.max(1, Math.round(300 / every)) ? 'm5' : 'own'
  })
  const hasCreds = base.auth_method === 'basic' || !!base.http_headers
  const [adv, setAdv] = useState(() => editing && advTouched(base, hasCreds))
  const bad = (f: string) => (errorFields?.includes(f) ? ' field-bad' : '')
  // поле, которое бэкенд не принял, может сидеть в свернутой тонкой настройке: раскрываем и
  // показываем первое такое поле
  const rootRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!errorFields?.length) return
    if (errorFields.some((f) => ADV_FIELDS.includes(f))) setAdv(true)
    requestAnimationFrame(() => rootRef.current?.querySelector('.field-bad')?.scrollIntoView({ block: 'center' }))
  }, [errorFields])
  const [addOff, setAddOff] = useState(false)
  // '' - в правке: сайт просит вход, а что с этим делать, монитор еще не решил
  const [auth, setAuth] = useState<'ok401' | 'give' | ''>(() =>
    !editing ? 'ok401' : hasCreds ? 'give' : statusOk(401, base.expected_status || '200-399') ? 'ok401' : '',
  )
  // блок входа не прячем, когда с логином сайт ответил 200: там то, что человек вписал
  const [authSeen, setAuthSeen] = useState(editing && hasCreds)
  const [allLocs, setAllLocs] = useState<Location[]>([])
  const [sslTxt, setSslTxt] = useState(() => (base.ssl_warn_days ?? EXPIRY_WARN_DAYS).join(', '))
  const [domTxt, setDomTxt] = useState(() => (base.domain_warn_days ?? EXPIRY_WARN_DAYS).join(', '))
  const [hdrText, setHdrText] = useState(() => headersText(base.http_headers ?? ''))
  const [pathsText, setPathsText] = useState(() => (base.extra_paths ?? []).join('\n'))
  const [rules, setRules] = useState<ServerAlertRules | null>(null)
  // dns: поле ожидаемого ответа; пустое - монитор запомнит первый ответ
  const [wantOpen, setWantOpen] = useState(() => !!(base.dns_expect ?? '').trim())
  // push: свой период минутами, когда ни один пресет не подходит
  const [ownPeriod, setOwnPeriod] = useState(() => !PUSH_PERIODS.some(([v]) => v === (base.push_period || 3600)))

  // правила алертов видит только админ; без них предпросмотр в обычном формате
  useEffect(() => {
    getSiteAlertRules().then(setRules).catch(() => {})
    listLocations().then(setAllLocs).catch(() => {})
  }, [])
  // ответы прежнего нажатия после закрытия формы никуда не пишем
  useEffect(() => () => void runRef.current++, [])

  const isDns = kind === 'dns'
  const isPush = kind === 'push'
  const addrFor = (raw: string): Addr =>
    kind === 'push'
      ? { type: 'push', target: '', port: 0 }
      : kind === 'dns'
        ? { type: 'dns', target: dnsName(raw), port: 0 }
        : parseAddr(raw)
  const askOf = (f: CheckForm, type: Addr['type']) => (type === 'dns' ? f.dns_type || 'A' : '')
  const cur = addrFor(addr)
  const fresh =
    !!tried &&
    tried.target === cur.target &&
    tried.type === cur.type &&
    tried.port === cur.port &&
    tried.ask === askOf(form, cur.type)
  const panel = fresh ? done(tried.panel)?.panel ?? undefined : undefined
  const ctx = fresh ? done(tried.panel) : undefined
  const exp = fresh ? done(tried.exp) : undefined
  const locs = fresh ? done(tried.locs) : undefined
  const inside = fresh ? done(tried.inside) : undefined
  const http = cur.type === 'http'
  const isTcp = cur.type === 'tcp_port'
  const expected = form.expected_status || '200-399'
  const dnsType = form.dns_type || 'A'

  // "работает": отвечает, либо ответ - код, который в форме объявлен нормой (401 и т.п.)
  const works = (r: TryResult | undefined) =>
    !!r &&
    (r.status === 'up' ||
      r.status === 'degraded' ||
      (r.status === 'down' && r.kind === 'http' && r.code > 0 && statusOk(r.code, expected)))

  // over - то, что поменяли этим же нажатием и чего в form еще нет (тип записи, ожидаемый ответ)
  const probe = (over: Partial<CheckForm> = {}) => {
    const f = { ...form, ...over }
    const a = addrFor(addr)
    if (!a.target) return
    const run = ++runRef.current
    const isHttp = a.type === 'http'
    if (!editing) setWhereTouched(false)
    if (tried && tried.target !== a.target) setAuthSeen(false)
    setTried({
      ...a,
      auth: authKey(f),
      ask: askOf(f, a.type),
      dns: dnsKey(f),
      panel: WAIT,
      exp: isHttp ? WAIT : null,
      locs: isHttp ? WAIT : null,
      inside: isHttp ? WAIT : null,
    })
    const put = (patch: Partial<Tried>) => {
      if (runRef.current === run) setTried((x) => (x ? { ...x, ...patch } : x))
    }
    const fail = (e: unknown): { st: 'err'; msg: string } => {
      if (e instanceof ApiError && e.status === 401) onUnauthorized()
      return { st: 'err', msg: e instanceof Error ? e.message : t('Ошибка') }
    }
    const body: Omit<TryIn, 'part'> =
      a.type === 'dns'
        ? {
            check_id: checkId,
            type: 'dns',
            target: a.target,
            timeout_ms: f.timeout_ms,
            dns_type: askOf(f, a.type),
            dns_server: (f.dns_server ?? '').trim(),
            dns_expect: f.dns_expect ?? '',
            dns_match: f.dns_match ?? 'equal',
          }
        : {
            check_id: checkId,
            type: a.type === 'tcp_port' ? 'tcp_port' : 'http',
            target: a.target,
            port: a.port,
            timeout_ms: f.timeout_ms,
            degraded_ms: f.degraded_ms,
            method: f.method,
            expected_status: f.expected_status,
            keyword_up: f.keyword_up,
            keyword_down: f.keyword_down,
            auth_method: f.auth_method ?? '',
            auth_user: f.auth_user ?? '',
            auth_pass: f.auth_pass ?? '',
            http_headers: f.http_headers ?? '',
            ignore_tls: f.ignore_tls,
            extra_paths: f.extra_paths,
          }
    tryCheck({ ...body, part: 'panel' }).then(
      (v) => put({ panel: { st: 'ok', v } }),
      (e) => put({ panel: fail(e) }),
    )
    if (!isHttp) return
    tryCheck({ ...body, part: 'expiry' }).then(
      (v) => put({ exp: { st: 'ok', v } }),
      (e) => put({ exp: fail(e) }),
    )
    tryCheck({ ...body, part: 'locations' }).then(
      (v) => put({ locs: { st: 'ok', v: v.locations ?? [] } }),
      (e) => put({ locs: fail(e) }),
    )
    tryCheck({ ...body, part: 'inside' }).then(
      (v) => put({ inside: v.inside ? { st: 'ok', v: v.inside } : fail(null) }),
      (e) => put({ inside: fail(e) }),
    )
  }

  // в правке монитор уже есть: видно сразу, как он сейчас открывается
  useEffect(() => {
    if (editing) probe()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ответ агента забираем опросом, пока он в пути
  const insideSlot = tried?.inside
  useEffect(() => {
    const v = insideSlot && insideSlot.st === 'ok' ? insideSlot.v : null
    if (!v || v.status !== 'pending' || v.request_id == null) return
    const run = runRef.current
    const rid = v.request_id
    const h = window.setTimeout(() => {
      tryCheckInside(rid).then(
        (r) => {
          if (runRef.current === run && r.inside)
            setTried((x) => (x ? { ...x, inside: { st: 'ok', v: r.inside as TryInside } } : x))
        },
        (e) => {
          if (e instanceof ApiError && e.status === 401) unauthRef.current()
          if (runRef.current === run)
            setTried((x) => (x ? { ...x, inside: { st: 'err', msg: e instanceof Error ? e.message : '' } } : x))
        },
      )
    }, v.fast ? 1000 : 2000)
    return () => window.clearTimeout(h)
  }, [insideSlot])

  const ips = panel?.ip_results && panel.ip_results.length > 1 ? panel.ip_results : null
  // адрес оцениваем так же, как весь сайт: код, объявленный нормой, - не ошибка
  const ipWorks = (x: { status: string; message: string }) => {
    const code = Number(x.message.match(/HTTP (\d{3})/)?.[1] ?? 0)
    return x.status !== 'down' || (code > 0 && statusOk(code, expected))
  }
  const ipBad = ips ? ips.filter((x) => !ipWorks(x)) : []
  const locOk = (locs ?? []).filter((l) => works(l))
  const locBad = (locs ?? []).filter((l) => !works(l))
  // у домена с несколькими адресами общий текст начинается с первого сбойного: код смотрим по каждому
  const has401 =
    panel?.code === 401 || inside?.code === 401 || !!ips?.some((x) => x.message.includes('HTTP 401'))
  const panelWorks = ips ? ipBad.length === 0 : works(panel)
  const srv = inside?.server_name || ''
  const insideKnown = !!inside && inside.kind !== 'no_server'
  const panelPending = fresh && tried.panel.st === 'wait'
  // правка: адрес поменяли, а проверка была по старому
  const stale = !!tried && !fresh

  // Что показала проверка и откуда лучше проверять. Локации не советуем никогда: у такого
  // монитора основной статус все равно с панели, локации только добавляются к нему.
  const assess = (): { rec: Where | ''; tone: string; text: string; dead: boolean } => {
    const none = { rec: '' as const, tone: '', text: '', dead: false }
    if (!fresh) return none
    if (tried.panel.st === 'err') return { ...none, tone: 'bad', text: tried.panel.msg }
    if (!panel) return none
    const msg = panel.message
    if (isDns) return assessDns(panel)
    if (isTcp) {
      if (panelWorks) return { rec: 'panel', tone: 'ok', text: t('Порт открыт с панели.'), dead: false }
      return {
        rec: '',
        tone: 'bad',
        dead: true,
        text: editing
          ? t('С панели порт не открывается: {msg}. Изнутри сервера и из локаций проверяются только сайты. Проверьте адрес и порт, а если сервис на обслуживании, монитор можно выключить ниже.', { msg })
          : t('С панели порт не открывается: {msg}. Изнутри сервера и из локаций проверяются только сайты, поэтому такой монитор сразу будет красным. Проверьте адрес и порт, а если сервис только поднимается, можно добавить выключенным.', { msg }),
      }
    }
    if ((has401 || authSeen) && auth === 'give' && (!panelWorks || tried.auth !== authKey(form)))
      return {
        rec: 'panel',
        tone: 'warn',
        dead: false,
        text: t('Сайт просит вход (401): впишите заголовок или логин и пароль ниже и нажмите "проверить снова".'),
      }
    if (has401 && auth === '' && !panelWorks)
      return {
        rec: 'panel',
        tone: 'warn',
        dead: false,
        text: t('Сайт просит вход (401), а монитор считает это сбоем. Выберите ниже: считать 401 нормой или дать доступ.'),
      }
    if (ips && ipBad.length && ipBad.length < ips.length) {
      const v = { n: ips.length, bad: ipBad.map((x) => x.ip).join(', ') }
      return {
        rec: 'panel',
        tone: 'warn',
        dead: false,
        text: form.check_all_ips
          ? t('Открывается, но из {n} адресов домена с ошибкой отвечают {bad}: часть посетителей видит ошибку. Включено "проверять все адреса", поэтому монитор будет красным, пока их не починят. Не нужно - снимите галочку ниже.', v)
          : t('Открывается, но из {n} адресов домена с ошибкой отвечают {bad}: часть посетителей видит ошибку. Монитор этого не заметит, пока не включить "проверять все адреса" ниже.', v),
      }
    }
    if (panelWorks) {
      let text =
        panel.code === 401
          ? t('Сайт отвечает, но просит вход (401). Сейчас 401 считается нормой: монитор следит только, что сайт жив. Чтобы проверять с входом, выберите "дать доступ".')
          : panel.status === 'degraded'
            ? t('Открывается, но {msg}. Лучше всего проверять с панели: так его видят посетители.', { msg })
            : t('Открывается с панели. Лучше всего проверять с панели: так его видят посетители.')
      if (locBad.length) text += ' ' + t('Из локаций {names} не открывается.', { names: locBad.map((l) => l.name).join(', ') })
      return { rec: 'panel', tone: panel.code === 401 || panel.status === 'degraded' ? 'warn' : 'ok', text, dead: false }
    }
    if (works(inside))
      return {
        rec: 'local',
        tone: 'warn',
        dead: false,
        text: t('С панели не открывается ({msg}), а изнутри {srv} работает. Проверять изнутри сервера: агент {srv} откроет сайт на localhost с этим именем. Такой монитор скажет, что сайт жив, но не скажет, видят ли его посетители.', { msg, srv }),
      }
    if (inside?.status === 'pending')
      return { ...none, text: t('С панели не открывается ({msg}). Жду ответ изнутри {srv}...', { msg, srv }) }
    if (locOk.length)
      return {
        rec: '',
        tone: 'bad',
        dead: false,
        text: t('С панели не открывается ({msg}), хотя из локаций {names} открывается: похоже, панель не пускают (белый список или блокировка). Монитор с панели будет красным: пустите адрес панели или проверяйте изнутри сервера, если сайт на сервере с агентом.', { msg, names: locOk.map((l) => l.name).join(', ') }),
      }
    if (tried.locs?.st === 'wait' || tried.inside?.st === 'wait') return { ...none, text: t('С панели не открывается ({msg}). Проверяю остальное...', { msg }) }
    return {
      rec: '',
      tone: 'bad',
      dead: true,
      text: editing
        ? t('Сейчас адрес не открывается ниоткуда: {msg}. Проверьте адрес, а если сайт на обслуживании, монитор можно выключить ниже.', { msg })
        : t('Сейчас адрес не открывается ниоткуда: {msg}. Если добавить так, монитор сразу будет красным и придет алерт. Проверьте адрес, а если сайт только поднимается, можно добавить выключенным.', { msg }),
    }
  }
  // DNS: что ответил резолвер и что с этим сделает монитор
  function assessDns(p: TryPanel): { rec: Where | ''; tone: string; text: string; dead: boolean } {
    const msg = p.message
    const want = !!(form.dns_expect ?? '').trim()
    if (tried && tried.dns !== dnsKey(form))
      return { rec: '', tone: 'warn', dead: false, text: t('Настройки поменялись - нажмите "проверить снова": ответ сравнится уже с ними.') }
    if (p.status !== 'down') {
      const text = want
        ? t('Ответ совпадает с ожидаемым. Станет другим - придет алерт.')
        : p.baseline === 'kept'
          ? t('Ответ тот же, что монитор запомнил. Изменится - придет алерт.')
          : editing
            ? t('Монитор запомнит этот ответ и пришлет алерт, когда он изменится.')
            : t('Монитор запомнит этот ответ и пришлет алерт, когда он изменится. Если ответ должен быть другим, задайте его ниже.')
      return { rec: '', tone: 'ok', dead: false, text }
    }
    if (!(p.answers ?? []).length)
      return {
        rec: '',
        tone: 'bad',
        dead: true,
        text: editing
          ? t('DNS сейчас не дает ответа: {msg}. Проверьте имя и тип записи, а если так и задумано, монитор можно выключить ниже.', { msg })
          : t('DNS сейчас не дает ответа: {msg}. Если добавить так, монитор сразу будет красным и придет алерт. Проверьте имя и тип записи.', { msg }),
      }
    if (want)
      return {
        rec: '',
        tone: 'bad',
        dead: true,
        text: editing
          ? t('Ответ не тот, что ждем: {msg}. Монитор будет красным, пока ответ не станет таким.', { msg })
          : t('Ответ не тот, что ждем: {msg}. Если добавить так, монитор сразу будет красным и придет алерт.', { msg }),
      }
    return {
      rec: '',
      tone: 'warn',
      dead: false,
      text: t('{msg}. Монитор пришлет алерт; если новый ответ правильный, примите его кнопкой в карточке монитора.', {
        msg: msg.charAt(0).toUpperCase() + msg.slice(1),
      }),
    }
  }
  const verdict = assess()

  // выбор по итогам проверки, пока человек не выбрал сам
  useEffect(() => {
    if (!whereTouched && verdict.rec) setWhere(verdict.rec)
  }, [verdict.rec, whereTouched])
  // проверять все адреса - когда один из них уже сейчас отвечает ошибкой
  useEffect(() => {
    if (panel && !editing) set({ check_all_ips: ipBad.length > 0 && !!ips && ipBad.length < ips.length })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [panel])
  // сайт просит вход: по умолчанию 401 - норма, монитор следит, что сайт жив
  useEffect(() => {
    if (has401) setAuthSeen(true)
    if (has401 && !editing && auth === 'ok401' && !statusOk(401, expected)) set({ expected_status: '200-399,401' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [has401])

  const chooseAuth = (v: 'ok401' | 'give') => {
    setAuth(v)
    if (v === 'ok401') set({ expected_status: '200-399,401' })
    else set({ expected_status: '200-399' })
  }

  const autoName = isPush ? '' : isDns ? (cur.target ? `DNS ${cur.target}${dnsType !== 'A' ? ` ${dnsType}` : ''}` : '') : displayHost(cur)
  const shownName = name ?? autoName
  const shownGroup = group ?? (ctx?.group || '')
  const interval = form.interval_seconds ?? (isDns ? 300 : 60)
  // длительность, как ее пишет алерт push (checks._dur): 5 мин, 1 ч 30 мин, 2 дн
  const dur = (sec: number) => {
    if (sec < 3600) return t('{n} мин', { n: Math.max(Math.floor(sec / 60), 1) })
    if (sec < 2 * 86400) {
      const h = Math.floor(sec / 3600)
      const m = Math.floor(sec / 60) % 60
      return m && h < 6 ? t('{h} ч {m} мин', { h, m }) : t('{n} ч', { n: h })
    }
    return t('{n} дн', { n: Math.floor(sec / 86400) })
  }
  const period = Math.max(form.push_period || 3600, 60)
  const grace = pushGrace(form)
  const presetFails = (p: Preset) =>
    p === 'n1' ? 1 : p === 'n3' ? 3 : p === 'm5' ? Math.max(1, Math.round(300 / interval)) : ownFails
  const presetLabel: Record<Preset, string> = {
    n1: t('сразу'),
    n3: t('после 3 сбоев подряд'),
    m5: t('если лежит 5 минут'),
    own: t('после {n} сбоев подряд', { n: ownFails }),
  }
  const presetHint: Record<Preset, string> = {
    n1: t('после первой неудачной проверки'),
    n3: t('около {n} мин: единичный сбой не будит', { n: Math.max(1, Math.round((3 * interval) / 60)) }),
    m5: t('для того, что иногда моргает'),
    own: t('так настроено у этого монитора'),
  }
  // свой порог показываем, только если он уже стоит у монитора; "5 минут" - если это не те же
  // 1 или 3 сбоя (у DNS проверка и так раз в 5 минут)
  const presets: Preset[] = (['n1', 'n3', 'm5', 'own'] as Preset[]).filter((p) =>
    p === 'm5' ? ![1, 3].includes(presetFails('m5')) : p === 'own' ? editing && !!ownFails && presetOfBase() === 'own' : true,
  )
  function presetOfBase(): Preset {
    const every = base.interval_seconds ?? 60
    return ownFails === 1 ? 'n1' : ownFails === 3 ? 'n3' : ownFails === Math.max(1, Math.round(300 / every)) ? 'm5' : 'own'
  }
  const whereLabel =
    where === 'local'
      ? t('изнутри {srv}', { srv: srv || t('сервера') })
      : where === 'locations'
        ? t('с панели и из локаций')
        : t('с панели')
  const everyLabel = interval % 60 === 0 ? t('раз в {n} мин', { n: interval / 60 }) : t('раз в {n} с', { n: interval })
  // push проверять нечего: хватит названия
  const canAdd =
    editing || isPush
      ? (isPush || !!cur.target) && shownName.trim() !== '' && !busy
      : fresh && !panelPending && tried.panel.st !== 'err' && shownName.trim() !== '' && !busy
  const hdr = headersJson(hdrText)

  const submit = () => {
    const out: CheckForm = {
      ...form,
      type: cur.type,
      target: cur.target,
      port: cur.port,
      name: shownName.trim(),
      group_name: shownGroup.trim(),
      probe_local: where === 'local',
      check_locations: where === 'locations',
      alert_after_failures: presetFails(preset),
      enabled: editing ? form.enabled ?? true : !(verdict.dead && addOff),
    }
    if (!http) Object.assign(out, { check_ssl: false, check_domain: false, check_all_ips: false, extra_paths: [] })
    if (isDns || isPush) Object.assign(out, { probe_local: false, check_locations: false })
    if (isDns) out.dns_type = dnsType
    // у push адреса нет: панель выдаст свой; алерт - с первого же пропуска
    if (isPush) Object.assign(out, { target: 'push', push_period: period, alert_after_failures: 1 })
    onSubmit(out)
  }

  const switchKind = (k: Kind) => {
    runRef.current++
    setTried(null)
    setKind(k)
    // DNS меняется редко: раз в 5 минут хватает, а резолверы не любят частых вопросов
    if (k === 'dns') {
      set({ dns_type: form.dns_type || 'A', interval_seconds: Math.max(form.interval_seconds ?? 60, 300) })
      setAddr((v) => dnsName(v))
    }
    if (k === 'addr' && (form.interval_seconds ?? 60) === 300) set({ interval_seconds: 60 })
    if (k === 'push') set({ push_period: form.push_period || 3600 })
  }
  const kindLabel: Record<Kind, string> = {
    addr: t('сайт или порт'),
    dns: t('DNS-записи'),
    push: t('сигнал от скрипта'),
  }

  // ---- куски разметки ----
  const dot = (s: string) => <span className={`sdot sdot-${s === 'none' ? 'unknown' : s}`} />
  const spin = (text: string) => (
    <span className="muted">
      <span className="run-spin" /> {text}
    </span>
  )
  // код, объявленный нормой, показываем кодом: текст проверки писал его как ошибку
  const resLine = (r: TryResult) => (
    <span className="am-res" title={r.message}>
      {dot(works(r) ? (r.status === 'down' ? 'degraded' : r.status) : 'down')}
      <span>
        {r.status === 'down' && works(r)
          ? r.code === 401
            ? t('просит вход (HTTP 401)')
            : `HTTP ${r.code}`
          : r.message}
      </span>
    </span>
  )
  const panelLine = (p: TryPanel) =>
    ips && ipBad.length ? (
      <span className="am-res" title={p.message}>
        {dot(ipBad.length < ips.length ? 'degraded' : 'down')}
        <span>{t('{ok} из {n} адресов отвечают', { ok: ips.length - ipBad.length, n: ips.length })}</span>
      </span>
    ) : (
      resLine(p)
    )
  // результата по этому адресу еще нет: проверка идет или адрес поменяли после нее
  const notYet = () => (stale ? <span className="muted">{t('не проверено: адрес поменялся')}</span> : spin(t('проверяю...')))
  const insideLine = () => {
    if (!fresh) return notYet()
    const s = tried?.inside
    if (!s || s.st === 'wait') return spin(t('проверяю...'))
    if (s.st === 'err') return <span className="muted">{s.msg}</span>
    const v = s.v
    if (v.status === 'pending') return spin(t('жду агента {srv}...', { srv }))
    if (v.kind === 'no_server') return <span className="muted">{t('недоступно')}</span>
    if (v.kind === 'offline') return <span className="am-res">{dot('down')}<span>{t('сервер не на связи')}</span></span>
    if (v.kind === 'no_answer')
      return <span className="am-res">{dot('down')}<span>{t('агент не ответил за {n} с', { n: v.deadline })}</span></span>
    return resLine(v)
  }
  const locLine = () => {
    if (!fresh) return notYet()
    const s = tried?.locs
    if (!s || s.st === 'wait') return spin(t('проверяю...'))
    if (s.st === 'err') return <span className="muted">{s.msg}</span>
    if (!s.v.length) return <span className="muted">{t('Локаций пока нет — добавьте в ⚙ → Локации.')}</span>
    return (
      <span className="am-res">
        {dot(locBad.length === 0 ? 'up' : locOk.length ? 'degraded' : 'down')}
        <span>
          {t('открывается из {ok} из {n}', { ok: locOk.length, n: s.v.length })}
          {locBad.length > 0 && (
            <span className="muted">
              {' · '}
              {t('нет: {list}', { list: locBad.map((l) => `${l.name} (${l.message})`).join(', ') })}
            </span>
          )}
        </span>
      </span>
    )
  }

  type Opt = { k: Where; title: string; desc: string; sub?: string; line: ReactNode; off: boolean }
  const opts: Opt[] = [
    {
      k: 'panel',
      title: t('С панели'),
      desc: http
        ? t('Из интернета, как посетитель. Подходит почти всем сайтам.')
        : t('Подключение к порту из интернета, с самой панели.'),
      line: !fresh ? notYet() : tried.panel.st === 'wait' ? spin(t('проверяю...')) : tried.panel.st === 'err' ? <span className="t-down">{tried.panel.msg}</span> : panel ? panelLine(panel) : null,
      off: false,
    },
    {
      k: 'local',
      title: srv ? t('Изнутри {srv}', { srv }) : t('Изнутри сервера'),
      desc: !http
        ? t('Изнутри сервера проверяются только сайты.')
        : inside?.kind === 'no_server'
          ? t('Домен не нашелся на серверах с агентом: проверять изнутри некому.')
          : t('Агент откроет сайт на самом сервере, через localhost. Для сайтов за белым списком.'),
      sub: inside?.web && srv ? t('домен есть в {web} на {srv}', { web: inside.web, srv }) : undefined,
      line: http ? insideLine() : <span className="muted">{t('недоступно')}</span>,
      off: !http || (fresh && !!inside && !insideKnown) || tried?.inside?.st === 'err',
    },
    {
      k: 'locations',
      title: t('Из локаций'),
      desc: http
        ? t('Как с панели, и еще через прокси из других стран и сетей: видно, если сайт не открывается из какой-то сети.')
        : t('Через локации проверяются только сайты.'),
      line: http ? locLine() : <span className="muted">{t('недоступно')}</span>,
      off: !http || (locs !== undefined && locs.length === 0),
    },
  ]

  // ---- предпросмотр алерта ----
  const chosen: TryResult | undefined = where === 'local' ? inside : panel
  // сервер не на связи или агент молчит - ответа сайта нет, и пример алерта обычный
  const answered = !!chosen && ['up', 'degraded', 'down'].includes(chosen.status) && !!chosen.message
  const failingNow = answered && (where === 'local' ? !works(chosen) : !panelWorks)
  // у push адреса нет: в заголовке алерта его имя (collector)
  const host = isPush ? shownName || t('название монитора') : isDns ? cur.target : displayHost(cur)
  const ments = (shownName.match(/@[A-Za-z0-9_]+/g) ?? []).join(' ')
  const sample = isPush
    ? t('нет сигнала {dur}, ждали каждые {every}', { dur: dur(period + grace), every: dur(period) })
    : failingNow
      ? chosen.message
      : http
        ? t('сервер не ответил на попытку соединения (ConnectTimeout)')
        : isDns
          ? t('записей {type} нет', { type: dnsType })
          : t('порт {port}: недоступен', { port: cur.port })
  const rule = rules?.rules?.down
  const dflt = rules?.kinds.find((k) => k.key === 'down')?.default_text
  const custom = !!rule && !!dflt && rule.text !== dflt && !rule.text.startsWith('🔴 Kervax:')
  const ruleOff =
    !!rule &&
    (!rule.enabled ||
      rule.scope_type === 'checks' ||
      (rule.scope_type === 'groups' && !rule.scope.includes(shownGroup.trim())))
  const locPart = where === 'locations' && panelWorks && locBad.length > 0
  const bubble = custom ? (
    <div className="am-pre">
      {rule.text
        .replace(/\{name\}/g, shownName)
        .replace(/\{group\}/g, shownGroup)
        .replace(/\{message\}/g, sample)
        .replace(/\{status\}/g, 'down')}
    </div>
  ) : locPart ? (
    <div className="am-pre">
      {'🌍 '}
      <span className="am-a">{host}</span>
      {' · '}
      <span className="am-a">{t('монитор')}</span>
      {ments && ` · ${ments}`}
      {locOk.map((l) => `\n🟢 ${l.name} — ${t('доступен')}`).join('')}
      {locBad.map((l) => `\n🔴 ${l.name} — ${t('недоступен')}`).join('')}
    </div>
  ) : (
    <div>
      {'🔴 '}
      <span className={http ? 'am-a' : ''}>{host}</span>
      {` — ${sample} · `}
      <span className="am-a">{t('монитор')}</span>
      {ments && ` · ${ments}`}
    </div>
  )
  const off = editing ? form.enabled === false : verdict.dead && addOff
  const bubbleNote = off
    ? editing
      ? t('Монитор выключен: алертов не будет, пока его не включат.')
      : t('Монитор добавится выключенным: алертов не будет, пока его не включат.')
    : ruleOff
      ? t('По правилам алертов этот монитор алерт о падении не шлет.')
      : isPush
        ? t('Так придет алерт, если скрипт не постучится вовремя. Сообщит об ошибке - придет сразу, с ее текстом.')
        : isDns
          ? failingNow
            ? editing
              ? t('Так придет после сохранения: ответ DNS сейчас не тот.')
              : t('Так придет сразу после добавления: ответ DNS сейчас не тот.')
            : t('Так придет алерт, если ответ DNS изменится или пропадет.')
          : failingNow || locPart
            ? editing
              ? t('Так придет после сохранения: адрес сейчас не открывается.')
              : t('Так придет сразу после добавления: адрес сейчас не открывается.')
            : t('Так придет алерт, если адрес перестанет открываться.')

  // выбор локаций: null = все, [] = ни одной, [id...] = эти
  const proxyLocs = allLocs.filter((l) => l.enabled)
  const locIds = form.location_ids
  const locOn = (id: number) => locIds == null || locIds.includes(id)
  const toggleLoc = (id: number) => {
    const ids = locIds == null ? proxyLocs.map((l) => l.id) : locIds
    set({ location_ids: locOn(id) ? ids.filter((x) => x !== id) : [...ids, id] })
  }
  const parseDays = (v: string) => v.split(/[^0-9]+/).filter(Boolean).map(Number)

  const existingNote = ctx?.existing_id != null && (
    <div className="am-note warn">
      {isDns
        ? editing
          ? t('Эту запись смотрит еще монитор "{name}": оба шлют те же алерты.', { name: ctx.existing_name })
          : t('На эту запись уже есть монитор "{name}": второй будет слать те же алерты.', { name: ctx.existing_name })
        : editing
          ? t('Этот адрес смотрит еще монитор "{name}": оба шлют те же алерты.', { name: ctx.existing_name })
          : t('На этот адрес уже есть монитор "{name}": второй будет слать те же алерты.', { name: ctx.existing_name })}{' '}
      {onOpenCheck && (
        <button type="button" className="am-link" onClick={() => onOpenCheck(ctx.existing_id as number)}>
          {t('открыть его')}
        </button>
      )}
    </div>
  )

  // ---- push: как часто ждать сигнал ----
  const pushSec = (
    <div className="am-sec">
      <span className="am-lab">{t('Сигнал не реже, чем раз в')}</span>
      <div className="win-switch">
        {PUSH_PERIODS.map(([v, label]) => (
          <button
            type="button"
            key={v}
            className={`win-btn${!ownPeriod && period === v ? ' win-btn-active' : ''}`}
            onClick={() => {
              setOwnPeriod(false)
              set({ push_period: v })
            }}
          >
            {t(label)}
          </button>
        ))}
        <button type="button" className={`win-btn${ownPeriod ? ' win-btn-active' : ''}`} onClick={() => setOwnPeriod(true)}>
          {t('свой')}
        </button>
      </div>
      {ownPeriod && (
        <label className={'field am-period' + bad('push_period')}>
          <span>{t('Период, минут')}</span>
          <input
            type="number"
            min={1}
            value={Math.round(period / 60)}
            onChange={(e) => set({ push_period: Math.max(1, Number(e.target.value) || 1) * 60 })}
          />
        </label>
      )}
      <div className="muted small am-hint">
        {t('Алерт, если сигнала нет дольше {dur} или скрипт сообщил об ошибке.', { dur: dur(period + grace) })}{' '}
        {!editing && t('Адрес для скрипта появится в карточке монитора после сохранения.')}
      </div>
    </div>
  )

  // ---- DNS: что отвечает сейчас и что ждать ----
  const answers = panel?.answers ?? []
  const wantText = form.dns_expect ?? ''
  const dnsSec = (
    <div className="am-sec">
      <h4>{t('Что отвечает DNS')}</h4>
      {!fresh ? (
        notYet()
      ) : tried.panel.st === 'wait' ? (
        spin(t('проверяю...'))
      ) : answers.length > 0 ? (
        <div className="am-ips">
          {answers.map((a) => (
            <div key={a} className="am-res">
              {dot(panel?.status === 'down' ? 'down' : 'up')}
              <span className="mono">{a}</span>
            </div>
          ))}
        </div>
      ) : null}
      {verdict.text && <div className={`am-note ${verdict.tone}`}>{verdict.text}</div>}
      {existingNote}
      {wantOpen ? (
        <div className="am-box">
          <label className={'field' + bad('dns_expect')}>
            <span>{t('Ожидаемый ответ, по строке')}</span>
            <textarea
              className="mono"
              rows={Math.min(Math.max(wantText.split('\n').length, 2), 6)}
              value={wantText}
              placeholder={dnsType === 'MX' ? 'mx1.example.com\nmx2.example.com' : '203.0.113.10'}
              spellCheck={false}
              onChange={(e) => set({ dns_expect: e.target.value })}
            />
            <span className="field-hint muted small">
              {t('Пусто - запомним первый ответ и пришлем алерт, когда он изменится. У MX можно без приоритета.')}
            </span>
          </label>
          <div className="am-row">
            {wantText.trim() !== '' && (
              <div className="win-switch">
                {(['equal', 'contains'] as const).map((m) => (
                  <button
                    type="button"
                    key={m}
                    className={`win-btn${(form.dns_match ?? 'equal') === m ? ' win-btn-active' : ''}`}
                    onClick={() => set({ dns_match: m })}
                  >
                    {m === 'equal' ? t('ответ совпадает целиком') : t('ответ содержит все эти')}
                  </button>
                ))}
              </div>
            )}
            <button type="button" className="ghost" disabled={!cur.target || panelPending} onClick={() => probe()}>
              {t('проверить снова')}
            </button>
          </div>
        </div>
      ) : (
        <div className="muted small am-hint">
          <button
            type="button"
            className="am-link small"
            onClick={() => {
              setWantOpen(true)
              // чаще всего ждут ровно то, что DNS отвечает сейчас: подставляем, поправить можно
              if (answers.length && !wantText.trim()) {
                const v = answers.join('\n')
                set({ dns_expect: v })
                probe({ dns_expect: v })
              }
            }}
          >
            {t('задать ожидаемый ответ')}
          </button>{' '}
          {t('- если ответ должен быть именно таким, а не просто не меняться.')}
        </div>
      )}
    </div>
  )

  return (
    <div ref={rootRef} className={editing ? 'am am-edit' : 'card am'}>
      {!editing && <h3 className="am-h">{t('Новый монитор')}</h3>}
      <span className="am-lab">{t('Что проверять')}</span>
      {!isPush && (
        <div className="am-addr">
          <input
            className={'mono' + bad('target') + bad('port')}
            value={addr}
            autoFocus={!editing}
            placeholder={isDns ? 'example.com' : 'shop.example.com'}
            spellCheck={false}
            onChange={(e) => setAddr(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') probe()
            }}
          />
          <button type="button" disabled={!cur.target || panelPending} onClick={() => probe()}>
            {fresh ? t('Проверить снова') : t('Проверить')}
          </button>
        </div>
      )}
      <div className="am-type">
        {isDns ? (
          <div className="win-switch" title={t('Запись')}>
            {DNS_TYPES.map((x) => (
              <button
                type="button"
                key={x}
                className={`win-btn${dnsType === x ? ' win-btn-active' : ''}`}
                onClick={() => {
                  set({ dns_type: x })
                  // другая запись - другой ответ: спрашиваем сразу, если имя уже вписано
                  if (cur.target) probe({ dns_type: x })
                }}
              >
                {x}
              </button>
            ))}
          </div>
        ) : (
          (cur.target || isPush) && (
            <span className="type-chip on">
              {isPush ? t('сигнал от скрипта') : http ? t('это сайт') : t('это TCP-порт {port}', { port: cur.port })}
            </span>
          )
        )}
        <span className="muted small">
          {isPush
            ? t('Скрипт или крон сам стучится в панель после каждого прогона: бэкап, выгрузка, очередь.')
            : isDns
              ? t('Домен и какую запись спрашивать.')
              : http
                ? t('Сайт, адрес API или хост:порт.')
                : t('Нужен сайт на этом порту - допишите https:// в начале.')}{' '}
          {!editing && (
            <>
              {t('Нужно другое:')}{' '}
              {(['addr', 'dns', 'push'] as Kind[])
                .filter((k) => k !== kind)
                .map((k, i) => (
                  <span key={k}>
                    {i > 0 && ', '}
                    <button type="button" className="am-link" onClick={() => switchKind(k)}>
                      {kindLabel[k]}
                    </button>
                  </span>
                ))}
            </>
          )}
        </span>
      </div>

      {!isPush && !fresh && (!editing || stale) && (
        <div className="am-note">
          {tried
            ? isDns
              ? t('Имя поменялось - проверьте снова.')
              : t('Адрес поменялся - проверьте снова.')
            : isDns
              ? t('Нажмите "Проверить": панель спросит запись у DNS и покажет ответ еще до сохранения.')
              : t('Нажмите "Проверить": панель откроет адрес с панели, изнутри сервера, где нашелся домен, и из локаций. Так видно, работает ли он, еще до сохранения.')}
        </div>
      )}

      {(fresh || editing || isPush) && (
        <>
          {isPush && pushSec}
          {isDns && dnsSec}
          {kind === 'addr' && (
          <div className="am-sec">
            <h4>{t('Откуда проверять')}</h4>
            <div className="am-opts">
              {opts.map((o) => (
                <button
                  type="button"
                  key={o.k}
                  disabled={o.off && where !== o.k}
                  className={`am-opt${where === o.k ? ' on' : ''}${o.off && where !== o.k ? ' off' : ''}`}
                  onClick={() => {
                    setWhere(o.k)
                    setWhereTouched(true)
                  }}
                >
                  <span className="am-opt-t">
                    <span className="am-rad" />
                    {o.title}
                    {verdict.rec === o.k && <span className="am-rec">{t('лучше так')}</span>}
                  </span>
                  <span className="am-opt-d">{o.desc}</span>
                  {o.sub && <span className="am-opt-d">{o.sub}</span>}
                  <span className="am-opt-r">{o.line}</span>
                </button>
              ))}
            </div>
            {verdict.text && <div className={`am-note ${verdict.tone}`}>{verdict.text}</div>}
            {existingNote}
            {(has401 || authSeen) && http && (
              <div className="am-box">
                <div className="am-row">
                  <b className="small">{has401 ? t('Сайт просит вход') : t('Вход и заголовки')}</b>
                  <div className="win-switch">
                    <button type="button" className={`win-btn${auth === 'ok401' ? ' win-btn-active' : ''}`} onClick={() => chooseAuth('ok401')}>
                      {t('401 - норма')}
                    </button>
                    <button type="button" className={`win-btn${auth === 'give' ? ' win-btn-active' : ''}`} onClick={() => chooseAuth('give')}>
                      {t('дать доступ')}
                    </button>
                  </div>
                </div>
                {auth === 'give' && (
                  <>
                    <div className="am-grid2">
                      <label className={'field' + bad('http_headers')}>
                        <span>{t('Заголовок, по строке')}</span>
                        <textarea
                          className="mono"
                          rows={2}
                          value={hdrText}
                          placeholder="Authorization: Bearer ..."
                          spellCheck={false}
                          onChange={(e) => {
                            setHdrText(e.target.value)
                            set({ http_headers: headersJson(e.target.value).json })
                          }}
                        />
                      </label>
                      <div className="am-grid2 am-tight">
                        <label className={'field' + bad('auth_user')}>
                          <span>{t('или логин (HTTP Basic)')}</span>
                          <input
                            value={form.auth_user ?? ''}
                            autoComplete="off"
                            onChange={(e) => set({ auth_user: e.target.value, auth_method: e.target.value || form.auth_pass ? 'basic' : '' })}
                          />
                        </label>
                        <label className={'field' + bad('auth_pass')}>
                          <span>{t('Пароль')}</span>
                          <input
                            type="password"
                            value={form.auth_pass ?? ''}
                            autoComplete="new-password"
                            onChange={(e) => set({ auth_pass: e.target.value, auth_method: e.target.value || form.auth_user ? 'basic' : '' })}
                          />
                        </label>
                      </div>
                    </div>
                    {hdr.bad.length > 0 && (
                      <div className="form-error small">{t('Заголовок пишется как "Имя: значение": {list}', { list: hdr.bad.join(', ') })}</div>
                    )}
                    <div className="am-row">
                      <button type="button" className="ghost" disabled={panelPending} onClick={() => probe()}>
                        {t('проверить снова')}
                      </button>
                      <span className="muted small">{t('в алерты не попадает, наблюдатели его не видят')}</span>
                    </div>
                  </>
                )}
              </div>
            )}
            {ips && ipBad.length > 0 && ipBad.length < ips.length && (
              <div className="am-ips">
                {ips.map((x) => (
                  <div key={x.ip} className="am-res">
                    {dot(ipWorks(x) ? (x.status === 'down' ? 'degraded' : x.status) : 'down')}
                    <span className="mono">{x.ip}</span>
                    <span className="muted">{x.message}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
          )}

          {(http || verdict.dead || editing) && (
            <div className="am-sec">
              <h4>{t('Что еще проверить')}</h4>
              <div className="am-cks">
                {http && (
                  <label className="am-ck">
                    <input type="checkbox" checked={form.check_ssl ?? true} onChange={(e) => set({ check_ssl: e.target.checked })} />
                    <span>
                      {t('Следить за сертификатом')}{' '}
                      {fresh && tried.exp?.st === 'wait' ? (
                        <span className="run-spin" />
                      ) : exp?.ssl_days != null ? (
                        <span className={exp.ssl_days < 14 ? 't-degraded' : 'muted'}>{t('сейчас {n} дн.', { n: exp.ssl_days })}</span>
                      ) : (
                        exp?.ssl_message && <span className="muted">{exp.ssl_message}</span>
                      )}
                      <small>{t('напомню за {days} дн.', { days: (form.ssl_warn_days ?? EXPIRY_WARN_DAYS).join(', ') })}</small>
                    </span>
                  </label>
                )}
                {http && (
                  <label className="am-ck">
                    <input type="checkbox" checked={form.check_domain ?? true} onChange={(e) => set({ check_domain: e.target.checked })} />
                    <span>
                      {t('Следить за сроком домена')}{' '}
                      {fresh && tried.exp?.st === 'wait' ? (
                        <span className="run-spin" />
                      ) : exp?.domain_days != null ? (
                        <span className={exp.domain_days < 30 ? 't-degraded' : 'muted'}>{t('сейчас {n} дн.', { n: exp.domain_days })}</span>
                      ) : (
                        exp?.domain_message && <span className="muted">{exp.domain_message}</span>
                      )}
                      <small>{t('напомню за {days} дн.', { days: (form.domain_warn_days ?? EXPIRY_WARN_DAYS).join(', ') })}</small>
                    </span>
                  </label>
                )}
                {(ips || (editing && http && form.check_all_ips)) && (
                  <label className="am-ck">
                    <input type="checkbox" checked={form.check_all_ips ?? false} onChange={(e) => set({ check_all_ips: e.target.checked })} />
                    <span>
                      {ips ? t('Проверять все адреса домена ({n})', { n: ips.length }) : t('Проверять все адреса домена')}
                      <small>{t('иначе мертвый адрес за балансировщиком не видно')}</small>
                    </span>
                  </label>
                )}
                {editing && (
                  <label className="am-ck">
                    <input type="checkbox" checked={form.enabled ?? true} onChange={(e) => set({ enabled: e.target.checked })} />
                    <span>
                      {t('Монитор включен')}
                      <small>{t('выключенный не проверяется и не шлет алертов')}</small>
                    </span>
                  </label>
                )}
                {verdict.dead && !editing && (
                  <label className="am-ck">
                    <input type="checkbox" checked={addOff} onChange={(e) => setAddOff(e.target.checked)} />
                    <span>
                      {t('Добавить выключенным')}
                      <small>{t('включите, когда заработает')}</small>
                    </span>
                  </label>
                )}
              </div>
            </div>
          )}

          <div className="am-sec am-grid2">
            <label className={'field' + bad('name')}>
              <span>{t('Название')}</span>
              <input
                value={shownName}
                autoFocus={isPush && !editing}
                placeholder={isPush ? t('напр. ночной бэкап') : undefined}
                onChange={(e) => setName(e.target.value)}
              />
              <span className="field-hint muted small">
                {t('Название попадает в текст алерта: @упоминания в нём тегнут людей в Telegram.')}
              </span>
            </label>
            <label className={'field' + bad('group_name')}>
              <span>{t('Группа')}</span>
              <input
                value={shownGroup}
                placeholder={t('без группы')}
                list="kervax-am-groups"
                onChange={(e) => setGroup(e.target.value)}
              />
              <datalist id="kervax-am-groups">
                {groups.map((g) => (
                  <option key={g} value={g} />
                ))}
              </datalist>
            </label>
          </div>

          {!isPush && (
          <div className="am-sec">
            <span className="am-lab">{t('Слать алерт')}</span>
            <div className="win-switch">
              {presets.map((p) => (
                <button
                  type="button"
                  key={p}
                  className={`win-btn${preset === p ? ' win-btn-active' : ''}`}
                  onClick={() => setPreset(p)}
                >
                  {presetLabel[p]}
                </button>
              ))}
            </div>
            <div className="muted small am-hint">{presetHint[preset]}</div>
          </div>
          )}

          {!adv ? (
            <div className="am-sec">
              <button type="button" className="am-link small" onClick={() => setAdv(true)}>
                {t('тонкая настройка')}
              </button>{' '}
              <span className="muted small">
                {isPush
                  ? editing
                    ? t('- допуск по времени, заглушенные алерты.')
                    : t('- допуск по времени. Можно поменять и потом в карточке монитора.')
                  : isDns
                    ? editing
                      ? t('- интервал, DNS-сервер, заглушенные алерты.')
                      : t('- интервал и DNS-сервер. Можно поменять и потом в карточке монитора.')
                    : editing
                      ? http
                        ? t('- интервал, ожидаемый ответ, пути, заголовки, повторы, локации, заглушенные алерты.')
                        : t('- интервал, порог "медленно", повторы, заглушенные алерты.')
                      : http
                        ? t('- интервал, ожидаемый ответ, пути, заголовки. Все это можно поменять и потом в карточке монитора.')
                        : t('- интервал и порог "медленно". Можно поменять и потом в карточке монитора.')}
              </span>
            </div>
          ) : (
            <div className="am-box am-adv">
              <div className="am-row">
                <b className="small am-grow">{t('Тонкая настройка')}</b>
                <button type="button" className="am-link" onClick={() => setAdv(false)}>
                  {t('свернуть')}
                </button>
              </div>
              {isPush && (
                <label className={'field' + bad('push_grace')}>
                  <span>{t('Допуск, минут (0 = десятая часть периода)')}</span>
                  <input
                    type="number"
                    min={0}
                    value={Math.round((form.push_grace || 0) / 60)}
                    onChange={(e) => set({ push_grace: Math.max(0, Number(e.target.value) || 0) * 60 })}
                  />
                  <span className="field-hint muted small">
                    {t('сейчас {dur}: крон стартует не в нулевую секунду, а скрипт идет не мгновенно', { dur: dur(grace) })}
                  </span>
                </label>
              )}
              {isDns && (
                <div className="form-grid">
                  <label className={'field' + bad('interval_seconds')}>
                    <span>{t('Интервал, сек')}</span>
                    <input
                      type="number"
                      value={interval}
                      onChange={(e) => set({ interval_seconds: Number(e.target.value) || 0 })}
                    />
                  </label>
                  <label className={'field' + bad('dns_server')}>
                    <span>{t('DNS-сервер (IP)')}</span>
                    <input
                      className="mono"
                      value={form.dns_server ?? ''}
                      placeholder={t('системный')}
                      spellCheck={false}
                      onChange={(e) => set({ dns_server: e.target.value })}
                    />
                  </label>
                </div>
              )}
              {kind === 'addr' && (
              <div className="form-grid">
                <label className={'field' + bad('interval_seconds')}>
                  <span>{t('Интервал, сек')}</span>
                  <input
                    type="number"
                    value={interval}
                    onChange={(e) => set({ interval_seconds: Number(e.target.value) || 0 })}
                  />
                </label>
                <label className={'field' + bad('degraded_ms')}>
                  <span>{t('Порог «медленно», мс')}</span>
                  <input
                    type="number"
                    value={form.degraded_ms ?? 2000}
                    onChange={(e) => set({ degraded_ms: Number(e.target.value) || 0 })}
                  />
                </label>
                <label className={'field' + bad('retries')}>
                  <span>{t('Повторы при сбое')}</span>
                  <input
                    type="number"
                    value={form.retries ?? 3}
                    onChange={(e) => set({ retries: Number(e.target.value) || 0 })}
                    title={t('Считать «упавшим» и слать алерт, только если провалятся все попытки подряд.')}
                  />
                </label>
                <label className={'field' + bad('degraded_after_failures')}>
                  <span>{t('Алерт «медленно» после N подряд')}</span>
                  <input
                    type="number"
                    value={form.degraded_after_failures ?? 10}
                    onChange={(e) => set({ degraded_after_failures: Number(e.target.value) || 0 })}
                    title={t('Отдельный, обычно больший порог для деградации (медленных ответов) — она шумнее падения.')}
                  />
                </label>
                {http && (
                  <>
                    <label className={'field' + bad('expected_status')}>
                      <span>{t('Ожидаемый статус')}</span>
                      <input
                        value={form.expected_status ?? ''}
                        placeholder="200-399"
                        onChange={(e) => set({ expected_status: e.target.value })}
                      />
                    </label>
                    <label className={'field' + bad('keyword_up')}>
                      <span>{t('Слово в ответе (должно быть)')}</span>
                      <input value={form.keyword_up ?? ''} onChange={(e) => set({ keyword_up: e.target.value })} />
                    </label>
                    <label className={'field' + bad('keyword_down')}>
                      <span>{t('Слово-стоп (не должно быть)')}</span>
                      <input value={form.keyword_down ?? ''} onChange={(e) => set({ keyword_down: e.target.value })} />
                    </label>
                    <label className={'field' + bad('ssl_warn_days')}>
                      <span>{t('Сертификат: напоминать за, дн.')}</span>
                      <input
                        value={sslTxt}
                        placeholder="14, 7, 1"
                        onChange={(e) => {
                          setSslTxt(e.target.value)
                          set({ ssl_warn_days: parseDays(e.target.value) })
                        }}
                      />
                    </label>
                    <label className={'field' + bad('domain_warn_days')}>
                      <span>{t('Домен: напоминать за, дн.')}</span>
                      <input
                        value={domTxt}
                        placeholder="30, 7, 1"
                        onChange={(e) => {
                          setDomTxt(e.target.value)
                          set({ domain_warn_days: parseDays(e.target.value) })
                        }}
                      />
                    </label>
                  </>
                )}
              </div>
              )}
              {http && where === 'locations' && proxyLocs.length > 0 && (
                <div className="am-locs">
                  <span className="am-lab">{t('Из каких локаций проверять:')}</span>
                  {proxyLocs.map((l) => (
                    <label className="am-ck" key={l.id}>
                      <input type="checkbox" checked={locOn(l.id)} onChange={() => toggleLoc(l.id)} />
                      <span>
                        {l.name}
                        {!l.url && <span className="type-chip">{t('напрямую')}</span>}
                      </span>
                    </label>
                  ))}
                </div>
              )}
              {http && (
                <div className="am-grid2">
                  <label className={'field' + bad('extra_paths')}>
                    <span>{t('Дополнительные пути')}</span>
                    <textarea
                      className="mono"
                      rows={2}
                      value={pathsText}
                      placeholder="/health"
                      spellCheck={false}
                      onChange={(e) => {
                        setPathsText(e.target.value)
                        set({ extra_paths: e.target.value.split('\n').map((x) => x.trim()).filter(Boolean) })
                      }}
                    />
                  </label>
                  {!((has401 || authSeen) && auth === 'give') && (
                    <label className={'field' + bad('http_headers')}>
                      <span>{t('Заголовок, по строке')}</span>
                      <textarea
                        className="mono"
                        rows={2}
                        value={hdrText}
                        placeholder="X-Token: ..."
                        spellCheck={false}
                        onChange={(e) => {
                          setHdrText(e.target.value)
                          set({ http_headers: headersJson(e.target.value).json })
                        }}
                      />
                    </label>
                  )}
                </div>
              )}
              {http && (
                <label className="am-ck">
                  <input type="checkbox" checked={form.ignore_tls ?? false} onChange={(e) => set({ ignore_tls: e.target.checked })} />
                  <span>
                    {t('Не проверять сертификат')}
                    <small>{t('самоподписанный или выпущен на другое имя')}</small>
                  </span>
                </label>
              )}
              {editing && (
                <div className="am-mutes">
                  <span className="muted small">{t('Не слать алерты этого монитора:')}</span>
                  <div className="mute-chips">
                    {MON_ALERT_KINDS.map(({ k, label }) => {
                      const mutes = form.alert_mutes ?? []
                      const muted = mutes.includes(k)
                      return (
                        <button
                          key={k}
                          type="button"
                          className={`mute-chip${muted ? ' mute-chip-on' : ''}`}
                          onClick={() => set({ alert_mutes: muted ? mutes.filter((x) => x !== k) : [...mutes, k] })}
                        >
                          {muted ? '🔕 ' : ''}
                          {t(label)}
                        </button>
                      )
                    })}
                  </div>
                </div>
              )}
              {!isPush && (
                <div className="muted small">{t('После правок нажмите "Проверить снова": проверка пойдет уже с ними.')}</div>
              )}
            </div>
          )}

          <div className="am-sec">
            <h4>{t('Так придет алерт')}</h4>
            <div className="am-bubble">
              <div className="am-who">Kervax</div>
              {bubble}
            </div>
            <div className="muted small am-hint">{bubbleNote}</div>
          </div>
        </>
      )}

      <div className="am-foot">
        <div className="am-sum">
          {isPush ? (
            shownName.trim() ? (
              <>
                {t('Будет:')} <b>{shownName}</b>, {t('ждать сигнал')} <b>{t('раз в {every}', { every: dur(period) })}</b>,{' '}
                {t('алерт')} <b>{t('если его нет {dur}', { dur: dur(period + grace) })}</b>
                {off && <>, <b>{t('выключенным')}</b></>}
              </>
            ) : (
              t('Назовите монитор: название будет в алерте.')
            )
          ) : fresh && !panelPending ? (
            <>
              {t('Будет:')} <b>{shownName}</b>, {t('проверять')}{' '}
              <b>{isDns ? t('запись {type}', { type: dnsType }) : whereLabel}</b> {everyLabel},{' '}
              {t('алерт')} <b>{presetLabel[preset]}</b>
              {off && <>, <b>{t('выключенным')}</b></>}
            </>
          ) : (
            t('Сначала проверка, потом сохранение.')
          )}
          {error && <div className="form-error">{error}</div>}
        </div>
        <button type="button" className="ghost" onClick={onCancel}>
          {t('Отмена')}
        </button>
        <button type="button" disabled={!canAdd} onClick={submit}>
          {busy ? t('Сохраняю...') : editing ? t('Сохранить') : t('Добавить монитор')}
        </button>
      </div>
    </div>
  )
}
