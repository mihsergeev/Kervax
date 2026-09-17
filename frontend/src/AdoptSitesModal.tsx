import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  adoptDomains,
  type AdoptedDomain,
  domainProbes,
  ignoreDomains,
  probeDomains,
  type DomainProbe,
  type ProbeStatus,
} from './api'
import { useI18n } from './i18n'

// Мастер «Поставить на мониторинг»: список доменов, найденных агентами на веб-серверах
// (nginx/Apache/Caddy, Ingress, Gateway API), с отметкой галочками и созданием мониторов.
//
// Два входа, один компонент: из «Сервисов» приходит список одного веб-сервиса, из
// «Сайтов» — весь парк. Разница только во входных данных, поведение одинаковое.

export type AdoptItem = { domain: string; servers?: string[] }

// Годится ли имя в монитор. Та же логика, что на бэкенде (_adopt_problem в
// backend/app/api/checks.py): маску и regexp проверять нечем — HTTP-монитору нужен
// конкретный адрес.
export function adoptable(d: string): boolean {
  return !!d && !d.includes('*') && !d.startsWith('~') && d.includes('.')
}

// за раз столько же, сколько принимает POST /api/checks/adopt
const ADOPT_MAX = 500
// и сколько принимает POST /api/checks/discovered/probe
const PROBE_MAX = 300

type T = (s: string, p?: Record<string, string | number>) => string
type Mode = 'ext' | 'local'

const works = (s?: ProbeStatus) => s === 'up' || s === 'degraded'
const waiting = (p: DomainProbe) => p.ext_status === 'pending' || p.local_status === 'pending'

// Что не так — одним словом: строк в мастере бывает сотня, и полную причину («сервер не
// ответил на попытку соединения (ConnectTimeout)») читать в каждой некому. Она в подсказке.
function kindText(kind: string, code: number, t: T): string {
  switch (kind) {
    case 'http': return `HTTP ${code}`
    case 'dns': return t('нет в DNS')
    case 'tls': return t('ошибка TLS')
    case 'refused': return t('порт закрыт')
    case 'timeout': return t('нет ответа')
    case 'reset': return t('обрыв связи')
    case 'connect': return t('нет связи')
    case 'offline': return t('нода офлайн')
    case 'no_answer': return t('агент молчит')
    default: return t('ошибка')
  }
}

function agoText(iso: string, t: T): string {
  const sec = Math.max(0, (Date.now() - Date.parse(iso)) / 1000)
  if (sec < 60) return t('только что')
  if (sec < 3600) return t('{n} мин назад', { n: Math.round(sec / 60) })
  return t('{n} ч назад', { n: Math.round(sec / 3600) })
}

// Вариант проверки монитора — снаружи или изнутри сервера — вместе с итогом разовой
// проверки. Выделенный вариант и будет у монитора: по умолчанию снаружи, а если снаружи
// сайт не открылся, но открылся изнутри (белый список) — изнутри.
function ProbeChoice({ p, mode, onMode }: { p: DomainProbe; mode: Mode; onMode: (m: Mode) => void }) {
  const { t } = useI18n()
  const side = (key: Mode) => {
    const ext = key === 'ext'
    const status = ext ? p.ext_status : p.local_status
    const lat = ext ? p.ext_latency_ms : p.local_latency_ms
    const msg = ext ? p.ext_message : p.local_message
    const icon =
      status === 'pending' ? <span className="run-spin" />
        : status === 'up' ? '✓'
          : status === 'degraded' ? '⚠'
            : status === 'down' ? '✗' : '—'
    const text =
      status === 'pending' ? t('проверяем…')
        : works(status) ? (lat != null ? `${lat} ${t('мс')}` : t('работает'))
          : kindText(ext ? p.ext_kind : p.local_kind, ext ? p.ext_code : p.local_code, t)
    const about = ext
      ? t('Снаружи: монитор проверяет панель — как посетитель сайта.')
      : t('Изнутри сервера {srv}: монитор проверяет агент на нём — для сайтов, закрытых снаружи белым списком.', {
          srv: p.local_server || '—',
        })
    return (
      <button
        type="button"
        role="radio"
        aria-checked={mode === key}
        className={`adopt-probe probe-${status}${mode === key ? ' on' : ''}`}
        title={msg ? `${about}\n${msg}` : about}
        onClick={() => onMode(key)}
      >
        <span className="adopt-probe-lbl">{ext ? t('снаружи') : t('изнутри')}</span>
        <span className="adopt-probe-ic">{icon}</span>
        <span className="adopt-probe-txt">{text}</span>
      </button>
    )
  }
  return (
    <span className="adopt-probes" role="radiogroup" aria-label={t('Откуда проверять')}>
      {side('ext')}
      {side('local')}
    </span>
  )
}

