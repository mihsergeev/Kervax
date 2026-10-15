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
import { MON_ALERT_KINDS } from './checkUtils'

// Форма нового монитора: адрес, пробная проверка и выбор "откуда проверять" по ее итогам.
// Старая форма спрашивала тип, адрес и способ проверки вслепую: увидит ли панель сайт,
// человек узнавал от красного монитора и алерта. Здесь адрес проверяется до сохранения - с
// панели, изнутри сервера, где нашелся домен, и из локаций (backend app/try_probe.py), а
// рабочий способ выбран сам. Push и DNS остаются в CheckFormCard.
//
// Правка монитора - та же форма (editing): адрес и настройки берутся из монитора, проверка
// запускается сама при открытии, но сохранить можно и без нее. По итогам проверки форма в
// правке ничего не меняет сама, только подсказывает: человек открыл ее поменять что-то свое.

type Where = 'panel' | 'local' | 'locations'
type Preset = 'n1' | 'n3' | 'm5' | 'own'
type Slot<T> = { st: 'wait' } | { st: 'ok'; v: T } | { st: 'err'; msg: string }
type Addr = { type: 'http' | 'tcp_port'; target: string; port: number }
type Tried = Addr & {
  auth: string // настройки входа, с которыми шла проверка (authKey)
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
  onSubmit: (form: CheckForm) => void
  onCancel: () => void
  onClassic?: (type: 'push' | 'dns') => void
  onOpenCheck?: (id: number) => void
  onUnauthorized: () => void
}

const WAIT = { st: 'wait' } as const
const SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i

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
  if (f.type !== 'tcp_port' || !f.port) return f.target
  return f.target.includes(':') ? `[${f.target}]:${f.port}` : `${f.target}:${f.port}`
}

