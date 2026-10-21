import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  checkHistory,
  checkLocations,
  checkLog,
  checkToForm,
  checkUptime,
  listIncidents,
  acceptDns,
  dropCheckKnown,
  rotatePushToken,
  setCheckKnown,
  setCheckNote,
  runCheck,
  runCheckResult,
  snoozeCheck,
  updateCheck,
  type Check,
  type CheckForm,
  type CheckRun,
  type CheckSample,
  type CheckStatus,
  type Incident,
  type LocationResult,
  type Uptime,
} from './api'
import { AddMonitor } from './AddMonitor'
import { CheckWhere, phaseSeries } from './CheckPhases'
import { SnoozeControl } from './SnoozeControl'
import { StackedAreaChart } from './charts/StackedAreaChart'
import { StatusBar, type BarStatus } from './charts/StatusBar'
import { useI18n } from './i18n'
import { MON_ALERT_KINDS, expiryText, expiryTone } from './checkUtils'
import { useAuth } from './auth'
import { NoteBar } from './NoteBar'
import { CheckParentBar } from './ParentBar'
import { KnownCheckBar, KnownDialog } from './KnownDialog'
import { MaintBanner } from './MaintModal'
import { CardNav, OvAreas, OvRows, secInfoOf, type OvRow, type SecDef, type SecInfo, type SecItem } from './cardLayout'
import { NetTile, type Tone } from './NetLinks'
import { useUrlParam } from './deeplink'
import { fmtRel } from './ServersPage'
import { fmtDur } from './units'
import { knownActive } from './serverUtils'
import { TamperSigns } from './TamperSigns'

type Props = {
  check: Check
  groups?: string[]
  initialSec?: string | null // раздел из ссылки алерта: ?check=id&sec=exp
  onClose: () => void
  onSaved?: () => void
  onRun?: () => void // ручная проверка закончилась — обновить список
  onDelete?: () => void
  onUnauthorized: () => void
}

const WINDOWS: { key: string; label: string; hours: number }[] = [
  { key: '24h', label: '24ч', hours: 24 },
  { key: '7d', label: '7д', hours: 168 },
  { key: '30d', label: '30д', hours: 720 },
]