// Итог «Поставить на мониторинг»: что заведено и как будет проверяться, что нет и почему.
// Раньше добавленные строки просто пропадали из списка, оставалась строчка «создано
// мониторов: 5» над пустым списком — что добавилось, а что нет, было не понять.
type Report = { group: string; items: AdoptedDomain[]; wantedLocal: string[] }

function AdoptReport({
  report,
  left,
  onMore,
  onClose,
  onOpenCheck,
}: {
  report: Report
  left: number // сколько новых доменов осталось вне мониторинга
  onMore: () => void
  onClose: () => void
  onOpenCheck?: (id: number) => void
}) {
  const { t } = useI18n()
  const added = report.items.filter((i) => !i.reason)
  const missed = report.items.filter((i) => i.reason)
  const open = (i: AdoptedDomain) =>
    i.check_id > 0 && onOpenCheck ? (
      <button className="linklike adopt-open" onClick={() => onOpenCheck(i.check_id)}>
        {t('открыть монитор')}
      </button>
    ) : null
  const how = (i: AdoptedDomain) =>
    i.local ? (
      <span
        className="adopt-how"
        title={t('Изнутри сервера {srv}: монитор проверяет агент на нём — для сайтов, закрытых снаружи белым списком.', { srv: i.server })}
      >
        {t('изнутри')} · {i.server}
      </span>
    ) : report.wantedLocal.includes(i.domain) ? (
      // просили изнутри, а домен уже не найден ни на одной ноде
      <span
        className="adopt-how t-degraded"
        title={t('Домен не нашёлся ни на одной ноде — проверять изнутри некому, поэтому монитор проверяет панель снаружи.')}
      >
        {t('снаружи — изнутри некому')}
      </span>
    ) : (
      <span className="adopt-how" title={t('Снаружи: монитор проверяет панель — как посетитель сайта.')}>
        {t('снаружи')}
      </span>
    )
  return (
    <>
      <div className={`adopt-report-head${added.length === 0 ? ' none' : ''}`}>
        <span className="adopt-report-ic">{added.length > 0 ? '✓' : '—'}</span>
        <span className="adopt-report-title">
          {added.length > 0
            ? t('Поставлено на мониторинг: {n}', { n: added.length })
            : t('Ничего не добавлено')}
        </span>
        {added.length > 0 && (
          <span className="muted small">
            {report.group ? t('группа «{g}»', { g: report.group }) : t('без группы')}
          </span>
        )}
      </div>
      <div className="adopt-list adopt-report">
        {added.map((i) => (
          <div key={i.domain} className="adopt-row">
            <span className="adopt-mark up">✓</span>
            <span className="adopt-dom mono">{i.domain}</span>
            {how(i)}
            {open(i)}
          </div>
        ))}
        {missed.length > 0 && (
          <div className="adopt-report-sep muted small">
            {t('не добавлены: {n}', { n: missed.length })}
          </div>
        )}
        {missed.map((i) => (
          <div key={i.domain} className="adopt-row adopt-monitored">
            <span className="adopt-mark">—</span>
            <span className="adopt-dom mono">{i.domain}</span>
            <span className="adopt-how">
              {i.reason === 'monitored' ? t('уже на мониторинге') : t(i.problem)}
            </span>
            {open(i)}
          </div>
        ))}
      </div>
      <div className="adopt-foot">
        <span className="muted small">
          {left > 0
            ? t('вне мониторинга осталось доменов: {n}', { n: left })
            : t('новых доменов вне мониторинга не осталось')}
        </span>
        <span className="adopt-foot-btns">
          {left > 0 && (
            <button className="ghost" onClick={onMore}>
              {t('добавить ещё')}
            </button>
          )}
          <button className="primary" onClick={onClose}>
            {t('Готово')}
          </button>
        </span>
      </div>
    </>
  )
}

