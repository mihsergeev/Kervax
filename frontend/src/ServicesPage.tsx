import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  knownHosts,
  listServers,
  updateServer,
  type DBStat,
  type Server,
  type ServiceInfo,
} from './api'
import { useI18n } from './i18n'
import { useAuth } from './auth'
import { AdoptSitesModal, adoptable } from './AdoptSitesModal'
import { EngineIcon } from './engineIcon'
import { OsIcon } from './osIcon'
import { CountryFlag } from './CountryFlag'
import { byteUnits } from './units'

// Список сайтов веб-сервиса: превью + раскрытие в скроллящийся список с фильтром (у
// ingress-нод бывают сотни доменов — «10 + ещё 390» неюзабельно). Рядом с каждым —
// признак «уже под мониторингом»; завести новые можно кнопкой, она открывает общий
// мастер (AdoptSitesModal) — отмечать сотни доменов плюсиками по одному невозможно.
function SitesList({ sites, t, hosts, skip, onAdopt, onOpenCheck }: {
  sites: string[]
  t: (s: string, v?: Record<string, string | number>) => string
  hosts: Record<string, number> | null // null = список мониторов недоступен учётке
  skip: Set<string> // помеченные «мониторить не нужно» — в счётчик не идут
  onAdopt?: (domains: string[]) => void
  onOpenCheck?: (id: number) => void
}) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const PREVIEW = 6
  const ql = q.trim().toLowerCase()
  const filtered = ql ? sites.filter((s) => s.toLowerCase().includes(ql)) : sites
  const shown = open ? filtered : sites.slice(0, PREVIEW)
  const missing = hosts
    ? sites.filter(
        (s) => adoptable(s) && hosts[s.toLowerCase()] == null && !skip.has(s.toLowerCase()),
      )
    : []
  return (
    <div className="svc-sites">
      <div className="svc-sites-head">
        <span className="svc-sites-count">{t('сайтов: {n}', { n: sites.length })}</span>
        {sites.length > PREVIEW && (
          <button className="svc-act" onClick={() => setOpen(!open)}>
            {open ? t('свернуть') : t('показать все')}
          </button>
        )}
        {onAdopt && missing.length > 0 && (
          <button className="svc-act svc-act-primary" onClick={() => onAdopt(sites)}>
            {t('на мониторинг: {n}', { n: missing.length })}
          </button>
        )}
      </div>
      {open && sites.length > PREVIEW && (
        <input
          className="svc-sites-filter mono"
          placeholder={t('фильтр доменов…')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      )}
      <div className={`svc-sites-list ${open ? 'open' : ''}`}>
        {open && filtered.length === 0 ? (
          <div className="muted small">{t('ничего не найдено')}</div>
        ) : (
          <>
            {shown.map((s) => {
              const id = hosts?.[s.toLowerCase()]
              return (
                <div key={s} className="svc-site-row mono">
                  <span className="svc-site-name">{s}</span>
                  {id == null ? null : id > 0 && onOpenCheck ? (
                    <button
                      className="svc-site-on"
                      title={t('Уже в мониторинге — открыть монитор')}
                      onClick={() => onOpenCheck(id)}
                    >
                      ✓
                    </button>
                  ) : (
                    <span className="svc-site-on" title={t('Уже в мониторинге')}>✓</span>
                  )}
                </div>
              )
            })}
            {!open && sites.length > PREVIEW && (
              <div className="muted small svc-site-more">{t('…и ещё {n}', { n: sites.length - PREVIEW })}</div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

// Раздел «Сервисы»: реестр прикладных сервисов по парку, СЕРВЕР-ОРИЕНТИРОВАННО (как Докер/
// Кубер): сверху ноды, клик по ноде → её сервисы с деталями. Глубина есть у RabbitMQ
// (очереди через prometheus-плагин, без паролей/exec) и у СУБД (статус дампа из аудита).

type SvcItem = {
  kind: string // ClickHouse / Grafana / RabbitMQ / nginx / Envoy / …
  where: string // «контейнер X» / «под ns/name» / «процесс на хосте»
  inKube?: boolean // СУБД в поде — туда хелпер инвентаря не дотягивается
  svc?: ServiceInfo // метрики (очереди RabbitMQ)
  sites?: string[] // домены/сайты веб-сервиса (из k8s Ingress или nginx -T)
  web?: boolean // это веб-сервер/прокси — если доменов нет, объясняем почему
  db?: DBStat[] // инвентарь СУБД: ВСЕ инстансы этого движка на ноде (хелпер dbstat-setup)
}
type SrvServices = { server: Server; items: SvcItem[] }

const QUEUE_HOT = 100 // очередь глубже — подсвечиваем: обычно отставший консьюмер

// Ключ очереди ДОЛЖЕН совпадать с backend/app/collector.py::queue_key — по нему
// хранятся пороги отдельных очередей. Источник в ключе обязателен: на ноде бывает
// несколько инстансов RabbitMQ (dev/stage) с одинаковыми именами очередей.
function queueKey(source: string, q: { name: string; vhost?: string }): string {
  return `${source}|${q.vhost || '/'}/${q.name}`
}

function qDepth(q: { ready: number; unacked?: number }): number {
  return (q.ready || 0) + (q.unacked || 0)
}

// Инвентарь СУБД → карточке сервиса. Матчим по имени контейнера (оно есть и в «где»
// у находки аудита, и в инвентаре), а движки на хосте (container пустой) — по названию.
const DB_ENGINE_LABEL: Record<string, string> = {
  pg: 'PostgreSQL',
  mysql: 'MySQL/MariaDB',
  clickhouse: 'ClickHouse',
  redis: 'Redis',
}

// ВСЕ инстансы движка, а не один: на одной ноде бывает 37 postgres-контейнеров, а карточка в
// модалке одна на движок — показать инвентарь одного из них значило бы соврать.
function matchDbStats(kind: string, stats: DBStat[]): DBStat[] {
  return stats.filter((d) => DB_ENGINE_LABEL[d.engine] === kind)
}

function fmtBytes(n: number): string {
  const u = byteUnits()
  let v = n
  let i = 0
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${u[i]}`
}

// Инвентарь СУБД: сколько инстансов, самые крупные базы, логины, версия. Раньше карточка
// отвечала только «бэкапьте отдельно» — полезно, но не на первый вопрос «что внутри».
// Паролей тут нет by design: root-хелпер отдаёт лишь имена, размеры и версию.
function DbInventory({ db, t }: {
  db: DBStat[]
  t: (s: string, v?: Record<string, string | number>) => string
}) {
  const [open, setOpen] = useState(false)
  const keys = db[0]?.engine === 'redis' // у redis в size лежат ключи, а не байты
  const multi = db.length > 1
  // все базы всех инстансов, крупные сверху; при нескольких инстансах помечаем, чей это
  const rows = db
    .flatMap((d) => (d.dbs ?? []).map((x) => ({ ...x, cont: d.container ?? '' })))
    .sort((a, b) => b.size - a.size)
  const total = rows.reduce((a, r) => a + r.size, 0)
  const users = new Set(db.flatMap((d) => d.users ?? []))
  const vers = [...new Set(db.map((d) => d.version).filter(Boolean))]
  // Слоты подключений: показываем самый нагруженный инстанс движка. Число само по себе
  // ни о чём не говорит, пока не видно предела, поэтому всегда рядом с ним. Инстансы без
  // лимита (старый helper) пропускаем: 0 из 0 выглядело бы как исправное «свободно».
  const conn = db
    .filter((d) => (d.conn_max ?? 0) > 0)
    .map((d) => ({ used: d.conn_used ?? 0, max: d.conn_max ?? 0, cont: d.container ?? '' }))
    .sort((a, b) => b.used / b.max - a.used / a.max)[0]
  const connPct = conn ? Math.round((conn.used / conn.max) * 100) : 0
  const shown = open ? rows : rows.slice(0, 5)
  return (
    <div className="svc-db">
      <div className="svc-db-head muted small">
        {vers.length > 0 && (
          <span className="svc-db-ver mono" title={vers.join(', ')}>
            v{vers[0]}{vers.length > 1 && '…'}
          </span>
        )}
        {multi && <span>{t('инстансов: {n}', { n: db.length })}</span>}
        <span>
          {t('баз: {n}', { n: rows.length })}
          {rows.length > 0 && ` · ${keys ? t('ключей: {n}', { n: total }) : fmtBytes(total)}`}
        </span>
        {users.size > 0 && (
          <span title={[...users].join(', ')}>{t('логинов: {n}', { n: users.size })}</span>
        )}
        {conn && (
          <span
            className={connPct >= 85 ? 't-down' : connPct >= 70 ? 't-degraded' : undefined}
            title={t('Занято подключений на {c}', { c: conn.cont || t('этом движке') })}
          >
            {t('коннекты: {u} из {m} ({p}%)', { u: conn.used, m: conn.max, p: connPct })}
          </span>
        )}
        {rows.length > 5 && (
          <button className="svc-act" onClick={() => setOpen(!open)}>
            {open ? t('свернуть') : t('показать все')}
          </button>
        )}
      </div>
      <div className={open ? 'svc-db-list open' : 'svc-db-list'}>
        {shown.map((r, i) => (
          <div key={r.cont + '/' + r.name + i} className="svc-db-row">
            <span className="mono">
              {multi && r.cont && <span className="muted">{r.cont} / </span>}
              {r.name}
            </span>
            <span className="muted mono">
              {keys ? t('{n} ключей', { n: r.size }) : fmtBytes(r.size)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

// Почему у веб-сервиса не видно доменов. Пустая карточка читается как «сломалось»,
// хотя чаще это норма: контейнер-апстрим за прокси домена не знает (server_name
// localhost), а у Envoy маршруты вообще приходят по xDS. Отдельный случай — не
// выкачен хелпер: тогда доменов нет НИ У КОГО на ноде, и это чинится выкаткой.
function noSitesHint(
  kind: string,
  server: Server,
  t: (s: string, v?: Record<string, string | number>) => string,
): string {
  const helperOk = !!server.last_report?.setup_versions?.['webserver-setup']
  if (!helperOk) return t('домены не собраны: на ноде нет хелпера webserver-setup')
  if (kind === 'Envoy') return t('маршруты приходят по xDS — панель их не читает')
  if (kind === 'ingress-nginx') return t('в кластере нет Ingress/HTTPRoute с доменами')
  return t('домены не заданы — обычно это апстрим за прокси')
}

// СУБД без инвентаря: объясняем, чего не хватает, чтобы увидеть базы и размеры.
// Про бэкап здесь не пишем — это страница про сервисы.
function noDbHint(
  kind: string,
  inKube: boolean,
  server: Server,
  t: (s: string, v?: Record<string, string | number>) => string,
): string | null {
  // инвентарь снимает только dbstat-setup и только для этих движков
  if (!Object.values(DB_ENGINE_LABEL).includes(kind)) return null
  if (inKube) {
    return t('инвентарь снимается с хостовых и docker-баз — до баз в кластере хелпер не дотягивается')
  }
  if (!server.last_report?.setup_versions?.['dbstat-setup']) {
    return t('базы и размеры не собраны: на ноде нет хелпера dbstat-setup')
  }
  return t('база не ответила на опрос инвентаря')
}

// сервисы ноды: собственные метрики (services[]) + движки из аудита покрытия (без метрик)
function buildByServer(
  servers: Server[],
  t: (s: string, v?: Record<string, string | number>) => string,
): SrvServices[] {
  const out: SrvServices[] = []
  for (const s of servers) {
    if (!s.online) continue
    const items: SvcItem[] = []
    const seen = new Set<string>()
    for (const svc of s.last_report?.services ?? []) {
      const kind = svc.kind === 'rabbitmq' ? 'RabbitMQ' : svc.kind
      seen.add(kind)
      items.push({ kind, where: svc.source || '', svc })
    }
    // Аудит покрытия — единственное место, где перечислены СУБД ноды (в services[]
    // их нет), поэтому для ОБНАРУЖЕНИЯ он нужен. Но берём из него только «что и где»:
    // сам текст находки — совет про бэкап, и ему место в разделе «Бэкапы».
    for (const a of s.backup_audit ?? []) {
      if (!a.kind.startsWith('db') || seen.has(a.subject)) continue
      seen.add(a.subject)
      // Строим «где» из ПОЛЕЙ находки. Разбор её текста (detail) сюда затаскивал
      // бэкапные формулировки: у находки «дамп уже настроен» текст начинается с
      // «дамп уже настроен: CronJob …», и он оказывался в поле «где».
      const pods = a.pods ?? []
      items.push({
        kind: a.subject,
        where: a.container
          ? t('контейнер: {c}', { c: a.container })
          : pods.length
            ? t('под kubernetes: {p}', { p: pods.join(', ') })
            : t('процесс на хосте'),
        inKube: pods.length > 0,
      })
    }
    // веб-серверы/прокси (nginx/envoy/…): их не бэкапят, поэтому идут отдельным источником
    for (const w of s.last_report?.web_services ?? []) {
      if (seen.has(w.kind)) continue
      seen.add(w.kind)
      items.push({ kind: w.kind, where: w.source || '', sites: w.sites, web: true })
    }
    const stats = s.last_report?.db_stats ?? []
    if (stats.length)
      for (const it of items) {
        if (it.web) continue
        const m = matchDbStats(it.kind, stats)
        if (m.length) it.db = m
      }
    items.sort((a, b) => a.kind.localeCompare(b.kind))
    if (items.length) out.push({ server: s, items })
  }
  return out.sort((a, b) => a.server.name.localeCompare(b.server.name))
}

// суммарная «горячесть» ноды по очередям RabbitMQ (для цвета строки)
function hotQueues(items: SvcItem[]): number {
  return items.reduce((n, it) => n + (it.svc?.queues ?? []).filter((q) => qDepth(q) >= QUEUE_HOT).length, 0)
}

// Очереди RabbitMQ одной ноды (вложенная модалка из карточки сервисов)
function QueuesModal({ server, svc, onClose, onChanged }: {
  server: Server
  svc: ServiceInfo
  onClose: () => void
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(false)
  // порог общий по ноде и переопределения по конкретным очередям — оба живут на сервере
  const depth = server.queue_alert_depth ?? 0
  const over = server.queue_alert_over ?? {}
  const [draft, setDraft] = useState(String(depth || ''))
  const save = async (body: Parameters<typeof updateServer>[1]) => {
    setBusy(true)
    try {
      await updateServer(server.id, body)
      onChanged()
    } finally {
      setBusy(false)
    }
  }
  // порог конкретной очереди: своё значение важнее общего; 0 = не алертить
  const thrOf = (key: string) => (key in over ? over[key] : depth)
  // Черновики ввода держим отдельно от сохранённых значений: поле правится посимвольно,
  // а запись на сервер идёт по уходу фокуса/Enter — иначе каждый набранный знак улетал бы
  // отдельным PATCH'ем, и «100000» сохранилось бы как «1», потом «10» и так далее.
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  // В поле показываем ДЕЙСТВУЮЩИЙ порог: свой, если задан, иначе унаследованный от
  // ноды. Пустое поле значит ровно одно — «не алертить», а не «наследовать».
  const draftOf = (key: string) => drafts[key] ?? (thrOf(key) > 0 ? String(thrOf(key)) : '')
  const commitQueue = (key: string) => {
    const raw = (drafts[key] ?? '').trim()
    setDrafts((d) => {
      const c = { ...d }
      delete c[key]
      return c
    })
    // пусто и 0 — одно и то же: выключить алерты по этой очереди
    const val = raw === '' ? 0 : Math.max(0, Math.round(Number(raw) || 0))
    const next = { ...over }
    if (val === thrOf(key)) return // ничего не изменилось — не дёргаем сервер
    next[key] = val
    save({ queue_alert_over: next })
  }
  const ql = q.trim().toLowerCase()
  const queues = (svc.queues ?? []).filter((x) => !ql || x.name.toLowerCase().includes(ql))
  const withMsgs = (svc.queues ?? []).filter((x) => qDepth(x) > 0).length
  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal docker-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3><span className="sdot sdot-up" /> <CountryFlag code={server.country} /> {server.name} · <EngineIcon name="RabbitMQ" /> RabbitMQ</h3>
          <button className="ghost" onClick={onClose}>{t('Закрыть')}</button>
        </div>
        <div className="docker-host-head">
          <span className="muted small">
            {t('очередей: {n} · с сообщениями: {m}', { n: svc.total ?? queues.length, m: withMsgs })}
          </span>
        </div>
        {/* Порог общий для всех очередей ноды; у отдельной очереди его можно
            переопределить или выключить — кнопкой в её строке. */}
        <div className="queue-alert-cfg">
          <span className="muted small">{t('Алерт, когда в очереди накопилось')}</span>
          <input
            className="mono queue-thr-input"
            type="number"
            min={0}
            placeholder="0"
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => {
              const v = Math.max(0, Math.round(Number(draft) || 0))
              if (v !== depth) save({ queue_alert_depth: v })
            }}
          />
          <span className="muted small">
            {t('сообщений и больше — сразу во всех очередях ниже. У любой очереди значение можно поправить или стереть.')}
          </span>
        </div>
        <div className="modal-toolbar">
          <input className="checks-search modal-search" placeholder={t('Фильтр: имя очереди…')}
            value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div className="loc-results docker-clist docker-clist-scroll">
          {queues.length === 0 ? (
            <div className="muted small">{t('Ничего не найдено.')}</div>
          ) : (
            queues.map((x) => {
              const d = qDepth(x)
              return (
                <div key={(x.vhost || '') + '/' + x.name}
                  className={`loc-res docker-row ${d >= QUEUE_HOT ? 't-down' : d > 0 ? 't-degraded' : 't-up'}`}>
                  <div className="docker-c-main">
                    <div className="docker-c-name mono">{x.name}</div>
                    {x.vhost && x.vhost !== '/' && (
                      <div className="docker-c-img mono muted small">vhost: {x.vhost}</div>
                    )}
                  </div>
                  <div className="queue-nums mono">
                    <span title={t('в очереди, ждут доставки')}>{x.ready}</span>
                    {(x.unacked ?? 0) > 0 && (
                      <span className="muted" title={t('взяты консьюмером, ещё не подтверждены')}>+{x.unacked}</span>
                    )}
                  </div>
                  {(() => {
                    const key = queueKey(svc.source || '', x)
                    const eff = thrOf(key)
                    return (
                      <input
                        className={`mono queue-thr-cell${eff > 0 ? ' queue-alert-on' : ''}`}
                        type="number"
                        min={0}
                        disabled={busy}
                        placeholder={t('выкл')}
                        value={draftOf(key)}
                        title={t('Порог этой очереди: с этого числа сообщений алертим. Пусто или 0 — не алертить.')}
                        onChange={(e) => setDrafts((d) => ({ ...d, [key]: e.target.value }))}
                        onBlur={() => commitQueue(key)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') e.currentTarget.blur()
                        }}
                      />
                    )
                  })()}
                </div>
              )
            })
          )}
        </div>
        {(svc.total ?? 0) > (svc.queues?.length ?? 0) && (
          <div className="muted small">
            {t('Показаны {n} самых глубоких из {m} — остальные пустые.', {
              n: svc.queues?.length ?? 0, m: svc.total ?? 0,
            })}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

// Карточка ноды: все её сервисы с деталями
function ServerServicesModal({ srv, onClose, onChanged, autoQueues, sites }: {
  srv: SrvServices
  onClose: () => void
  onChanged: () => void
  autoQueues?: boolean // пришли из алерта (?queues=1) — сразу раскрыть очереди
  sites: SitesCtx // «какие домены уже мониторятся» + как завести новый
}) {
  const { t } = useI18n()
  const s = srv.server
  // Из алерта по очереди приходим СРАЗУ к очередям: иначе человек попадает в общий
  // список сервисов ноды и должен искать нужный RabbitMQ руками.
  const [queues, setQueues] = useState<ServiceInfo | null>(
    () => (autoQueues ? srv.items.find((x) => x.svc?.kind === 'rabbitmq')?.svc ?? null : null),
  )
  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal docker-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />{' '}
            <OsIcon os={s.os} /> <CountryFlag code={s.country} /> {s.name}
          </h3>
          <button className="ghost" onClick={onClose}>{t('Закрыть')}</button>
        </div>
        <div className="docker-host-head">
          {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          <span className="muted small">{t('сервисов: {n}', { n: srv.items.length })}</span>
        </div>
        <div className="svc-cards">
          {srv.items.map((it, i) => {
            const total = it.svc?.total ?? 0
            const msgs = (it.svc?.queues ?? []).reduce((a, q) => a + qDepth(q), 0)
            const hot = hotQueues([it]) > 0
            return (
              <div key={it.kind + '-' + i} className={`svc-card ${hot ? 'svc-card-hot' : ''}`}>
                <div className="svc-card-head">
                  <span className="svc-ico"><EngineIcon name={it.kind} /></span>
                  <span className="svc-name">{it.kind}</span>
                  {it.where && <span className="type-chip mono">{it.where}</span>}
                  {it.svc?.kind === 'rabbitmq' && (
                    <button className="svc-act" onClick={() => setQueues(it.svc!)}>
                      {t('очереди')}
                    </button>
                  )}
                </div>
                {it.svc?.kind === 'rabbitmq' && (
                  <div className={`svc-card-detail small ${hot ? 'form-error' : 'muted'}`}>
                    {t('очередей: {n}', { n: total })}
                    {msgs > 0 && ` · ${t('сообщений: {m}', { m: msgs })}`}
                    {hot && ` · ${t('есть переполненные (≥{n})', { n: QUEUE_HOT })}`}
                  </div>
                )}
                {it.sites && it.sites.length > 0 && (
                  <SitesList
                    sites={it.sites}
                    t={t}
                    hosts={sites.hosts}
                    skip={sites.skip}
                    onAdopt={sites.canAdd ? sites.openWizard : undefined}
                    onOpenCheck={sites.onOpenCheck}
                  />
                )}
                {it.db && <DbInventory db={it.db} t={t} />}
                {!it.db && !it.web && !it.svc && noDbHint(it.kind, !!it.inKube, s, t) && (
                  <div className="svc-card-detail muted small">
                    {noDbHint(it.kind, !!it.inKube, s, t)}
                  </div>
                )}
                {it.web && !it.sites?.length && (
                  <div className="svc-card-detail muted small">{noSitesHint(it.kind, s, t)}</div>
                )}
              </div>
            )
          })}
        </div>
      </div>
      {queues && (
        <QueuesModal server={s} svc={queues} onClose={() => setQueues(null)} onChanged={onChanged} />
      )}
    </div>,
    document.body,
  )
}

// строка ноды: имя + иконки её сервисов (сразу видно, что где крутится)
function HostRow({ srv, onOpen }: { srv: SrvServices; onOpen: () => void }) {
  const { t } = useI18n()
  const s = srv.server
  const hot = hotQueues(srv.items) > 0
  return (
    <button className="check-row srv-row docker-host-row" onClick={onOpen}>
      <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
      <div className="check-main">
        <div className="check-name">
          <OsIcon os={s.os} />
          <CountryFlag code={s.country} />
          {s.name}
          {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
        </div>
        <div className="svc-row-icons">
          {srv.items.map((it, i) => (
            <span key={it.kind + '-' + i} className="svc-chip"
              title={it.where ? `${it.kind} · ${it.where}` : it.kind}>
              <span className="svc-ico"><EngineIcon name={it.kind} /></span> {it.kind}
            </span>
          ))}
        </div>
      </div>
      <div className={`docker-host-count mono ${hot ? 't-degraded' : ''}`}>
        {srv.items.length}<span className="muted"> {t('серв.')}</span>
      </div>
    </button>
  )
}

// то, что нужно списку доменов: чем помечать строки и как открыть мастер
type SitesCtx = {
  hosts: Record<string, number> | null
  skip: Set<string>
  canAdd: boolean
  openWizard: (domains: string[]) => void
  onOpenCheck?: (id: number) => void
}

export function ServicesPage({ onUnauthorized, openServerId, openQueues, onConsumed, onOpenCheck }: {
  onUnauthorized: () => void
  openServerId?: number | null
  openQueues?: boolean
  onConsumed?: () => void
  onOpenCheck?: (id: number) => void // перейти к монитору в разделе «Сайты»
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [servers, setServers] = useState<Server[] | null>(null)
  // Домены под мониторингом. null = учётке недоступен раздел «Сайты» (403) — тогда
  // ни галочек, ни кнопок: показывать «добавь монитор» тому, кто его не увидит, незачем.
  const [hosts, setHosts] = useState<Record<string, number> | null>(null)
  const [siteGroups, setSiteGroups] = useState<string[]>([])
  const [ignored, setIgnored] = useState<string[]>([])
  // домены, с которыми открыт мастер «поставить на мониторинг» (null = закрыт)
  const [wizard, setWizard] = useState<string[] | null>(null)
  const [openId, setOpenId] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [searchRo, setSearchRo] = useState(true) // защита от автозаполнения, см. «Бэкапы»
  const [groupBy, setGroupBy] = useState<'none' | 'group'>(
    () => (localStorage.getItem('kervax_services_groupby') as 'none' | 'group') || 'group',
  )
  const setGroup = (g: 'none' | 'group') => {
    setGroupBy(g)
    localStorage.setItem('kervax_services_groupby', g)
  }

  const load = useCallback(() => {
    listServers()
      .then(setServers)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
      })
  }, [onUnauthorized])

  const loadHosts = useCallback(() => {
    knownHosts()
      .then((r) => {
        setHosts(r.hosts)
        setSiteGroups(r.groups)
        setIgnored(r.ignored)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
        else setHosts(null) // 403: раздел «Сайты» закрыт — фичу просто не показываем
      })
  }, [onUnauthorized])

  const sitesCtx: SitesCtx = {
    hosts,
    skip: new Set(ignored),
    canAdd: hosts != null && !isViewer,
    openWizard: setWizard,
    onOpenCheck,
  }

  // Диплинк из алерта: ?services=<id> — раскрыть карточку этой ноды. Ждём, пока
  // список загрузится, иначе открывать нечего; id «съедаем», чтобы повторное
  // закрытие карточки не открывало её снова.
  useEffect(() => {
    if (openServerId == null || servers == null) return
    setOpenId(openServerId)
    onConsumed?.()
  }, [openServerId, servers, onConsumed])

  useEffect(() => {
    load()
    loadHosts()
    const id = window.setInterval(load, 20000)
    return () => window.clearInterval(id)
  }, [load, loadHosts])

  if (!servers) return <p className="muted">{t('загрузка…')}</p>
  const all = buildByServer(servers, t)
  // Ищем и по ноде, и по СЕРВИСУ: на этой странице чаще спрашивают «где у меня redis»,
  // а не «что на ноде X» — поиск только по именам нод отвечал бы на другой вопрос.
  const q = query.trim().toLowerCase()
  const rows = q
    ? all.filter(({ server, items }) =>
        [server.name, server.group_name, ...items.map((i) => i.kind)]
          .join(' ')
          .toLowerCase()
          .includes(q),
      )
    : all
  const kinds = new Set(rows.flatMap((r) => r.items.map((i) => i.kind)))
  const open = all.find((r) => r.server.id === openId)
  const groups: { key: string; label: string; items: SrvServices[] }[] =
    groupBy === 'none'
      ? [{ key: 'all', label: '', items: rows }]
      : (() => {
          const map = new Map<string, SrvServices[]>()
          for (const r of rows) {
            const k = r.server.group_name?.trim() || ''
            map.set(k, [...(map.get(k) ?? []), r])
          }
          return [...map.entries()]
            .sort((a, b) => (a[0] || '￿').localeCompare(b[0] || '￿'))
            .map(([k, items]) => ({ key: k || '__none__', label: k, items }))
        })()

  return (
    <div>
      <div className="page-head">
        <h2>{t('Сервисы')}</h2>
        <span className="muted small">
          {t('{s} нод · {k} типов сервисов', { s: rows.length, k: kinds.size })}
        </span>
      </div>
      {all.length > 1 && (
        <div className="checks-head">
          <div className="checks-head-actions">
            <div className="win-switch">
              {(['group', 'none'] as const).map((g) => (
                <button
                  key={g}
                  className={`win-btn${groupBy === g ? ' win-btn-active' : ''}`}
                  onClick={() => setGroup(g)}
                >
                  {g === 'group' ? t('По группе') : t('Без групп')}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
      {all.length > 1 && (
        <div className="checks-search-row">
          <input
            className="checks-search"
            type="search"
            name="services-search"
            autoComplete="off"
            readOnly={searchRo}
            onFocus={() => setSearchRo(false)}
            placeholder={t('Поиск: нода, группа, сервис…')}
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
      {all.length === 0 ? (
        <div className="card muted">
          {t('Сервисы не обнаружены. Панель находит их по контейнерам, подам и процессам — обновите агенты.')}
        </div>
      ) : rows.length === 0 ? (
        <div className="card muted">
          {t('По запросу «{q}» ничего нет.', { q: query.trim() })}
          <button className="ghost search-clear" onClick={() => setQuery('')}>{t('Сбросить')}</button>
        </div>
      ) : (
        groups.map((g) => (
          <div className="mon-group" key={g.key}>
            {groupBy === 'group' && (
              <div className="mon-group-head docker-group-head">
                <span className="mon-group-name">{g.label || t('Без группы')}</span>
                <span className="mon-group-n">{g.items.length}</span>
              </div>
            )}
            <div className="check-list">
              {g.items.map((r) => (
                <HostRow key={r.server.id} srv={r} onOpen={() => setOpenId(r.server.id)} />
              ))}
            </div>
          </div>
        ))
      )}
      {open && (
        <ServerServicesModal
          srv={open}
          onClose={() => setOpenId(null)}
          onChanged={load}
          autoQueues={!!openQueues && open.server.id === openServerId}
          sites={sitesCtx}
        />
      )}
      {wizard && hosts && (
        <AdoptSitesModal
          title={t('Поставить сайты на мониторинг')}
          items={wizard.map((d) => ({ domain: d }))}
          hosts={hosts}
          groups={siteGroups}
          ignored={ignored}
          onClose={() => setWizard(null)}
          onDone={(h) => setHosts(h)}
          onIgnored={(domains, ignore) =>
            setIgnored((prev) => {
              const next = new Set(prev)
              for (const d of domains) {
                if (ignore) next.add(d)
                else next.delete(d)
              }
              return [...next]
            })
          }
          onOpenCheck={onOpenCheck}
        />
      )}
    </div>
  )
}