function fmtDateTime(ts: string): string {
  return new Date(ts).toLocaleString([], {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

// Состояние ручной проверки: идёт → готово (вердикт) или «проверить не удалось».
type RunState =
  | {
      phase: 'running'
      source: 'panel' | 'agent'
      server: string
      fast: boolean
      pending: number | null // номер запроса к агенту, когда ответ ещё в пути
      since: number
    }
  | { phase: 'done'; result: CheckRun }
  | { phase: 'failed'; message: string }

// Бины окна целиком: пропуски — «нет данных», а не соседние бины, растянутые на всю
// ширину. У свежего монитора было два бина на всю ленту, и один сбой из двадцати
// проверок выглядел как «полсуток лежал» — рядом с лентой в списке, где тот же сбой
// занимал одну клетку из двадцати четырёх.
function fillWindow(
  points: CheckSample[],
  fromMs: number,
  toMs: number,
  stepSec: number,
): { ts: number; p: CheckSample | null }[] {
  const plain = points.map((p) => ({ ts: new Date(p.ts).getTime(), p }))
  if (!stepSec || points.length === 0) return plain
  const stepMs = stepSec * 1000
  const first = Math.floor(fromMs / stepMs) * stepMs
  const last = Math.floor(toMs / stepMs) * stepMs
  const n = (last - first) / stepMs + 1
  if (n <= 0 || n > 2000) return plain
  const byBin = new Map<number, CheckSample>()
  for (const x of plain) byBin.set(Math.floor(x.ts / stepMs) * stepMs, x.p)
  const out: { ts: number; p: CheckSample | null }[] = []
  for (let b = first; b <= last; b += stepMs) out.push({ ts: b, p: byBin.get(b) ?? null })
  return out
}

/** Метка «алерта не было» с объяснением, чего именно не хватило. */
function NoAlertChip({ inc, t }: { inc: Incident; t: (k: string, p?: Record<string, string | number>) => string }) {
  const n = inc.alert_after ?? 0
  const iv = inc.interval_seconds ?? 0
  const mins = n && iv ? Math.round((n * iv) / 60) : 0
  const hint = n
    ? t('Алерт уходит после {n} неудачных проверок подряд — это около {m} мин непрерывного сбоя. Этот инцидент закончился раньше. Порог меняется в настройках монитора.', { n, m: mins })
    : t('По этому инциденту алерт не отправлялся.')
  return <span className="type-chip inc-noalert" title={hint}>{t('без алерта')}</span>
}

// Карточка монитора по разделам, как карточки серверов (cardLayout): меню слева, на экране один
// раздел. Раньше все шло одной лентой - плитки аптайма, сроки, локации, пути, график, инциденты и
// журнал, где сбои повторялись дважды. У push-монитора вместо времени ответа "Сигнал", у DNS
// еще "Записи DNS"
const CHECK_SECTIONS: SecDef[] = [
  { id: 'overview', label: 'Сводка', group: '' },
  { id: 'rt', label: 'Время ответа', group: 'Проверка' },
  { id: 'signal', label: 'Сигнал', group: 'Проверка' },
  { id: 'dns', label: 'Записи DNS', group: 'Проверка' },
  { id: 'loc', label: 'Локации', group: 'Проверка' },
  { id: 'paths', label: 'Пути и адреса', group: 'Проверка' },
  { id: 'exp', label: 'Сертификат и домен', group: 'Проверка' },
  { id: 'tamper', label: 'Признаки взлома', group: 'Проверка' },
  { id: 'inc', label: 'Инциденты', group: 'История' },
  { id: 'log', label: 'Журнал проверок', group: 'История' },
  { id: 'deps', label: 'Зависимости', group: 'Связи' },
  { id: 'alerts', label: 'Алерты', group: 'Связи' },
]
const CHECK_SEC_IDS = new Set(CHECK_SECTIONS.map((x) => x.id))
// разделы, которым нужно окно 24ч/7д/30д: переключатель в шапке раздела
const WIN_SECS = new Set(['overview', 'rt', 'signal', 'dns', 'inc', 'log'])
const subOf = (...parts: (string | false | 0 | null | undefined)[]) => parts.filter(Boolean).join(' · ')
const maxWarn = (w: number[]) => (w.length ? Math.max(...w) : 0)
// интервал без нулевого хвоста: "1м", а не "1м 0с"
const fmtEvery = (s?: number) => fmtDur(s).replace(/ 0\D+$/, '')
const upTone = (u: number | null | undefined): Tone | undefined =>
  u == null ? undefined : u >= 99 ? 'ok' : u >= 95 ? 'warn' : 'bad'

export function CheckDetail({ check, groups, initialSec, onClose, onSaved, onRun, onDelete, onUnauthorized }: Props) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [hours, setHours] = useState(24)
  const [uptime, setUptime] = useState<Uptime | null>(null)
  const [points, setPoints] = useState<CheckSample[] | null>(null)
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [locs, setLocs] = useState<LocationResult[]>([])
  const [logRows, setLogRows] = useState<CheckSample[]>([])
  // сбои показывают инциденты, журнал по умолчанию - все проверки
  const [logFailed, setLogFailed] = useState(false)
  // Старое разворачивают руками. Открытая карточка показывает выбранное окно —
  // иначе первым же экраном встречает сбой недельной давности, и кажется, что
  // с сайтом что-то не так прямо сейчас.
  const [oldInc, setOldInc] = useState(false)
  const [oldLog, setOldLog] = useState(false)
  // локация, чью тайм-серию показываем на графике (null = основная/прямая)
  const [selectedLoc, setSelectedLoc] = useState<number | null>(null)
  const [selectedIp, setSelectedIp] = useState<string | null>(null) // график по IP
  const [zoomOpen, setZoomOpen] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [step, setStep] = useState(0) // ширина бина истории, сек
  // ручная проверка и её вердикт; refresh — перечитать карточку сразу после неё,
  // не дожидаясь очередного автообновления
  const [run, setRun] = useState<RunState | null>(null)
  const [refresh, setRefresh] = useState(0)
  const [knownOpen, setKnownOpen] = useState(false) // отметить сбой известной проблемой
  const [active, setActive] = useState<string>(() => (CHECK_SEC_IDS.has(initialSec ?? '') ? initialSec! : 'overview'))
  const layoutRef = useRef<HTMLDivElement>(null)
  const go = (id: string) => {
    setActive(id)
    const el = layoutRef.current
    if (el && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: 'start' })
  }

  // редактирование прямо в модалке: там же заметка и удаление, как у сервера
  const [editing, setEditing] = useState(false)
  const [snoozing, setSnoozing] = useState(false)
  const [form, setForm] = useState<CheckForm | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)

  const startEdit = () => {
    setForm(checkToForm(check))
    setSaveErr(null)
    setEditing(true)
  }
  // старый монитор сертификата формы не имеет: его заводят и меняют только через API
  const certOnly = check.type === 'cert'

  const submitEdit = async (next?: CheckForm) => {
    const body = next ?? form
    if (!body) return
    setSaving(true)
    setSaveErr(null)
    try {
      await updateCheck(check.id, body)
      setEditing(false)
      onSaved?.()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setSaveErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setSaving(false)
    }
  }

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    },
    [onUnauthorized, t],
  )

  // «Проверить сейчас»: человек жмёт кнопку, когда что-то поменял или сомневается, и
  // ответ должен быть виден сразу и однозначно — работает сайт или нет и почему.
  // Раньше результат молча записывался в журнал: кнопку жали двадцать раз подряд.
  const running = run?.phase === 'running'
  const finishRun = (r: CheckRun) => {
    setRun(r.run_error ? { phase: 'failed', message: r.run_error } : { phase: 'done', result: r })
    setRefresh((x) => x + 1)
    onRun?.()
  }
  // опрос ответа агента живёт в эффекте, а колбэки меняются на каждой отрисовке
  const runCb = useRef({ finishRun, onUnauthorized })
  runCb.current = { finishRun, onUnauthorized }

  const startRun = async () => {
    if (running) return
    const local = check.probe_local
    setRun({
      phase: 'running', source: local ? 'agent' : 'panel',
      server: check.probe_server_name ?? '', fast: false, pending: null, since: Date.now(),
    })
    try {
      const r = await runCheck(check.id)
      if (r.run_pending != null) {
        setRun({
          phase: 'running', source: 'agent', server: r.run_server, fast: r.run_fast,
          pending: r.run_pending, since: Date.now(),
        })
      } else {
        finishRun(r)
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setRun({ phase: 'failed', message: e instanceof Error ? e.message : t('Ошибка') })
    }
  }

  const pendingId = run?.phase === 'running' ? run.pending : null
  useEffect(() => {
    if (pendingId == null) return
    let stop = false
    const started = Date.now()
    let timer = 0
    const poll = async () => {
      if (stop) return
      try {
        const r = await runCheckResult(check.id, pendingId)
        if (stop) return
        if (r.run_pending == null) return runCb.current.finishRun(r)
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return runCb.current.onUnauthorized()
        // сеть моргнула — спрашиваем дальше, срок ожидания считает панель
      }
      if (Date.now() - started > 150_000) {
        setRun({ phase: 'failed', message: t('Ответ проверки так и не пришёл.') })
        return
      }
      timer = window.setTimeout(poll, 1000)
    }
    timer = window.setTimeout(poll, 700)
    return () => {
      stop = true
      window.clearTimeout(timer)
    }
  }, [pendingId, check.id, t])

  useEffect(() => setRun(null), [check.id])
  const closeRun = useCallback(() => setRun(null), [])

  // статусы/аптайм/инциденты/локации — с автообновлением (пока не редактируем)
  useEffect(() => {
    if (editing) return
    const run = () => {
      checkUptime(check.id).then(setUptime).catch(fail)
      listIncidents(50, check.id).then(setIncidents).catch(fail)
      checkLog(check.id, logFailed, 100).then(setLogRows).catch(fail)
      if (check.check_locations)
        checkLocations(check.id)
          .then((rows) => {
            setLocs(rows)
            setSelectedLoc((cur) =>
              cur ?? rows.find((r) => r.direct)?.location_id ?? rows[0]?.location_id ?? null,
            )
          })
          .catch(fail)
    }
    run()
    const id = window.setInterval(run, 12000)
    return () => window.clearInterval(id)
  }, [check.id, check.check_locations, editing, logFailed, fail, refresh])

  useEffect(() => {
    setOldInc(false)
    setOldLog(false)
  }, [hours, check.id])

  // Граница «свежего»: то же окно, что у графика и полосы статусов. Один
  // переключатель на всю карточку — переключил на 7д, увидел и историю сбоев.
  const cutoff = Date.now() - hours * 3600_000
  const winLabel = t(WINDOWS.find((w) => w.hours === hours)?.label ?? '24ч')
  // Незакрытый инцидент — всегда свежий, даже если начался месяц назад: он идёт СЕЙЧАС.
  const incFresh = incidents.filter(
    (i) => !i.ended_at || new Date(i.ended_at).getTime() >= cutoff,
  )
  const incHidden = incidents.length - incFresh.length
  const incShown = oldInc ? incidents : incFresh
  const logFresh = logRows.filter((r) => new Date(r.ts).getTime() >= cutoff)
  const logHidden = logRows.length - logFresh.length
  const logShown = oldLog ? logRows : logFresh

  // выбранная локация: если она прямая — бэкенд отдаст основную серию
  const selLoc = locs.find((l) => l.location_id === selectedLoc)
  const selDirect = selLoc?.direct ?? true
  const selName = selLoc?.name

  useEffect(() => {
    setPoints(null) // сброс только при смене окна/локации/IP — на автообновлении без мигания
    const run = () =>
      checkHistory(
        check.id,
        hours,
        selectedIp ? undefined : selDirect ? undefined : selectedLoc ?? undefined,
        undefined,
        selectedIp ?? undefined,
      )
        .then((h) => {
          setPoints(h.points)
          setStep(h.step_seconds ?? 0)
        })
        .catch(fail)
    run()
    if (editing) return
    const id = window.setInterval(run, 12000)
    return () => window.clearInterval(id)
  }, [check.id, hours, selectedLoc, selDirect, selectedIp, editing, fail, refresh])

  const isCert = check.type === 'cert'
  const chartTs = (points ?? []).map((p) => new Date(p.ts).getTime())
  // основная серия http-монитора - по фазам (DNS, подключение, TLS, ожидание, загрузка)
  const phased = !isCert && !selectedIp && selDirect ? phaseSeries(points, t) : null
  const chartSeries = phased ?? [
    {
      name: isCert ? t('дн.') : t('мс'),
      color: isCert ? '#22c55e' : '#4b74ff',
      values: (points ?? []).map((p) => (isCert ? p.value : p.latency_ms)),
    },
  ]
  const fmtCY = (v: number) => `${Math.round(v)}`
  const fmtCV = (v: number) =>
    isCert ? `${Math.round(v)} ${t('дн.')}` : `${Math.round(v)} ${t('мс')}`
  const chartCap = isCert
    ? t('Осталось дней сертификата')
    : selectedIp
      ? t('Время ответа · IP {ip}', { ip: selectedIp })
      : check.check_locations && selName
        ? t('Время ответа · {loc}', { loc: selName })
        : t('Время ответа')
  const fmtX =
    hours <= 24
      ? (ms: number) =>
          new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : (ms: number) =>
          new Date(ms).toLocaleDateString([], { day: '2-digit', month: '2-digit' })

  const nowMs = Date.now()
  const segments: { status: BarStatus; title: string }[] =
    points == null
      ? []
      : fillWindow(points, nowMs - hours * 3600_000, nowMs, step).map(({ ts, p }) => ({
          status: p ? p.status : 'nodata',
          title: `${fmtDateTime(new Date(ts).toISOString())} · ${
            p ? statusWord(p.status, t) : t('нет данных')
          }`,
        }))

  const paths = check.last_path_results ?? []
  const ips = check.last_ip_results ?? []
  // "проверять все адреса", а у домена адрес один: бэкенд пишет пустую разбивку (null - еще не проверяли)
  const oneIp = !!check.check_all_ips && Array.isArray(check.last_ip_results) && check.last_ip_results.length === 0
  const parents = check.parents ?? []
  const mutes = check.alert_mutes ?? []
  const snoozed = !!check.snooze_until && new Date(check.snooze_until).getTime() > nowMs
  const known = knownActive(check.known_issue) ? check.known_issue : null
  const mainSec = check.type === 'push' ? 'signal' : check.type === 'dns' ? 'dns' : 'rt'
  const probed = check.type !== 'push' && check.type !== 'dns'
  const shown: Record<string, boolean> = {
    overview: true,
    rt: check.type !== 'push',
    signal: check.type === 'push',
    dns: check.type === 'dns',
    // у push и DNS нет ни локаций, ни путей, ни сроков, даже если флаги остались от другого типа
    loc: probed && check.check_locations,
    // пути, все адреса домена и сроки панель проверяет только у HTTP: у TCP-монитора раздел был бы
    // вечным "нет данных"
    paths: check.type === 'http' && ((check.extra_paths ?? []).length > 0 || check.check_all_ips),
    exp: check.type === 'http' && (check.check_ssl || check.check_domain),
    // главную разбирает панель (app/tamper.py): сайту за белым списком ее не открыть
    tamper: check.type === 'http' && !check.probe_local,
    inc: true,
    log: true,
    deps: true,
    alerts: true,
  }
  // у TLS-монитора на графике не время ответа, а сколько осталось сертификату
  const secs = CHECK_SECTIONS.filter((x) => shown[x.id]).map((x) =>
    x.id === 'rt' && isCert ? { ...x, label: 'Срок сертификата' } : x,
  )
  const cur = secs.find((x) => x.id === active) ?? secs[0]
  useUrlParam('sec', editing || cur.id === 'overview' ? null : cur.id)

  // Замечания - те же, что на главной в блоке "Сайты": лежит, деградирует или отвечает через раз
  // (кроме отмеченного известной проблемой) и истекающие сертификат и домен
  const items: Record<string, SecItem[]> = {}
  const add = (sec: string, x: SecItem) => (items[sec] ??= []).push(x)
  const st = check.last_status
  const bad = check.enabled && (st === 'down' || st === 'degraded' || !!check.flaky_since)
  const stText = st === 'down' ? t('Недоступно') : st === 'degraded' ? t('Деградация') : t('Отвечает через раз')
  const openInc = incidents.find((i) => !i.ended_at)
  if (bad && !known)
    add(mainSec, {
      level: st === 'down' ? 2 : 1,
      text: stText + (st !== 'up' && check.last_message ? ` · ${check.last_message}` : ''),
      since: st === 'up' ? check.flaky_since : openInc?.started_at,
    })
  if (shown.exp && check.check_ssl && check.ssl_days != null && check.ssl_days <= maxWarn(check.ssl_warn_days))
    add('exp', { level: check.ssl_days <= 3 ? 2 : 1, text: `${t('SSL-сертификат')}: ${expiryText(check.ssl_days, t)}` })
  if (shown.exp && check.check_domain && check.domain_days != null && check.domain_days <= maxWarn(check.domain_warn_days))
    add('exp', { level: check.domain_days <= 3 ? 2 : 1, text: `${t('Домен')}: ${expiryText(check.domain_days, t)}` })

  // признаки взлома: похоже на взлом - красным, один слабый - желтым, как строка на главной
  const tamper = check.tamper
  if (shown.tamper && check.enabled && tamper && tamper.level !== 'none')
    add('tamper', { level: tamper.level === 'hack' ? 2 : 1, text: tamper.summary, since: tamper.since })

  const lat = (points ?? []).map((p) => p.latency_ms).filter((x): x is number => x != null).sort((a, b) => a - b)
  const usual = lat.length >= 5 ? lat[Math.floor(lat.length / 2)] : null
  const lastInc = incidents[0]
  const ok: Record<string, string> = {
    rt: isCert
      ? check.last_value != null ? t('осталось {n} дн.', { n: Math.round(check.last_value) }) : t('нет данных')
      : check.last_latency_ms != null
        ? subOf(t('сейчас {ms} мс', { ms: check.last_latency_ms }), usual != null && t('обычно {ms} мс', { ms: usual }))
        : t('нет данных'),
    signal: check.push_at ? t('последний сигнал {ago}', { ago: fmtRel(check.push_at) }) : t('Сигнала еще не было'),
    dns: (check.dns_last ?? []).length ? `${t('сейчас')}: ${(check.dns_last ?? []).join(', ')}` : t('нет данных'),
    loc: locs.length ? t('видят все: {list}', { list: locs.map((l) => l.name).join(', ') }) : t('нет данных'),
    paths: subOf(
      paths.length > 0 && t('путей: {n}', { n: paths.length }),
      ips.length > 0 && t('адресов домена: {n}', { n: ips.length }),
      oneIp && t('у домена один адрес'),
    ) || t('нет данных'),
    exp: subOf(
      check.check_ssl && check.ssl_days != null && `${t('сертификат')} ${expiryText(check.ssl_days, t)}`,
      check.check_domain && check.domain_days != null && `${t('домен')} ${expiryText(check.domain_days, t)}`,
    ) || t('нет данных'),
    tamper: tamper
      ? tamper.err && !tamper.last_at ? t('разобрать не удалось: {err}', { err: tamper.err }) : t('признаков нет')
      : t('еще не разбирали'),
    inc: incFresh.length
      ? t('за {w}: {n}, последний {t}', { w: winLabel, n: incFresh.length, t: fmtDateTime(incFresh[0].started_at) })
      : lastInc
        ? t('за {w} не было, последний {t}', { w: winLabel, t: fmtDateTime(lastInc.started_at) })
        : t('Инцидентов не было.'),
    log: check.last_checked_at ? t('последняя проверка {ago}', { ago: fmtRel(check.last_checked_at) }) : t('Пока пусто.'),
    deps: parents.length
      ? t('зависит от {list}', { list: parents.map((p) => p.name).join(', ') })
      : (check.parent_mode ?? 'auto') === 'off' ? t('ни от какого сервера не зависит') : t('на каком сервере работает, панель не определила'),
    alerts: snoozed
      ? t('алерты заглушены до {t}', { t: fmtDateTime(check.snooze_until!) })
      : mutes.length
        ? t('не слать: {list}', { list: mutes.map((k) => t(MON_ALERT_KINDS.find((x) => x.k === k)?.label ?? k)).join(', ') })
        : t('ничего не заглушено'),
  }
  const info: Record<string, SecInfo> = {}
  for (const { id } of CHECK_SECTIONS) info[id] = secInfoOf(items[id] ?? [], ok[id] ?? '', t)
  // не проблема с главной, но и не "все в порядке": желтым на карте, без числа в меню
  const soft = (id: string, line: string) => {
    if (!info[id].level) info[id] = { ...info[id], level: 1, line }
  }
  if (!check.enabled) soft(mainSec, t('монитор выключен'))
  else if (bad && known) soft(mainSec, stText)
  // лежит целиком - не отвечает ни одна локация, в loc_down (частичная доступность) их нет
  const locDown = [...new Set([...(check.loc_down ?? []), ...locs.filter((l) => l.enabled && l.status === 'down').map((l) => l.name)])]
  if (locDown.length) soft('loc', t('не отвечает из: {list}', { list: locDown.join(', ') }))
  const pathsBy = (s: string) => [...paths.filter((r) => r.status === s).map((r) => r.path), ...ips.filter((r) => r.status === s).map((r) => r.ip)]
  if (pathsBy('down').length) soft('paths', t('не отвечает: {list}', { list: pathsBy('down').join(', ') }))
  else if (pathsBy('degraded').length) soft('paths', t('медленно отвечает: {list}', { list: pathsBy('degraded').join(', ') }))
  const silent = parents.filter((p) => p.silent)
  if (silent.length) soft('deps', t('{list}: сервер не на связи, алерты монитора ждут его', { list: silent.map((p) => p.name).join(', ') }))
  if (known) soft('alerts', t('известная проблема: {text}', { text: known.text }))
  if (!incidents.length) info.inc.idle = true

  const rows: OvRow[] = CHECK_SECTIONS.flatMap(({ id }) =>
    (items[id] ?? []).map((x) => ({
      tone: x.level >= 2 ? ('t-down' as const) : ('t-degraded' as const),
      text: x.text,
      since: x.since,
      act: shown[id] ? { label: t(secs.find((s) => s.id === id)?.label ?? id), onClick: () => go(id) } : undefined,
    })),
  ).sort((a, b) => Number(b.tone === 't-down') - Number(a.tone === 't-down'))
  const alertAfterMin = Math.round((check.alert_after_failures * check.interval_seconds) / 60)

  const statusBar = (
    <>
      <div className="chart-cap">{t('Доступность за {w}', { w: winLabel })}</div>
      <StatusBar segments={segments} />
    </>
  )
  const chartBlock = (
    <div className="chart-block chart-card">
      <button className="chart-cap chart-cap-btn" onClick={() => setZoomOpen(true)}>
        <span>{chartCap}</span>
        <span className="chart-expand" title={t('Открыть на весь экран')}>⤢</span>
      </button>
      {points == null ? (
        <div className="chart-empty">{t('загрузка…')}</div>
      ) : chartTs.length ? (
        <StackedAreaChart
          ts={chartTs}
          series={chartSeries}
          fmtY={fmtCY}
          fmtV={fmtCV}
          fmtTime={fmtX}
          height={170}
          onExpand={() => setZoomOpen(true)}
        />
      ) : (
        <div className="chart-empty">—</div>
      )}
    </div>
  )

  return (
    <>
    <div className="modal-backdrop">
      <div className="card detail detail-server hs-modal">
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot sdot-${check.last_status}`} />
            <h3>{check.name}</h3>
            <span className="type-chip">
              {{ http: 'HTTP', tcp_port: 'TCP', cert: 'TLS', push: 'PUSH', dns: 'DNS' }[check.type]}
            </span>
            {check.group_name && <span className="type-chip group-chip">{check.group_name}</span>}
            {/* что заглушено - значком, сами заглушки в разделе "Алерты" */}
            {(snoozed || mutes.length > 0) && !editing && (
              <button className="srv-head-chip" onClick={() => go('alerts')} title={ok.alerts}>
                🔕
              </button>
            )}
            {/* заметка живет в редактировании монитора, здесь - значком с текстом в подсказке */}
            {check.note && !editing && (
              <button
                className="srv-head-chip srv-head-note"
                title={check.note}
                onClick={isViewer ? undefined : startEdit}
              >
                📝
              </button>
            )}
          </div>
          <div className="detail-head-actions">
            {!editing && !isViewer && onRun && (
              <button
                className="ghost icon-btn"
                onClick={startRun}
                disabled={running}
                title={t('Проверить сейчас')}
              >
                {running ? <span className="run-spin" /> : '▶'}
              </button>
            )}
            {!editing && !isViewer && (
              <button className="ghost icon-btn" onClick={startEdit} title={t('Изменить')}>
                ✎
              </button>
            )}
            <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
              ✕
            </button>
          </div>
        </div>
        <div className="srv-meta srv-meta-line">
          {check.type !== 'push' && (
            <span className="srv-meta-item mono">
              {check.target}
              {check.port ? `:${check.port}` : ''}
            </span>
          )}
          {check.type === 'dns' && (
            <span className="srv-meta-item">
              {check.dns_type || 'A'}
              {check.dns_server ? ` @${check.dns_server}` : ''}
            </span>
          )}
          <span className="srv-meta-item">
            <span className="srv-meta-k">{check.type === 'push' ? t('сигнал раз в') : t('раз в')}</span>{' '}
            {fmtEvery(check.type === 'push' ? check.push_period : check.interval_seconds)}
          </span>
          {check.type === 'push' ? (
            <span className="srv-meta-item">{t('последний сигнал {ago}', { ago: fmtRel(check.push_at ?? null) })}</span>
          ) : (
            <span className="srv-meta-item">
              <span className="srv-meta-k">{t('проверен')}</span> {fmtRel(check.last_checked_at)}
            </span>
          )}
          {check.last_latency_ms != null && !isCert && check.type !== 'push' && (
            <span className="srv-meta-item">
              <span className="srv-meta-k">{t('ответ')}</span> {check.last_latency_ms} {t('мс')}
            </span>
          )}
          {shown.loc && (
            <span className="srv-meta-item">
              <span className="srv-meta-k">{t('с локаций')}</span> {locs.length || (check.location_ids ?? []).length}
            </span>
          )}
          {check.probe_local && (
            <span className="srv-meta-item">
              <span className="srv-meta-k">{t('изнутри')}</span> {check.probe_server_name || '—'}
            </span>
          )}
          {!check.enabled && <span className="srv-meta-item t-degraded">{t('выключен')}</span>}
        </div>
        {!editing && <MaintBanner kind="check" id={check.id} />}

        {editing && form ? (
          <>
            <NoteBar
              note={check.note}
              by={check.note_by}
              at={check.note_at}
              canEdit={!isViewer}
              onSave={async (text) => {
                try {
                  await setCheckNote(check.id, text)
                  onSaved?.()
                } catch (e) {
                  if (e instanceof ApiError && e.status === 401) onUnauthorized()
                  throw e
                }
              }}
            />
            {/* удаление - сверху и отдельно, чтобы не нажать случайно рядом с "Сохранить" */}
            {onDelete && (
              <div className="edit-danger-top">
                <button className="ghost danger-btn" onClick={onDelete} disabled={saving}>
                  🗑 {t('Удалить монитор')}
                </button>
              </div>
            )}
            {/* правка - той же формой, что и новый монитор, с пробной проверкой; заметка и
                удаление выше есть у всех, включая старый монитор сертификата */}
            {certOnly ? (
              <div className="card">
                <p className="muted small">
                  {t('Это монитор сертификата старого типа: его настройки меняются только через API. Удалить его можно кнопкой выше, а за сертификатом следит и обычный монитор сайта.')}
                </p>
                <div className="modal-actions">
                  <button className="ghost" onClick={() => setEditing(false)}>
                    {t('Закрыть')}
                  </button>
                </div>
              </div>
            ) : (
              <AddMonitor
                editing
                checkId={check.id}
                base={form}
                groups={groups ?? []}
                busy={saving}
                error={saveErr}
                onSubmit={submitEdit}
                onCancel={() => setEditing(false)}
                onUnauthorized={onUnauthorized}
              />
            )}
          </>
        ) : (
          <>
        {err && <p className="form-error">{err}</p>}
        <div className="srv-layout" ref={layoutRef}>
          <CardNav secs={secs} active={cur.id} info={info} onGo={go} />
          <div className="srv-main">
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
              {WIN_SECS.has(cur.id) && (
                <div className="win-switch">
                  {WINDOWS.map((w) => (
                    <button
                      key={w.key}
                      className={`win-btn${hours === w.hours ? ' win-btn-active' : ''}`}
                      onClick={() => setHours(w.hours)}
                    >
                      {t(w.label)}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {cur.id === 'overview' && (
              <div className="srv-sec">
                <OvRows rows={rows} />
                {/* лежит - сразу видно, на какой фазе оборвался запрос и что изнутри сервера */}
                {st !== 'up' && <CheckWhere check={check} />}
                <div className="net-tiles srv-tiles">
                  <button className="srv-tile-btn" onClick={() => go(mainSec)} title={t('к разделу')}>
                    <NetTile
                      label={t('Сейчас')}
                      value={statusWord(st, t)}
                      sub={subOf(
                        check.last_latency_ms != null && !isCert && check.type !== 'push' && `${check.last_latency_ms} ${t('мс')}`,
                        check.last_checked_at && fmtRel(check.last_checked_at),
                      ) || undefined}
                      tone={st === 'up' ? 'ok' : st === 'degraded' ? 'warn' : st === 'down' ? 'bad' : undefined}
                    />
                  </button>
                  {([[t('Аптайм 24ч'), uptime?.day], [t('Аптайм 7д'), uptime?.week], [t('Аптайм 30д'), uptime?.month]] as const).map(([label, v]) => (
                    <button key={label} className="srv-tile-btn" onClick={() => go('inc')} title={t('к разделу')}>
                      <NetTile label={label} value={v == null ? '—' : `${v}%`} tone={upTone(v)} />
                    </button>
                  ))}
                </div>
                {statusBar}
                <OvAreas
                  cap={t('Монитор по областям')}
                  items={secs.filter((x) => x.id !== 'overview').map((x) => ({ id: x.id, label: x.label, info: info[x.id] }))}
                  onGo={go}
                />
              </div>
            )}

            {cur.id === 'rt' && (
              <div className="srv-sec">
                {statusBar}
                <CheckWhere check={check} />
                {(selectedIp || !selDirect) && (
                  <div>
                    <button
                      className="ghost small"
                      onClick={() => {
                        setSelectedIp(null)
                        setSelectedLoc(locs.find((l) => l.direct)?.location_id ?? null)
                      }}
                    >
                      {t('показать основную проверку')}
                    </button>
                  </div>
                )}
                {chartBlock}
              </div>
            )}

            {cur.id === 'signal' && (
              <div className="srv-sec">
                {statusBar}
                <PushInfo check={check} canEdit={!isViewer} />
              </div>
            )}

            {cur.id === 'tamper' && (
              <div className="srv-sec">
                <TamperSigns check={check} canEdit={!isViewer} onChanged={() => onSaved?.()} />
              </div>
            )}

            {cur.id === 'dns' && (
              <div className="srv-sec">
                {statusBar}
                <DnsInfo check={check} canEdit={!isViewer} onAccepted={() => onSaved?.()} />
              </div>
            )}

            {cur.id === 'loc' && (
              <div className="srv-sec">
                <div className="loc-results">
                  {locs.map((r) => (
                    <button
                      key={r.location_id}
                      className={
                        'loc-res loc-res-btn' +
                        (r.location_id === selectedLoc ? ' loc-res-active' : '') +
                        (!r.enabled ? ' loc-res-off' : '')
                      }
                      onClick={() => {
                        setSelectedLoc(r.location_id)
                        setSelectedIp(null) // локация активна → график по локации
                        go('rt')
                      }}
                      title={t('проверено: {t}', { t: fmtDateTime(r.checked_at) })}
                    >
                      <span className={`sdot sdot-${r.status}`} />
                      <div className="loc-res-name">
                        {r.name}
                        {r.direct && <span className="type-chip">{t('напрямую')}</span>}
                      </div>
                      <div className="loc-res-metric">
                        {r.latency_ms != null ? `${r.latency_ms} ${t('мс')}` : '—'}
                      </div>
                      <div className="loc-res-msg muted small" title={r.message}>
                        {r.message || '—'}
                      </div>
                    </button>
                  ))}
                  {locs.length === 0 && (
                    <div className="srv-empty">
                      {t('Ещё нет данных из локаций — проверка идёт в фоне.')}
                    </div>
                  )}
                </div>
                <div className="net-foot">{t('Клик по локации - ее время ответа на графике')}</div>
              </div>
            )}

            {cur.id === 'paths' && (
              <div className="srv-sec">
                {(check.extra_paths ?? []).length > 0 && (
                  <div className="detail-locs">
                    <div className="chart-cap">{t('Дополнительные пути')}</div>
                    <div className="loc-results">
                      {paths.map((r) => (
                        <div key={r.path} className="loc-res">
                          <span className={`sdot sdot-${r.status === 'pending' || r.status === 'skipped' ? 'unknown' : r.status}`} />
                          <div className="loc-res-name mono">{r.path}</div>
                          <div className="loc-res-metric">
                            {r.latency_ms != null ? `${r.latency_ms} ${t('мс')}` : '—'}
                          </div>
                          <div className="loc-res-msg muted small" title={r.message}>
                            {r.message || '—'}
                          </div>
                        </div>
                      ))}
                      {paths.length === 0 && (
                        <div className="muted small">
                          {t('Пути ещё не проверялись: результат появится после ближайшей проверки.')}
                        </div>
                      )}
                    </div>
                  </div>
                )}
                {check.check_all_ips && (
                  <div className="detail-locs">
                    <div className="chart-cap">{t('IP-адреса домена')}</div>
                    <div className="loc-results">
                      {ips.map((r) => (
                        <button
                          key={r.ip}
                          className={'loc-res loc-res-btn' + (r.ip === selectedIp ? ' loc-res-active' : '')}
                          onClick={() => {
                            setSelectedIp(r.ip)
                            go('rt')
                          }}
                          title={t('Показать время ответа этого IP')}
                        >
                          <span className={`sdot sdot-${r.status}`} />
                          <div className="loc-res-name mono">{r.ip}</div>
                          <div className="loc-res-metric">
                            {r.latency_ms != null ? `${r.latency_ms} ${t('мс')}` : '—'}
                          </div>
                          <div className="loc-res-msg muted small" title={r.message}>
                            {r.message || '—'}
                          </div>
                        </button>
                      ))}
                      {ips.length === 0 && (
                        <div className="muted small">
                          {oneIp
                            ? t('У домена один адрес: сравнивать нечего, его проверяет обычная проверка. Разбивка появится, когда адресов станет больше.')
                            : t('Разбивки по адресам еще нет: появится после ближайшей проверки.')}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}

            {cur.id === 'exp' && (
              <div className="srv-sec">
                <div className="expiry-pills">
                  {check.check_ssl && (
                    <ExpiryPill
                      icon="🔐"
                      label={t('SSL-сертификат')}
                      days={check.ssl_days}
                      warn={check.ssl_warn_days}
                      message={check.ssl_message}
                    />
                  )}
                  {check.check_domain && (
                    <ExpiryPill
                      icon="🌐"
                      label={t('Домен')}
                      days={check.domain_days}
                      warn={check.domain_warn_days}
                      message={check.domain_message}
                    />
                  )}
                </div>
                <div className="kv-list">
                  {check.check_ssl && (
                    <div className="kv-row">
                      <span className="kv-k muted small">{t('SSL-сертификат')}</span>
                      <span className="kv-v small">
                        {subOf(check.ssl_message, check.ssl_warn_days.length > 0 && t('предупреждать за {d} дн.', { d: check.ssl_warn_days.join(', ') }))}
                      </span>
                    </div>
                  )}
                  {check.check_domain && (
                    <div className="kv-row">
                      <span className="kv-k muted small">{t('Домен')}</span>
                      <span className="kv-v small">
                        {subOf(check.domain_message, check.domain_warn_days.length > 0 && t('предупреждать за {d} дн.', { d: check.domain_warn_days.join(', ') }))}
                      </span>
                    </div>
                  )}
                  {check.expiry_checked_at && (
                    <div className="kv-row">
                      <span className="kv-k muted small">{t('проверено')}</span>
                      <span className="kv-v small">{fmtRel(check.expiry_checked_at)}</span>
                    </div>
                  )}
                </div>
              </div>
            )}

            {cur.id === 'inc' && (
              <div className="srv-sec">
                {incidents.length === 0 ? (
                  <div className="srv-empty">{t('Инцидентов не было.')}</div>
                ) : incShown.length === 0 ? (
                  <div className="muted small">
                    {t('За {w} инцидентов не было.', { w: winLabel })}{' '}
                    {t('последний — {t}', { t: fmtDateTime(incidents[0].started_at) })}
                  </div>
                ) : (
                  <div className="inc-list">
                    {incShown.map((inc) => (
                      <div key={inc.id} className="inc-item">
                        <span className={`sdot sdot-${inc.status}`} />
                        <div className="inc-when">
                          <div>{fmtDateTime(inc.started_at)}</div>
                          <div className="muted small">
                            {inc.ended_at
                              ? t('до {t}', { t: fmtDateTime(inc.ended_at) })
                              : t('идёт сейчас')}
                          </div>
                        </div>
                        <div className="inc-msg muted small" title={inc.last_message}>
                          {inc.last_message || '—'}
                        </div>
                        {/* «Сайт лежал, а алерта не было» — самый частый вопрос к панели.
                            Ответ должен стоять прямо у инцидента: короткий сбой не набирает
                            порог «N проверок подряд», и это видно без похода в базу. */}
                        {inc.notified === false && <NoAlertChip inc={inc} t={t} />}
                      </div>
                    ))}
                  </div>
                )}
                {incHidden > 0 && (
                  <button className="ghost small stale-more" onClick={() => setOldInc(!oldInc)}>
                    {oldInc
                      ? t('скрыть прежние')
                      : t('показать прежние ({n})', { n: incHidden })}
                  </button>
                )}
              </div>
            )}

            {cur.id === 'log' && (
              <div className="srv-sec">
                <div className="win-switch log-switch">
                  <button
                    className={`win-btn${!logFailed ? ' win-btn-active' : ''}`}
                    onClick={() => setLogFailed(false)}
                  >
                    {t('Все')}
                  </button>
                  <button
                    className={`win-btn${logFailed ? ' win-btn-active' : ''}`}
                    onClick={() => setLogFailed(true)}
                  >
                    {t('Только сбои')}
                  </button>
                </div>
                {logRows.length === 0 ? (
                  <div className="srv-empty">
                    {logFailed ? t('Сбоев не было.') : t('Пока пусто.')}
                  </div>
                ) : logShown.length === 0 ? (
                  <div className="muted small">
                    {logFailed
                      ? t('За {w} сбоев не было.', { w: winLabel })
                      : t('За {w} проверок нет.', { w: winLabel })}{' '}
                    {logFailed &&
                      t('последний — {t}', { t: fmtDateTime(logRows[0].ts) })}
                  </div>
                ) : (
                  <div className="log-list">
                    {logShown.map((s, i) => (
                      <div key={i} className="log-item">
                        <span className={`sdot sdot-${s.status}`} />
                        <span className="log-time muted small">{fmtDateTime(s.ts)}</span>
                        <span className="log-metric">
                          {s.latency_ms != null ? `${s.latency_ms} ${t('мс')}` : '—'}
                        </span>
                        <span className="log-msg mono small" title={s.message}>
                          {s.message || '—'}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {logHidden > 0 && (
                  <button className="ghost small stale-more" onClick={() => setOldLog(!oldLog)}>
                    {oldLog
                      ? t('скрыть прежние')
                      : t('показать прежние ({n})', { n: logHidden })}
                  </button>
                )}
              </div>
            )}

            {cur.id === 'deps' && (
              <div className="srv-sec">
                <CheckParentBar check={check} canEdit={!isViewer} onSaved={() => onSaved?.()} />
                {!parents.length && isViewer && <div className="srv-empty">{ok.deps}</div>}
                <div className="net-foot">
                  {t('Пока сервер молчит, алерт монитора ждет его, а сам монитор назван в сообщении про сервер')}
                </div>
              </div>
            )}

            {cur.id === 'alerts' && (
              <div className="srv-sec">
                {!isViewer ? (
                  <SnoozeControl
                    snoozeUntil={check.snooze_until}
                    busy={snoozing}
                    onSnooze={async (hours) => {
                      setSnoozing(true)
                      try {
                        await snoozeCheck(check.id, hours)
                        onSaved?.()
                      } catch (e) {
                        fail(e)
                      } finally {
                        setSnoozing(false)
                      }
                    }}
                  />
                ) : snoozed ? (
                  <div className="muted small">🔕 {ok.alerts}</div>
                ) : null}
                <div className="kv-list">
                  <div className="kv-row">
                    <span className="kv-k muted small">{t('Не слать алерты этого монитора:')}</span>
                    <span className="kv-v small">
                      {mutes.length
                        ? mutes.map((k) => t(MON_ALERT_KINDS.find((x) => x.k === k)?.label ?? k)).join(', ')
                        : t('все виды включены')}
                      {!isViewer && (
                        <>
                          {' '}
                          <button className="ghost small" onClick={startEdit}>
                            {t('изменить')}
                          </button>
                        </>
                      )}
                    </span>
                  </div>
                  {check.type !== 'push' && check.alert_after_failures > 0 && (
                    <div className="kv-row">
                      <span className="kv-k muted small">{t('Когда уходит алерт')}</span>
                      <span className="kv-v small">
                        {t('после {n} неудачных проверок подряд, это около {m} мин', { n: check.alert_after_failures, m: alertAfterMin })}
                      </span>
                    </div>
                  )}
                </div>
                {check.known_issue ? (
                  <KnownCheckBar
                    known={check.known_issue}
                    canEdit={!isViewer}
                    onDrop={async () => {
                      await dropCheckKnown(check.id)
                      onSaved?.()
                    }}
                  />
                ) : !isViewer && (st === 'down' || st === 'degraded' || !!check.flaky_since) ? (
                  <div>
                    <button className="ghost small note-add" onClick={() => setKnownOpen(true)}>
                      📌 {t('отметить как известную проблему')}
                    </button>
                  </div>
                ) : null}
              </div>
            )}
          </div>
        </div>
          </>
        )}
      </div>
    </div>
    {knownOpen && (
      <KnownDialog
        title={check.name}
        onClose={() => setKnownOpen(false)}
        onSave={async (text, days) => {
          await setCheckKnown(check.id, text, days)
          setKnownOpen(false)
          onSaved?.()
        }}
      />
    )}
    {run && <RunVerdict run={run} onClose={closeRun} />}
    {zoomOpen && (
      <CheckChartModal
        check={check}
        isCert={isCert}
        locationId={selDirect ? undefined : selectedLoc ?? undefined}
        locName={selName}
        onClose={() => setZoomOpen(false)}
      />
    )}
    </>
  )
}

function CheckChartModal({
  check,
  isCert,
  locationId,
  locName,
  onClose,
}: {
  check: Check
  isCert: boolean
  locationId?: number
  locName?: string
  onClose: () => void
}) {
  const { t } = useI18n()
  const [hours, setHours] = useState(24)
  const [zoom, setZoom] = useState<{ from: number; to: number } | null>(null)
  const [points, setPoints] = useState<CheckSample[] | null>(null)

  useEffect(() => {
    setPoints(null)
    const run = () =>
      checkHistory(check.id, hours, locationId, zoom ?? undefined)
        .then((h) => setPoints(h.points))
        .catch(() => {})
    run()
    if (zoom) return
    const id = window.setInterval(run, 15000)
    return () => window.clearInterval(id)
  }, [check.id, hours, locationId, zoom])

  const ts = (points ?? []).map((p) => new Date(p.ts).getTime())
  const phased = !isCert && !locationId ? phaseSeries(points, t) : null
  const series = phased ?? [
    {
      name: isCert ? t('дн.') : t('мс'),
      color: isCert ? '#22c55e' : '#4b74ff',
      values: (points ?? []).map((p) => (isCert ? p.value : p.latency_ms)),
    },
  ]
  const spanH = zoom ? (zoom.to - zoom.from) / 3600 : hours
  const fmtT =
    spanH <= 24
      ? (ms: number) =>
          new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : (ms: number) =>
          new Date(ms).toLocaleDateString([], { day: '2-digit', month: '2-digit' })
  const title = isCert
    ? t('Осталось дней сертификата')
    : locName
      ? t('Время ответа · {loc}', { loc: locName })
      : t('Время ответа')

  return (
    <div className="modal-backdrop chart-full-backdrop">
      <div className="card chart-full">
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot sdot-${check.last_status}`} />
            <h3>{check.name}</h3>
            <span className="chart-full-metric">{title}</span>
          </div>
          <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
            ✕
          </button>
        </div>
        <div className="srv-meta">
          <span className="srv-meta-item mono">
            {check.target}
            {check.port ? `:${check.port}` : ''}
          </span>
        </div>
        <div className="detail-toolbar chart-full-bar">
          <div className="win-switch">
            {WINDOWS.map((w) => (
              <button
                key={w.key}
                className={`win-btn${!zoom && hours === w.hours ? ' win-btn-active' : ''}`}
                onClick={() => {
                  setZoom(null)
                  setHours(w.hours)
                }}
              >
                {t(w.label)}
              </button>
            ))}
          </div>
          {zoom ? (
            <button className="ghost" onClick={() => setZoom(null)}>
              {t('Сбросить зум')} ✕
            </button>
          ) : (
            <span className="muted small">{t('Выделите участок мышью, чтобы приблизить')}</span>
          )}
        </div>
        {points == null ? (
          <div className="chart-empty">{t('загрузка…')}</div>
        ) : ts.length ? (
          <>
          {!isCert && (
            <div className="chart-full-status">
              <StatusBar
                segments={(points ?? []).map((p) => ({
                  status: p.status,
                  title: `${fmtDateTime(p.ts)} · ${statusWord(p.status, t)}`,
                }))}
              />
            </div>
          )}
          <StackedAreaChart
            ts={ts}
            series={series}
            fmtY={(v) => `${Math.round(v)}`}
            fmtV={(v) =>
              isCert ? `${Math.round(v)} ${t('дн.')}` : `${Math.round(v)} ${t('мс')}`
            }
            fmtTime={fmtT}
            height={420}
            onZoom={(f, to) => setZoom({ from: f / 1000, to: to / 1000 })}
          />
          </>
        ) : (
          <div className="chart-empty">{t('Нет данных за период')}</div>
        )}
      </div>
    </div>
  )
}