// тонкую настройку в правке раскрываем сразу, если в ней уже что-то свое
function advTouched(f: CheckForm, authShown: boolean): boolean {
  const days = (v: number[] | undefined, d: number[]) => (v ?? d).join(',') !== d.join(',')
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
    days(f.ssl_warn_days, [14, 7, 1]) ||
    days(f.domain_warn_days, [30, 7, 1]) ||
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
  onSubmit,
  onCancel,
  onClassic,
  onOpenCheck,
  onUnauthorized,
}: Props) {
  const { t } = useI18n()
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
  const [addOff, setAddOff] = useState(false)
  // '' - в правке: сайт просит вход, а что с этим делать, монитор еще не решил
  const [auth, setAuth] = useState<'ok401' | 'give' | ''>(() =>
    !editing ? 'ok401' : hasCreds ? 'give' : statusOk(401, base.expected_status || '200-399') ? 'ok401' : '',
  )
  // блок входа не прячем, когда с логином сайт ответил 200: там то, что человек вписал
  const [authSeen, setAuthSeen] = useState(editing && hasCreds)
  const [allLocs, setAllLocs] = useState<Location[]>([])
  const [sslTxt, setSslTxt] = useState(() => (base.ssl_warn_days ?? [14, 7, 1]).join(', '))
  const [domTxt, setDomTxt] = useState(() => (base.domain_warn_days ?? [30, 7, 1]).join(', '))
  const [hdrText, setHdrText] = useState(() => headersText(base.http_headers ?? ''))
  const [pathsText, setPathsText] = useState(() => (base.extra_paths ?? []).join('\n'))
  const [rules, setRules] = useState<ServerAlertRules | null>(null)

  // правила алертов видит только админ; без них предпросмотр в обычном формате
  useEffect(() => {
    getSiteAlertRules().then(setRules).catch(() => {})
    listLocations().then(setAllLocs).catch(() => {})
  }, [])
  // ответы прежнего нажатия после закрытия формы никуда не пишем
  useEffect(() => () => void runRef.current++, [])

  const cur = parseAddr(addr)
  const fresh = !!tried && tried.target === cur.target && tried.type === cur.type && tried.port === cur.port
  const panel = fresh ? done(tried.panel)?.panel ?? undefined : undefined
  const ctx = fresh ? done(tried.panel) : undefined
  const exp = fresh ? done(tried.exp) : undefined
  const locs = fresh ? done(tried.locs) : undefined
  const inside = fresh ? done(tried.inside) : undefined
  const http = cur.type === 'http'
  const expected = form.expected_status || '200-399'

  // "работает": отвечает, либо ответ - код, который в форме объявлен нормой (401 и т.п.)
  const works = (r: TryResult | undefined) =>
    !!r &&
    (r.status === 'up' ||
      r.status === 'degraded' ||
      (r.status === 'down' && r.kind === 'http' && r.code > 0 && statusOk(r.code, expected)))

  const probe = () => {
    const a = parseAddr(addr)
    if (!a.target) return
    const run = ++runRef.current
    const isHttp = a.type === 'http'
    if (!editing) setWhereTouched(false)
    if (tried && tried.target !== a.target) setAuthSeen(false)
    setTried({
      ...a,
      auth: authKey(form),
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
    const body: Omit<TryIn, 'part'> = {
      check_id: checkId,
      type: a.type,
      target: a.target,
      port: a.port,
      timeout_ms: form.timeout_ms,
      degraded_ms: form.degraded_ms,
      method: form.method,
      expected_status: form.expected_status,
      keyword_up: form.keyword_up,
      keyword_down: form.keyword_down,
      auth_method: form.auth_method ?? '',
      auth_user: form.auth_user ?? '',
      auth_pass: form.auth_pass ?? '',
      http_headers: form.http_headers ?? '',
      ignore_tls: form.ignore_tls,
      extra_paths: form.extra_paths,
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
    if (!http) {
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

  const shownName = name ?? displayHost(cur)
  const shownGroup = group ?? (ctx?.group || '')
  const interval = form.interval_seconds ?? 60
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
  // свой порог показываем, только если он уже стоит у монитора
  const presets: Preset[] = editing && ownFails && presetOfBase() === 'own' ? ['n1', 'n3', 'm5', 'own'] : ['n1', 'n3', 'm5']
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
  const canAdd = editing
    ? !!cur.target && shownName.trim() !== '' && !busy
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
    onSubmit(out)
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
  const host = displayHost(cur)
  const ments = (shownName.match(/@[A-Za-z0-9_]+/g) ?? []).join(' ')
  const sample = failingNow
    ? chosen.message
    : http
      ? t('сервер не ответил на попытку соединения (ConnectTimeout)')
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

  return (
    <div className={editing ? 'am am-edit' : 'card am'}>
      {!editing && <h3 className="am-h">{t('Новый монитор')}</h3>}
      <span className="am-lab">{t('Что проверять')}</span>
      <div className="am-addr">
        <input
          className="mono"
          value={addr}
          autoFocus={!editing}
          placeholder="shop.example.com"
          spellCheck={false}
          onChange={(e) => setAddr(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') probe()
          }}
        />
        <button type="button" disabled={!cur.target || panelPending} onClick={probe}>
          {fresh ? t('Проверить снова') : t('Проверить')}
        </button>
      </div>
      <div className="am-type">
        {cur.target && (
          <span className="type-chip on">
            {http ? t('это сайт') : t('это TCP-порт {port}', { port: cur.port })}
          </span>
        )}
        <span className="muted small">
          {http
            ? t('Сайт, адрес API или хост:порт.')
            : t('Нужен сайт на этом порту - допишите https:// в начале.')}{' '}
          {onClassic && !editing && (
            <>
              {t('Нужно другое:')}{' '}
              <button type="button" className="am-link" onClick={() => onClassic('push')}>
                {t('сигнал от скрипта')}
              </button>
              {', '}
              <button type="button" className="am-link" onClick={() => onClassic('dns')}>
                {t('DNS-записи')}
              </button>
            </>
          )}
        </span>
      </div>

      {!fresh && (!editing || stale) && (
        <div className="am-note">
          {tried
            ? t('Адрес поменялся - проверьте снова.')
            : t('Нажмите "Проверить": панель откроет адрес с панели, изнутри сервера, где нашелся домен, и из локаций. Так видно, работает ли он, еще до сохранения.')}
        </div>
      )}

      {(fresh || editing) && (
        <>
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
            {ctx?.existing_id != null && (
              <div className="am-note warn">
                {editing
                  ? t('Этот адрес смотрит еще монитор "{name}": оба шлют те же алерты.', { name: ctx.existing_name })
                  : t('На этот адрес уже есть монитор "{name}": второй будет слать те же алерты.', { name: ctx.existing_name })}{' '}
                {onOpenCheck && (
                  <button type="button" className="am-link" onClick={() => onOpenCheck(ctx.existing_id as number)}>
                    {t('открыть его')}
                  </button>
                )}
              </div>
            )}
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
                      <label className="field">
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
                        <label className="field">
                          <span>{t('или логин (HTTP Basic)')}</span>
                          <input
                            value={form.auth_user ?? ''}
                            autoComplete="off"
                            onChange={(e) => set({ auth_user: e.target.value, auth_method: e.target.value || form.auth_pass ? 'basic' : '' })}
                          />
                        </label>
                        <label className="field">
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
                      <button type="button" className="ghost" disabled={panelPending} onClick={probe}>
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
                      <small>{t('напомню за {days} дн.', { days: (form.ssl_warn_days ?? [14, 7, 1]).join(', ') })}</small>
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
                      <small>{t('напомню за {days} дн.', { days: (form.domain_warn_days ?? [30, 7, 1]).join(', ') })}</small>
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
            <label className="field">
              <span>{t('Название')}</span>
              <input value={shownName} onChange={(e) => setName(e.target.value)} />
              <span className="field-hint muted small">
                {t('Название попадает в текст алерта: @упоминания в нём тегнут людей в Telegram.')}
              </span>
            </label>
            <label className="field">
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

          {!adv ? (
            <div className="am-sec">
              <button type="button" className="am-link small" onClick={() => setAdv(true)}>
                {t('тонкая настройка')}
              </button>{' '}
              <span className="muted small">
                {editing
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
              <div className="form-grid">
                <label className="field">
                  <span>{t('Интервал, сек')}</span>
                  <input
                    type="number"
                    value={interval}
                    onChange={(e) => set({ interval_seconds: Number(e.target.value) || 0 })}
                  />
                </label>
                <label className="field">
                  <span>{t('Порог «медленно», мс')}</span>
                  <input
                    type="number"
                    value={form.degraded_ms ?? 2000}
                    onChange={(e) => set({ degraded_ms: Number(e.target.value) || 0 })}
                  />
                </label>
                <label className="field">
                  <span>{t('Повторы при сбое')}</span>
                  <input
                    type="number"
                    value={form.retries ?? 3}
                    onChange={(e) => set({ retries: Number(e.target.value) || 0 })}
                    title={t('Считать «упавшим» и слать алерт, только если провалятся все попытки подряд.')}
                  />
                </label>
                <label className="field">
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
                    <label className="field">
                      <span>{t('Ожидаемый статус')}</span>
                      <input
                        value={form.expected_status ?? ''}
                        placeholder="200-399"
                        onChange={(e) => set({ expected_status: e.target.value })}
                      />
                    </label>
                    <label className="field">
                      <span>{t('Слово в ответе (должно быть)')}</span>
                      <input value={form.keyword_up ?? ''} onChange={(e) => set({ keyword_up: e.target.value })} />
                    </label>
                    <label className="field">
                      <span>{t('Слово-стоп (не должно быть)')}</span>
                      <input value={form.keyword_down ?? ''} onChange={(e) => set({ keyword_down: e.target.value })} />
                    </label>
                    <label className="field">
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
                    <label className="field">
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
                  <label className="field">
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
                    <label className="field">
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
              <div className="muted small">{t('После правок нажмите "Проверить снова": проверка пойдет уже с ними.')}</div>
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
          {fresh && !panelPending ? (
            <>
              {t('Будет:')} <b>{shownName}</b>, {t('проверять')} <b>{whereLabel}</b> {everyLabel},{' '}
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