// Домены второго уровня, за которыми регистрируют третий: для них «зона» — три метки,
// иначе shop.msk.ru и blog.msk.ru слиплись бы в одну кучу «msk.ru». Полный PSL сюда
// тащить не за чем — список нужен только для ГРУППИРОВКИ в списке, ошибка не критична.
const SLD = new Set([
  'co', 'com', 'net', 'org', 'edu', 'gov', 'mil', 'ac', 'or', 'ne', 'go',
  'msk', 'spb', 'nnov', 'sochi', 'pp', 'in', 'biz', 'info',
])

// «Зона» домена — то, по чему группируем список (обычно регистрируемое имя).
export function zoneOf(domain: string): string {
  const parts = domain.replace(/^\*\./, '').split('.').filter(Boolean)
  if (parts.length <= 2) return parts.join('.')
  const take = SLD.has(parts[parts.length - 2]) ? 3 : 2
  return parts.slice(-take).join('.')
}

// new — можно завести; monitored — уже есть монитор; bad — маска/regexp;
// skipped — человек сказал «мониторить не нужно» (общий список на панель)
type Status = 'new' | 'monitored' | 'bad' | 'skipped'

type Row = { domain: string; servers: string[]; status: Status; checkId: number }

export function AdoptSitesModal({
  title,
  items,
  hosts,
  groups,
  ignored,
  onClose,
  onDone,
  onIgnored,
  onOpenCheck,
}: {
  title: string
  items: AdoptItem[]
  hosts: Record<string, number>
  groups: string[]
  ignored?: string[] // домены, помеченные «не нужен» — прячем из предложений
  onClose: () => void
  onDone: (hosts: Record<string, number>, created: number) => void
  onIgnored?: (domains: string[], ignore: boolean) => void
  onOpenCheck?: (id: number) => void
}) {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const [group, setGroup] = useState('')
  const [onlyNew, setOnlyNew] = useState(true)
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  // итог последнего добавления: пока он есть, мастер показывает его вместо списка
  const [report, setReport] = useState<Report | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  // локальная копия «ненужных»: правим её сразу, не дожидаясь перезагрузки списка
  const [skip, setSkip] = useState<Set<string>>(() => new Set(ignored || []))
  const [showSkipped, setShowSkipped] = useState(false)
  // Разовая проверка предложенных доменов: итоги, домены с запросом в пути и выбор
  // варианта, если человек его поменял (иначе вариант следует из итогов, см. modeOf).
  const [probes, setProbes] = useState<Record<string, DomainProbe>>({})
  const [inflight, setInflight] = useState<Set<string>>(new Set())
  const [probeErr, setProbeErr] = useState('')
  const [modes, setModes] = useState<Record<string, Mode>>({})
  const requested = useRef<Set<string>>(new Set())

  // строки со статусом: считаем один раз на смену входных данных/карты мониторов
  const rows: Row[] = useMemo(() => {
    const seen = new Map<string, Row>()
    for (const it of items) {
      const domain = it.domain.trim().toLowerCase()
      if (!domain) continue
      const id = hosts[domain]
      const row = seen.get(domain)
      if (row) {
        // один домен может висеть на нескольких нодах — склеиваем источники
        for (const s of it.servers || []) if (!row.servers.includes(s)) row.servers.push(s)
        continue
      }
      seen.set(domain, {
        domain,
        servers: [...(it.servers || [])],
        status:
          id != null
            ? 'monitored'
            : skip.has(domain)
              ? 'skipped'
              : adoptable(domain)
                ? 'new'
                : 'bad',
        checkId: id ?? 0,
      })
    }
    return [...seen.values()].sort((a, b) => a.domain.localeCompare(b.domain))
  }, [items, hosts, skip])

  const ql = q.trim().toLowerCase()
  const visible = rows.filter(
    (r) =>
      // «не нужные» не мешаются под ногами, пока их явно не попросят показать
      (r.status !== 'skipped' || showSkipped) &&
      (!onlyNew || r.status === 'new' || (showSkipped && r.status === 'skipped')) &&
      (!ql || r.domain.includes(ql) || r.servers.some((s) => s.toLowerCase().includes(ql))),
  )

  // зоны в порядке «где больше доступного к добавлению» — самое полезное сверху
  const zones = useMemo(() => {
    const by = new Map<string, Row[]>()
    for (const r of visible) {
      const z = zoneOf(r.domain)
      const list = by.get(z)
      if (list) list.push(r)
      else by.set(z, [r])
    }
    return [...by.entries()]
      .map(([zone, list]) => ({ zone, list, fresh: list.filter((r) => r.status === 'new') }))
      .sort((a, b) => b.fresh.length - a.fresh.length || a.zone.localeCompare(b.zone))
  }, [visible])

  // ——— разовая проверка доступности ———
  const newDomains = rows.filter((r) => r.status === 'new').map((r) => r.domain)
  const newKey = newDomains.join(' ')

  // Свежее не затираем старым: опрос, ушедший до «перепроверить», мог вернуться позже
  // и вернуть прошлый итог — ожидание бы оборвалось, а новый итог так и не показался.
  const merge = (items: DomainProbe[]) =>
    setProbes((prev) => {
      const next = { ...prev }
      for (const it of items) {
        const cur = next[it.domain]
        if (!cur || Date.parse(it.started_at) >= Date.parse(cur.started_at)) next[it.domain] = it
      }
      return next
    })

  const startProbe = async (domains: string[], force: boolean) => {
    if (domains.length === 0) return
    setProbeErr('')
    setInflight((prev) => new Set([...prev, ...domains]))
    try {
      for (let i = 0; i < domains.length; i += PROBE_MAX) {
        const res = await probeDomains(domains.slice(i, i + PROBE_MAX), force)
        merge(res.items)
      }
    } catch (e) {
      setProbeErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setInflight((prev) => {
        const next = new Set(prev)
        for (const d of domains) next.delete(d)
        return next
      })
    }
  }

  // Открыли мастер (или вернули домен в предложения) — проверяем то, что ещё не
  // спрашивали. Свежий итог панель отдаст как есть, по сайтам заново не пойдёт.
  useEffect(() => {
    const need = newKey ? newKey.split(' ').filter((d) => !requested.current.has(d)) : []
    for (const d of need) requested.current.add(d)
    void startProbe(need, false)
    // startProbe пересоздаётся на каждый рендер, а запускать проверку надо только
    // на смену набора доменов
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [newKey])

  const pendingAny = newDomains.some((d) => probes[d] && waiting(probes[d]))
  useEffect(() => {
    if (!pendingAny) return
    const id = window.setInterval(() => {
      domainProbes()
        .then((r) => merge(r.items))
        .catch(() => {})
    }, 2000)
    return () => window.clearInterval(id)
  }, [pendingAny])

  const modeOf = (domain: string): Mode => {
    const chosen = modes[domain]
    if (chosen) return chosen
    const p = probes[domain]
    // снаружи — пока не доказано, что снаружи не работает, а изнутри работает
    if (!p || works(p.ext_status) || p.ext_status === 'pending') return 'ext'
    return works(p.local_status) ? 'local' : 'ext'
  }
  const chosenWorks = (domain: string) => {
    const p = probes[domain]
    return !!p && works(modeOf(domain) === 'local' ? p.local_status : p.ext_status)
  }

  const probeRows = newDomains.filter((d) => probes[d] || inflight.has(d))
  const probeDone = newDomains.filter((d) => probes[d] && !waiting(probes[d]) && !inflight.has(d))
  const probeRunning = probeRows.length - probeDone.length
  const extOk = probeDone.filter((d) => works(probes[d].ext_status)).length
  const localOnly = probeDone.filter(
    (d) => !works(probes[d].ext_status) && works(probes[d].local_status),
  ).length
  const dead = probeDone.length - extOk - localOnly
  const checkedAt = probeDone
    .map((d) => probes[d].started_at)
    .sort((a, b) => Date.parse(a) - Date.parse(b))[0]

  const freshVisible = visible.filter((r) => r.status === 'new')
  const availVisible = freshVisible.filter((r) => chosenWorks(r.domain))
  const picked = freshVisible.filter((r) => sel.has(r.domain))
  const pickedLocal = picked.filter((r) => modeOf(r.domain) === 'local').length
  const total = rows.length
  const newTotal = rows.filter((r) => r.status === 'new').length
  const skipTotal = rows.filter((r) => r.status === 'skipped').length

  const toggle = (domain: string) => {
    setSel((prev) => {
      const next = new Set(prev)
      if (next.has(domain)) next.delete(domain)
      else next.add(domain)
      return next
    })
  }
  const setMany = (list: Row[], on: boolean) => {
    setSel((prev) => {
      const next = new Set(prev)
      for (const r of list) {
        if (on) next.add(r.domain)
        else next.delete(r.domain)
      }
      return next
    })
  }
  const toggleZone = (zone: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(zone)) next.delete(zone)
      else next.add(zone)
      return next
    })
  }

  // «мониторить не нужно» / «вернуть в предложения»
  const mark = async (domains: string[], ignore: boolean) => {
    if (domains.length === 0) return
    setErr('')
    try {
      await ignoreDomains(domains, ignore)
      setSkip((prev) => {
        const next = new Set(prev)
        for (const d of domains) {
          if (ignore) next.add(d)
          else next.delete(d)
        }
        return next
      })
      setSel((prev) => {
        const next = new Set(prev)
        for (const d of domains) next.delete(d)
        return next
      })
      onIgnored?.(domains, ignore)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    }
  }

  const submit = async () => {
    setBusy(true)
    setErr('')
    try {
      const chunk = picked.slice(0, ADOPT_MAX).map((r) => r.domain)
      const local = chunk.filter((d) => modeOf(d) === 'local')
      const res = await adoptDomains(chunk, group.trim(), local)
      setSel((prev) => {
        const next = new Set(prev)
        for (const d of chunk) next.delete(d)
        return next
      })
      setReport({ group: res.group_name, items: res.items, wantedLocal: local })
      onDone(res.hosts, res.created)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal adopt-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{title}</h3>
          <button className="ghost" onClick={onClose}>{t('Закрыть')}</button>
        </div>

        {report ? (
          <AdoptReport
            report={report}
            left={newTotal}
            onMore={() => setReport(null)}
            onClose={onClose}
            onOpenCheck={onOpenCheck}
          />
        ) : (
          <>
            <div className="adopt-sub muted small">
              {t('найдено доменов: {n}', { n: total })}
              {' · '}
              {newTotal > 0
                ? t('вне мониторинга: {n}', { n: newTotal })
                : t('все уже под мониторингом')}
              {skipTotal > 0 && (
                <>
                  {' · '}
                  <button className="linklike" onClick={() => setShowSkipped(!showSkipped)}>
                    {showSkipped
                      ? t('скрыть ненужные ({n})', { n: skipTotal })
                      : t('ненужных: {n}', { n: skipTotal })}
                  </button>
                </>
              )}
            </div>

            <div className="adopt-tools">
              <input
                className="adopt-search mono"
                placeholder={t('фильтр по домену или ноде…')}
                value={q}
                onChange={(e) => setQ(e.target.value)}
              />
              <label className="adopt-chk">
                <input
                  type="checkbox"
                  checked={onlyNew}
                  onChange={(e) => setOnlyNew(e.target.checked)}
                />
                {t('только новые')}
              </label>
              <button
                className="ghost"
                disabled={freshVisible.length === 0}
                onClick={() => setMany(freshVisible, true)}
              >
                {t('выбрать все ({n})', { n: freshVisible.length })}
              </button>
              {probeRunning === 0 && availVisible.length > 0 && availVisible.length < freshVisible.length && (
                <button className="ghost" onClick={() => setMany(availVisible, true)}>
                  {t('выбрать доступные ({n})', { n: availVisible.length })}
                </button>
              )}
              <button className="ghost" disabled={sel.size === 0} onClick={() => setSel(new Set())}>
                {t('снять выбор')}
              </button>
              {picked.length > 0 && (
                <button
                  className="ghost"
                  onClick={() => mark(picked.map((r) => r.domain), true)}
                >
                  {t('не нужны ({n})', { n: picked.length })}
                </button>
              )}
            </div>

            {(probeRows.length > 0 || probeErr) && (
              <div className="adopt-probe-sum small">
                {probeErr ? (
                  <span className="t-down">
                    {t('Проверить доступность не удалось: {err}', { err: probeErr })}
                  </span>
                ) : probeRunning > 0 ? (
                  <>
                    <span className="run-spin" />
                    <span>
                      {t('Проверяем доступность снаружи и изнутри серверов: готово {n} из {m}', {
                        n: probeDone.length,
                        m: probeRows.length,
                      })}
                    </span>
                    <span className="adopt-probe-bar">
                      <span style={{ width: `${Math.round((probeDone.length / probeRows.length) * 100)}%` }} />
                    </span>
                  </>
                ) : (
                  <>
                    <span className={extOk > 0 ? 't-up' : 'muted'}>
                      {t('открываются снаружи: {n}', { n: extOk })}
                    </span>
                    {localOnly > 0 && <span>{t('только изнутри сервера: {n}', { n: localOnly })}</span>}
                    {dead > 0 && <span className="t-down">{t('не открываются: {n}', { n: dead })}</span>}
                    {checkedAt && <span className="muted">{t('проверено {ago}', { ago: agoText(checkedAt, t) })}</span>}
                    <button
                      className="linklike"
                      onClick={() => startProbe(newDomains, true)}
                      title={t('Проверить эти домены ещё раз — снаружи и изнутри серверов')}
                    >
                      {t('перепроверить')}
                    </button>
                  </>
                )}
              </div>
            )}

            <div className="adopt-list">
              {zones.length === 0 ? (
                <div className="muted small">
                  {onlyNew && newTotal === 0
                    ? t('Все найденные домены уже стоят на мониторинге.')
                    : t('ничего не найдено')}
                </div>
              ) : (
                zones.map(({ zone, list, fresh }) => {
                  const shut = collapsed.has(zone)
                  const on = fresh.length > 0 && fresh.every((r) => sel.has(r.domain))
                  return (
                    <div key={zone} className="adopt-zone">
                      <div className="adopt-zone-head">
                        <input
                          type="checkbox"
                          checked={on}
                          disabled={fresh.length === 0}
                          // частичный выбор внутри зоны — «квадратик», а не галка
                          ref={(el) => {
                            if (el) el.indeterminate = !on && fresh.some((r) => sel.has(r.domain))
                          }}
                          onChange={(e) => setMany(fresh, e.target.checked)}
                        />
                        <button className="adopt-zone-name" onClick={() => toggleZone(zone)}>
                          <span className="adopt-caret">{shut ? '▸' : '▾'}</span>
                          <span className="mono">{zone}</span>
                        </button>
                        <span className="muted small">
                          {fresh.length > 0
                            ? t('новых: {n} из {m}', { n: fresh.length, m: list.length })
                            : t('всё покрыто ({m})', { m: list.length })}
                        </span>
                      </div>
                      {!shut && (
                        <div className="adopt-rows">
                          {list.map((r) => (
                            <div key={r.domain} className={`adopt-row adopt-${r.status}`}>
                              {r.status === 'new' ? (
                                <input
                                  type="checkbox"
                                  checked={sel.has(r.domain)}
                                  onChange={() => toggle(r.domain)}
                                />
                              ) : r.status === 'monitored' ? (
                                r.checkId > 0 && onOpenCheck ? (
                                  <button
                                    className="adopt-mark up"
                                    title={t('Уже в мониторинге — открыть монитор')}
                                    onClick={() => onOpenCheck(r.checkId)}
                                  >
                                    ✓
                                  </button>
                                ) : (
                                  <span className="adopt-mark up" title={t('Уже в мониторинге')}>✓</span>
                                )
                              ) : (
                                <span
                                  className="adopt-mark"
                                  title={t('Маска или regexp — монитору нужен конкретный адрес')}
                                >
                                  —
                                </span>
                              )}
                              <span className="adopt-dom mono">{r.domain}</span>
                              {r.status === 'new' && probes[r.domain] ? (
                                <ProbeChoice
                                  p={probes[r.domain]}
                                  mode={modeOf(r.domain)}
                                  onMode={(m) => setModes((prev) => ({ ...prev, [r.domain]: m }))}
                                />
                              ) : r.status === 'new' && inflight.has(r.domain) ? (
                                <span className="adopt-probes adopt-probe-wait">
                                  <span className="run-spin" />
                                </span>
                              ) : null}
                              {r.servers.length > 0 && (
                                <span className="adopt-where muted small">{r.servers.join(', ')}</span>
                              )}
                              {r.status === 'new' && (
                                <button
                                  className="adopt-skip"
                                  title={t('Мониторить не нужно — убрать из предложений')}
                                  onClick={() => mark([r.domain], true)}
                                >
                                  ✕
                                </button>
                              )}
                              {r.status === 'skipped' && (
                                <button
                                  className="adopt-skip back"
                                  title={t('Вернуть в предложения')}
                                  onClick={() => mark([r.domain], false)}
                                >
                                  ↺
                                </button>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })
              )}
            </div>

            {err && <div className="form-error small">{err}</div>}

            {/* добавлять нечего — поле группы, «будет создано: 0» и серая кнопка только сбивают */}
            {newTotal > 0 && (
              <>
                <div className="adopt-foot">
                  <label className="adopt-grp">
                    {t('группа')}
                    <input
                      list="kervax-site-groups"
                      placeholder={t('без группы')}
                      value={group}
                      onChange={(e) => setGroup(e.target.value)}
                    />
                    <datalist id="kervax-site-groups">
                      {groups.map((g) => (
                        <option key={g} value={g} />
                      ))}
                    </datalist>
                  </label>
                  <span className="muted small">
                    {picked.length > ADOPT_MAX
                      ? t('выбрано {n}, за раз добавим {m}', { n: picked.length, m: ADOPT_MAX })
                      : pickedLocal > 0
                        ? t('будет создано мониторов: {n}, из них изнутри сервера: {m}', { n: picked.length, m: pickedLocal })
                        : t('будет создано мониторов: {n}', { n: picked.length })}
                  </span>
                  <button className="primary" disabled={busy || picked.length === 0} onClick={submit}>
                    {busy ? t('добавляем…') : t('Поставить на мониторинг')}
                  </button>
                </div>
                <div className="muted small adopt-hint">
                  {t('Создаётся HTTPS-монитор на каждый домен — с тем вариантом проверки, что выделен в строке: снаружи его проверяет панель, изнутри — агент на сервере сайта. Доступность здесь проверена один раз, итог хранится 12 часов.')}
                </div>
              </>
            )}
          </>
        )}
      </div>
    </div>,
    document.body,
  )
}