function statusWord(
  st: CheckStatus,
  t: (k: string, p?: Record<string, string | number>) => string,
): string {
  return st === 'up'
    ? t('работает')
    : st === 'degraded'
      ? t('деградация')
      : st === 'down'
        ? t('недоступен')
        : t('нет данных')
}

/** Всплывающий ответ на «Проверить сейчас»: крупно — работает или нет, ниже — почему
 *  и откуда проверяли. Успех сам уходит через несколько секунд; сбой и «не удалось
 *  проверить» висят, пока их не закроют: их надо прочитать. */
function RunVerdict({ run, onClose }: { run: RunState; onClose: () => void }) {
  const { t } = useI18n()
  const [, setTick] = useState(0)
  useEffect(() => {
    if (run.phase !== 'running') return
    const id = window.setInterval(() => setTick((x) => x + 1), 1000)
    return () => window.clearInterval(id)
  }, [run.phase])
  const autoHide = run.phase === 'done' && run.result.run_status === 'up'
  useEffect(() => {
    if (!autoHide) return
    const id = window.setTimeout(onClose, 8000)
    return () => window.clearTimeout(id)
  }, [autoHide, onClose, run])

  let tone: string
  let icon: React.ReactNode
  let title: string
  let text = ''
  let meta = ''
  if (run.phase === 'running') {
    tone = 'running'
    icon = <span className="run-spin run-spin-lg" />
    title = t('Проверяем…')
    const secs = Math.max(0, Math.round((Date.now() - run.since) / 1000))
    if (run.source === 'agent') {
      text = run.server
        ? t('Сайт проверяет агент изнутри сервера {srv}', { srv: run.server })
        : t('Сайт проверяет агент изнутри сервера')
      if (run.pending != null && !run.fast) meta = t('обычно это до 15 секунд')
    } else {
      text = t('Запрос к сайту идёт прямо сейчас')
    }
    if (secs >= 2) meta = meta ? `${meta} · ${secs} ${t('с')}` : `${secs} ${t('с')}`
  } else if (run.phase === 'failed') {
    tone = 'failed'
    icon = '!'
    title = t('Проверить не удалось')
    text = run.message
  } else {
    const r = run.result
    const st = r.run_status || r.last_status
    tone = st
    icon = st === 'up' ? '✓' : st === 'degraded' ? '!' : '✕'
    title =
      st === 'up' ? t('Работает') : st === 'degraded' ? t('Работает с замечаниями') : t('Не работает')
    text = r.run_message
    const where =
      r.run_source === 'agent'
        ? t('проверено изнутри сервера {srv}', { srv: r.run_server })
        : t('проверено из панели')
    const when = r.run_at
      ? new Date(r.run_at).toLocaleTimeString([], {
          hour: '2-digit', minute: '2-digit', second: '2-digit',
        })
      : ''
    meta = when ? `${where} · ${when}` : where
  }
  return (
    <div className={`run-verdict run-verdict-${tone}`} role="status" aria-live="polite">
      <span className="run-verdict-ic">{icon}</span>
      <div className="run-verdict-body">
        <div className="run-verdict-title">{title}</div>
        {text && <div className="run-verdict-text">{text}</div>}
        {meta && <div className="run-verdict-meta">{meta}</div>}
      </div>
      <button className="ghost icon-btn run-verdict-x" onClick={onClose} title={t('Закрыть')}>
        ✕
      </button>
    </div>
  )
}

function ExpiryPill({
  icon,
  label,
  days,
  warn,
  message,
}: {
  icon: string
  label: string
  days: number | null
  warn: number[]
  message: string
}) {
  const { t } = useI18n()
  const tn = days == null ? '' : expiryTone(days, warn) || 't-up'
  const text = days == null ? message || t('нет данных') : expiryText(days, t)
  return (
    <div className="expiry-pill" title={message}>
      <span className="expiry-ico">{icon}</span>
      <div className="expiry-body">
        <div className="expiry-lbl">{label}</div>
        <div className={`expiry-days ${tn}`}>{text}</div>
      </div>
    </div>
  )
}

// Адрес push-монитора и как его дергать: из крона после успешного прогона или всегда, с кодом выхода.
function PushInfo({ check, canEdit }: { check: Check; canEdit: boolean }) {
  const { t } = useI18n()
  const [token, setToken] = useState(check.push_token || '')
  const [busy, setBusy] = useState(false)
  const url = `${window.location.origin}/api/agent/push/${token}`
  const ok = `/path/to/backup.sh && curl -fsS -m 10 --retry 3 ${url} >/dev/null`
  const always = `/path/to/backup.sh; curl -fsS -m 10 --retry 3 "${url}?status=$?" >/dev/null`
  const rotate = async () => {
    if (!window.confirm(t('Выдать новый адрес? Старый перестанет приниматься сразу, скрипт надо будет поправить.'))) return
    setBusy(true)
    try {
      const c = await rotatePushToken(check.id)
      setToken(c.push_token || '')
    } finally {
      setBusy(false)
    }
  }
  const copy = (s: string) => navigator.clipboard?.writeText(s)
  const last = (
    <div className="muted small">
      {check.push_at
        ? t('Последний сигнал: {when}', { when: new Date(check.push_at).toLocaleString() })
        : t('Сигнала еще не было')}
      {check.push_fail ? ` · ${t('скрипт сообщил об ошибке: {msg}', { msg: check.push_fail })}` : ''}
    </div>
  )
  // токен - секрет, роль "только просмотр" его не получает
  if (!token) {
    return (
      <div className="push-info">
        {last}
        <div className="muted small">{t('Адрес для скрипта видят админ и редактор')}</div>
      </div>
    )
  }
  return (
    <div className="push-info">
      <div className="muted small">{t('Адрес для скрипта (секрет, не публикуйте):')}</div>
      <div className="agent-advice-cmd">
        <pre>{url}</pre>
        <button className="ghost" onClick={() => copy(url)}>{t('Копировать')}</button>
      </div>
      <div className="muted small">{t('Сигнал только после успешного прогона:')}</div>
      <div className="agent-advice-cmd">
        <pre>{ok}</pre>
        <button className="ghost" onClick={() => copy(ok)}>{t('Копировать')}</button>
      </div>
      <div className="muted small">{t('Сигнал всегда, с кодом выхода (ненулевой = ошибка, придет сразу):')}</div>
      <div className="agent-advice-cmd">
        <pre>{always}</pre>
        <button className="ghost" onClick={() => copy(always)}>{t('Копировать')}</button>
      </div>
      {last}
      {canEdit && (
        <button className="ghost" disabled={busy} onClick={rotate}>
          {t('Выдать новый адрес')}
        </button>
      )}
    </div>
  )
}

// DNS-монитор: что спрашиваем, что ждем или что запомнили, что ответили сейчас. Ответ сменился
// по плану (переехали) - "принять" делает его правильным, монитор зеленеет сразу.
function DnsInfo({ check, canEdit, onAccepted }: { check: Check; canEdit: boolean; onAccepted: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const expect = (check.dns_expect ?? '').split('\n').map((x) => x.trim()).filter(Boolean)
  const base = check.dns_baseline ?? []
  const last = check.dns_last ?? []
  const same = (a: string[], b: string[]) => [...a].sort().join('|') === [...b].sort().join('|')
  const changed = expect.length === 0 && base.length > 0 && last.length > 0 && !same(base, last)
  const accept = async () => {
    setBusy(true)
    setErr('')
    try {
      await acceptDns(check.id)
      onAccepted()
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="dns-info">
      {/* имя, тип записи и резолвер - в строке под названием монитора */}
      <div className="muted small">
        {expect.length > 0
          ? `${check.dns_match === 'contains' ? t('ждем, что ответ содержит') : t('ждем ответ')}: ${expect.join(', ')}`
          : base.length > 0
            ? `${t('запомнили')}: ${base.join(', ')}`
            : t('ответа еще не было: первый запомним и будем следить за изменениями')}
      </div>
      {last.length > 0 && (
        <div className="small">
          {t('сейчас')}: <span className="mono">{last.join(', ')}</span>
        </div>
      )}
      {changed && canEdit && (
        <button className="ghost small" disabled={busy} onClick={accept}>
          {t('Принять новый ответ как правильный')}
        </button>
      )}
      {err && <div className="muted small">{err}</div>}
    </div>
  )
}
