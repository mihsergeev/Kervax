import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  knownHosts,
  listServers,
  updateServer,
  type DBStat,
  type DbHealth,
  type DbLoad,
  type Server,
  type ServiceInfo,
} from './api'
import { currentLang, useI18n } from './i18n'
import { useAuth } from './auth'
import { useUrlCard, useUrlParam } from './deeplink'
import { shortList, srvIssues, srvMuted, webRate } from './serverUtils'
import { fmtRel } from './ServersPage'
import { CardNav, FreshChip, markMuted, OvAreas, OvRows, secInfoOf, type OvRow, type SecDef, type SecInfo, type SecItem } from './cardLayout'
import { AdoptSitesModal, adoptable } from './AdoptSitesModal'
import { EngineIcon } from './engineIcon'
import { OsIcon } from './osIcon'
import { CountryFlag } from './CountryFlag'
import { byteUnits } from './units'
import { NetTile } from './NetLinks'
import { EXPIRY_SHOW_DAYS, EXPIRY_WARN_DAYS, expiryTone } from './checkUtils'

// Список сайтов веб-сервиса: превью + раскрытие в скроллящийся список с фильтром (у
// ingress-нод бывают сотни доменов — «10 + ещё 390» неюзабельно). Рядом с каждым —
// признак «уже под мониторингом»; завести новые можно кнопкой, она открывает общий
// мастер (AdoptSitesModal) — отмечать сотни доменов плюсиками по одному невозможно.
function SitesList({ sites, t, hosts, skip, onAdopt, onOpenCheck, full = false }: {
  full?: boolean // раздел "Сайты" карточки ноды: список сразу целиком, с фильтром
  sites: string[]
  t: (s: string, v?: Record<string, string | number>) => string
  hosts: Record<string, number> | null // null = список мониторов недоступен учётке
  skip: Set<string> // помеченные «мониторить не нужно» — в счётчик не идут
  onAdopt?: (domains: string[]) => void
  onOpenCheck?: (id: number) => void
}) {
  const [opened, setOpen] = useState(false)
  const open = full || opened
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
        {sites.length > PREVIEW && !full && (
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
// точка строки очереди по тону
const DOT: Record<string, string> = { 't-up': ' lk-dot-ok', 't-down': ' lk-dot-bad', 't-degraded': ' lk-dot-warn', 't-soon': '' }
// подпись плитки из частей: пустые выкидываем
const subOf = (...parts: (string | false | 0 | undefined)[]) => parts.filter(Boolean).join(' · ')

// «6 042» вместо «6042»: тысячи в потоке запросов читаются взглядом, а не пересчётом
function rpmText(v: number): string {
  return Math.round(v).toLocaleString('ru-RU').replace(/\u00a0/g, ' ')
}

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
// Отставание реплики коротко: 40 с, 12 мин, 3 ч
function lagText(sec: number, t: (s: string, v?: Record<string, string | number>) => string): string {
  if (sec < 60) return t('{n} с', { n: sec })
  if (sec < 3600) return t('{n} мин', { n: Math.round(sec / 60) })
  return t('{n} ч', { n: Math.round(sec / 3600) })
}

// Что грузит PostgreSQL за последний час (dbstat-setup 0.8): временные файлы, чтение, WAL, чекпоинты
// и самые долгие запросы. Без алертов - это разбор, когда пришли выяснять, почему база тормозит или
// изнашиваются диски. Подсказки - только то, что из этих цифр прямо следует
const LOAD_QUIET_MS = 60_000 // запросы заняли меньше минуты за час - топ ни о чем не говорит
const LOAD_TEMP_HINT = 10 * 1024 ** 3 // временных файлов больше 10 ГБ в час
const LOAD_WBF_HINT = 100

function fmtCount(n: number): string {
  return Math.round(n).toLocaleString(currentLang() === 'ru' ? 'ru-RU' : 'en-US')
}

function DbLoadBlock({ ld, name, t }: { ld: DbLoad; name: string; t: (s: string, v?: Record<string, string | number>) => string }) {
  const facts: string[] = []
  const hints: string[] = []
  if (ld.win > 0) {
    if (ld.temp_b) facts.push(t('временные файлы {s} в час', { s: fmtBytes(ld.temp_b) }))
    if (ld.rd) facts.push(t('чтение мимо shared_buffers {s} в час', { s: fmtBytes(ld.rd) }))
    if (ld.wal_b) {
      const fpi = ld.wal_rec && ld.fpi != null ? Math.round((ld.fpi * 100) / ld.wal_rec) : null
      facts.push(t('WAL {s} в час', { s: fmtBytes(ld.wal_b) }) + (fpi != null ? `, ${t('полные копии страниц: {p}% записей', { p: fpi })}` : ''))
    }
    if (ld.ck_t != null && ld.ck_r != null) facts.push(t('чекпоинты: {a} по времени, {b} по объему WAL', { a: ld.ck_t, b: ld.ck_r }))
    if (ld.wbf) facts.push(t('буфер WAL переполнялся {n} раз за час', { n: fmtCount(ld.wbf) }))
    if (ld.xact != null) facts.push(t('{n} транзакций в секунду', { n: (ld.xact / 3600).toFixed(ld.xact < 36000 ? 1 : 0) }))
    if ((ld.temp_b ?? 0) >= LOAD_TEMP_HINT)
      hints.push(t('Запросы пишут временные файлы {s} в час: сортировки и хеши не помещаются в work_mem, это запись на диск и его износ. Какие именно - видно по временным файлам у запросов ниже', { s: fmtBytes(ld.temp_b ?? 0) }))
    // от трех в час: единичный по запросу дают CHECKPOINT, pg_basebackup, CREATE DATABASE
    if ((ld.ck_r ?? 0) >= 3 && (ld.ck_r ?? 0) >= (ld.ck_t ?? 0))
      hints.push(t('Чекпоинты чаще идут по объему WAL, чем по времени: max_wal_size мал для такой записи, а после каждого чекпоинта страницы снова пишутся в WAL целиком'))
    if ((ld.wbf ?? 0) >= LOAD_WBF_HINT)
      hints.push(t('Буфер WAL переполнялся {n} раз за час: запросы ждали записи WAL на диск, стоит увеличить wal_buffers (нужен перезапуск)', { n: fmtCount(ld.wbf ?? 0) }))
  }
  if (ld.pss === 'off')
    hints.push(t('Самые долгие запросы покажет pg_stat_statements: добавьте его в shared_preload_libraries (нужен перезапуск PostgreSQL) и выполните CREATE EXTENSION pg_stat_statements'))
  if (ld.pss === 'noext')
    hints.push(t('pg_stat_statements загружен, но расширение не создано ни в одной базе: выполните CREATE EXTENSION pg_stat_statements, перезапуск не нужен. Панель увидит его в течение часа'))
  if (ld.qtext > 1024 ** 3)
    hints.push(t('Тексты запросов pg_stat_statements занимают {s}: так бывает, когда один запрос приходит с разным числом значений (многострочный INSERT, длинный IN), и каждый вариант хранится отдельно со своим текстом. Они вытесняют из статистики другие запросы, а каждое чтение текстов поднимает файл в память целиком, поэтому новые тексты панель читает раз в 6 часов', { s: fmtBytes(ld.qtext) }))
  if (ld.cut) hints.push(t('Запросы не показаны: на сервере много инстансов, и отчет не вместил бы все'))
  const [open, setOpen] = useState<number | null>(null) // текст запроса целиком по нажатию (на телефоне нет подсказки)
  const quiet = (ld.ms_all ?? 0) < LOAD_QUIET_MS
  const multiDb = new Set(ld.top.map((q) => q.db).filter(Boolean)).size > 1
  return (
    <div className="svc-db-load">
      <div className="svc-db-load-head">
        <span className="svc-db-load-title">{t('Что грузит базу')}</span>
        {name && <span className="mono muted small">{name}</span>}
        {ld.win > 0 && (
          <span className="muted small">
            {ld.win >= 3000 ? t('за последний час') : t('за {n} мин, в пересчете на час', { n: Math.max(1, Math.round(ld.win / 60)) })}
          </span>
        )}
      </div>
      {ld.win <= 0 && ld.pss !== 'off' && ld.pss !== 'noext' && (
        <div className="muted small">{t('Собираю данные: первая картина через 15 минут, за полный час - через час')}</div>
      )}
      {facts.length > 0 && <div className="svc-db-load-facts small">{facts.map((f) => <span key={f}>{f}</span>)}</div>}
      {ld.win > 0 && ld.pss === 'on' && !ld.cut && quiet && (
        <div className="muted small">{t('Запросы заняли {s} с за час - база почти не нагружена', { s: Math.round((ld.ms_all ?? 0) / 1000) })}</div>
      )}
      {ld.win > 0 && !quiet && ld.top.length > 0 && (
        <div className="svc-db-load-top">
          {ld.top.map((q, i) => (
            <div key={i} className="svc-db-load-q">
              <div className={q.q ? `mono svc-db-load-text${open === i ? ' open' : ''}` : 'muted small'} title={q.q || undefined}
                onClick={() => q.q && setOpen(open === i ? null : i)}>
                {q.q || (q.db ? t('текст скрыт: в нем пароль или секрет') : t('текст еще не прочитан'))}
              </div>
              <div className="muted small">
                {[
                  t('{p}% времени запросов', { p: q.share }),
                  t('{n} вызовов', { n: fmtCount(q.calls) }),
                  q.avg_ms >= 1000
                    ? t('{s} с в среднем', { s: (q.avg_ms / 1000).toFixed(q.avg_ms >= 10000 ? 0 : 1) })
                    : t('{ms} мс в среднем', { ms: q.avg_ms >= 10 ? Math.round(q.avg_ms) : q.avg_ms }),
                  q.temp > 0 ? t('временные файлы {s}', { s: fmtBytes(q.temp) }) : '',
                  multiDb && q.db ? t('база {db}', { db: q.db }) : '',
                ].filter(Boolean).join(' · ')}
              </div>
            </div>
          ))}
        </div>
      )}
      {hints.map((h) => <div key={h} className="net-note">{h}</div>)}
    </div>
  )
}

function DbInventory({ db, t, health = [] }: {
  db: DBStat[]
  t: (s: string, v?: Record<string, string | number>) => string
  health?: DbHealth[] // здоровье этих инстансов (dbstat-setup 0.5): реплика, транзакции, VACUUM
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
        {health.map((h) => (h.role === 'replica' || h.replicas > 0) && (
          <span key={'r' + h.container} title={h.container || undefined}>
            {h.role === 'replica'
              ? h.lag > 0 ? t('реплика, отстает на {l}', { l: lagText(h.lag, t) }) : t('реплика, догнала')
              : t('основная, реплик: {n}', { n: h.replicas })}
          </span>
        ))}
        {rows.length > 5 && (
          <button className="svc-act" onClick={() => setOpen(!open)}>
            {open ? t('свернуть') : t('показать все')}
          </button>
        )}
      </div>
      {health.flatMap((h) => h.problems).map((p) => (
        <div key={p.text} className={p.level >= 2 ? 'net-prob' : 'net-note'}>{p.text}</div>
      ))}
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
      {health.filter((h) => h.load).map((h) => (
        <DbLoadBlock key={'l' + h.container} ld={h.load as DbLoad} name={health.length > 1 ? h.container : ''} t={t} />
      ))}
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
        // у базы в поде container — это её контроллер (k8s.<ns>.<вид>.<имя>), а не контейнер
        where: a.container && !a.container.startsWith('k8s.')
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

// сколько доменов можно поставить на мониторинг: не под монитором и не помечены "не нужно"
function missingSites(sites: string[], ctx: SitesCtx): number {
  const hosts = ctx.hosts
  if (!hosts) return 0
  return sites.filter((x) => adoptable(x) && hosts[x.toLowerCase()] == null && !ctx.skip.has(x.toLowerCase())).length
}

// суммарная «горячесть» ноды по очередям RabbitMQ (для цвета строки)
function hotQueues(items: SvcItem[]): number {
  return items.reduce((n, it) => n + (it.svc?.queues ?? []).filter((q) => qDepth(q) >= QUEUE_HOT).length, 0)
}

// Очереди RabbitMQ одного инстанса - прямо в разделе "Очереди" карточки ноды. Раньше это была
// вторая модалка поверх карточки
function QueueList({ server, svc, busy, save }: {
  server: Server
  svc: ServiceInfo
  busy: boolean
  save: (body: Parameters<typeof updateServer>[1]) => void
}) {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  // порог общий по ноде и переопределения по конкретным очередям — оба живут на сервере
  const depth = server.queue_alert_depth ?? 0
  const over = server.queue_alert_over ?? {}
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
  const all = svc.queues ?? []
  const queues = all.filter((x) => !ql || x.name.toLowerCase().includes(ql))
  const withMsgs = all.filter((x) => qDepth(x) > 0).length
  const hotN = all.filter((x) => qDepth(x) >= QUEUE_HOT).length
  const msgsAll = all.reduce((a, x) => a + qDepth(x), 0)
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="hs-svc-title">
          <span className="svc-ico"><EngineIcon name="RabbitMQ" /></span>
          RabbitMQ
          <span className="net-n">{svc.total ?? all.length}</span>
        </div>
      </div>
      {svc.source && <div className="net-sub mono">{svc.source}</div>}
      <div className="small muted">
        {subOf(
          t('с сообщениями: {n}', { n: withMsgs }),
          t('сообщений: {m}', { m: rpmText(msgsAll) }),
          hotN > 0 && t('переполнены: {n} (от {m} сообщений)', { n: hotN, m: QUEUE_HOT }),
        )}
      </div>
      {all.length > 8 && (
        <div className="modal-toolbar">
          <input className="checks-search modal-search" placeholder={t('Фильтр: имя очереди…')}
            value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      )}
      <div className="loc-results docker-clist">
        {queues.length === 0 ? (
          <div className="muted small">{t('Ничего не найдено.')}</div>
        ) : (
          queues.map((x) => {
            const d = qDepth(x)
            const tone = d >= QUEUE_HOT ? 't-down' : d > 0 ? 't-degraded' : 't-up'
            const key = queueKey(svc.source || '', x)
            const eff = thrOf(key)
            return (
              <div key={(x.vhost || '') + '/' + x.name} className={`loc-res docker-row ${tone}`}>
                <span className={`lk-dot${DOT[tone]}`} />
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
                <input
                  className={`mono queue-thr-cell${eff > 0 ? ' queue-alert-on' : ''}`}
                  type="number"
                  min={0}
                  disabled={busy}
                  placeholder={t('выкл')}
                  value={draftOf(key)}
                  title={t('Порог этой очереди: с этого числа сообщений алертим. Пусто или 0 — не алертить.')}
                  onChange={(e) => setDrafts((dd) => ({ ...dd, [key]: e.target.value }))}
                  onBlur={() => commitQueue(key)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') e.currentTarget.blur()
                  }}
                />
              </div>
            )
          })
        )}
      </div>
      {(svc.total ?? 0) > all.length && (
        <div className="net-foot">
          {t('Показаны {n} самых глубоких из {m} — остальные пустые.', { n: all.length, m: svc.total ?? 0 })}
        </div>
      )}
    </div>
  )
}

// Карточка сервисов ноды: меню разделов слева, на экране один раздел, как у сервера (cardLayout).
// Раньше шла карточка на каждый сервис вперемешку (nginx с сайтами и сертификатами, базы,
// RabbitMQ), а очереди открывались второй модалкой. Теперь веб, сайты, сертификаты, базы и
// очереди - своими разделами, на сводке проблемы из "Что сломано", плитки и карта.
const SVC_SECTIONS: SecDef[] = [
  { id: 'overview', label: 'Сводка', group: '' },
  { id: 'web', label: 'Веб-серверы', group: 'Веб' },
  { id: 'sites', label: 'Сайты', group: 'Веб' },
  { id: 'certs', label: 'Сертификаты', group: 'Веб' },
  { id: 'db', label: 'Базы и хранилища', group: 'Данные' },
  { id: 'queues', label: 'Очереди', group: 'Данные' },
]
const SVC_SEC_IDS = new Set(SVC_SECTIONS.map((x) => x.id))
// раздел карточки по виду проблемы, если бэкенд его не назвал (старый бэкенд в кэше вкладки)
const SVC_SEC_OF_KIND: Record<string, string> = {
  queue: 'queues', web_cert: 'certs', web_config: 'web', db_health: 'db', db_conn: 'db',
}

function ServerServicesModal({ srv, onClose, onChanged, initialSec, sites }: {
  srv: SrvServices
  onClose: () => void
  onChanged: () => void
  initialSec?: string | null // из ссылки алерта: ?services=id&sec=queues (или старое &queues=1)
  sites: SitesCtx // «какие домены уже мониторятся» + как завести новый
}) {
  const { t } = useI18n()
  const s = srv.server
  const [active, setActive] = useState<string>(() => (SVC_SEC_IDS.has(initialSec ?? '') ? initialSec! : 'overview'))
  const layoutRef = useRef<HTMLDivElement>(null)
  const go = (id: string) => {
    setActive(id)
    const el = layoutRef.current
    if (el && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: 'start' })
  }
  const [busy, setBusy] = useState(false)
  const save = (body: Parameters<typeof updateServer>[1]) => {
    setBusy(true)
    updateServer(s.id, body)
      .then(onChanged)
      .catch(() => {})
      .finally(() => setBusy(false))
  }
  const depth = s.queue_alert_depth ?? 0
  const [draft, setDraft] = useState(String(depth || ''))
  const web = srv.items.filter((it) => it.web)
  const siteList = [...new Set(web.flatMap((it) => it.sites ?? []))]
  const missing = missingSites(siteList, sites)
  const rate = webRate(s.last_report)
  // СУБД сверху, под ними прочее, что держит данные (Grafana, Prometheus, etcd): их тоже находит аудит
  const isDb = (it: SvcItem) => Object.values(DB_ENGINE_LABEL).includes(it.kind)
  const dbItems = srv.items.filter((it) => !it.web && !it.svc).sort((a, b) => Number(isDb(b)) - Number(isDb(a)))
  const dbs = dbItems.flatMap((it) => it.db ?? [])
  const dbRows = dbs.flatMap((d) => (d.engine === 'redis' ? [] : d.dbs ?? []))
  const rabbit = srv.items.filter((it) => it.svc?.kind === 'rabbitmq')
  const qAll = rabbit.flatMap((it) => it.svc?.queues ?? [])
  const qHot = qAll.filter((x) => qDepth(x) >= QUEUE_HOT).length
  const certs = (s.web_certs ?? []).slice().sort((a, b) => a.days - b.days)
  const cfg = s.web_config ?? []

  const shown: Record<string, boolean> = {
    overview: true,
    web: web.length > 0,
    sites: siteList.length > 0,
    certs: certs.length > 0,
    db: dbItems.length > 0,
    queues: rabbit.length > 0,
  }
  const secs = SVC_SECTIONS.filter((x) => shown[x.id])
  const cur = secs.find((x) => x.id === active) ?? secs[0]
  useUrlParam('sec', cur.id === 'overview' ? null : cur.id)

  // Замечания по разделам - те же проблемы, что в "Что сломано": конфиг и сертификаты веб-сервера,
  // здоровье и коннекты баз, переполненные очереди
  const items: Record<string, SecItem[]> = {}
  for (const i of srvIssues(s, t).filter((x) => x.section === 'services')) {
    const sec = SVC_SEC_IDS.has(i.sec ?? '') ? i.sec! : (SVC_SEC_OF_KIND[i.kind ?? ''] ?? 'overview')
    ;(items[sec] ??= []).push({ level: i.tone === 't-down' ? 2 : 1, text: i.text, since: i.since })
  }
  const nextCert = certs[0]
  const ok: Record<string, string> = {
    web: subOf(
      web.map((it) => it.kind).join(', '),
      cfg.length > 0 && cfg.every((c) => c.ok) && t('конфиг проходит проверку'),
      rate ? t('{n} запросов/мин', { n: rpmText(rate.rpm) }) : false,
    ),
    sites: sites.hosts ? t('всего {n}, все на мониторинге', { n: siteList.length }) : t('всего {n}', { n: siteList.length }),
    certs: nextCert ? t('ближайший: {l} через {n} дн', { l: nextCert.label, n: nextCert.days }) : '',
    db: subOf(
      [...new Set(dbItems.map((it) => it.kind))].join(', '),
      dbRows.length > 0 && t('баз: {n}', { n: dbRows.length }),
      dbRows.length > 0 && fmtBytes(dbRows.reduce((a, r) => a + r.size, 0)),
    ),
    queues: t('очередей: {n}, сообщений: {m}', {
      n: rabbit.reduce((n, it) => n + (it.svc?.total ?? 0), 0),
      m: rpmText(qAll.reduce((a, x) => a + qDepth(x), 0)),
    }),
  }
  const info: Record<string, SecInfo> = {}
  for (const { id } of SVC_SECTIONS) info[id] = secInfoOf(items[id] ?? [], ok[id] ?? '', t)
  // Не проблема, но и не "все в порядке": сайты без монитора, глубокие очереди без порога
  // алерта. Желтым на карте, без числа в меню
  const soft = (id: string, line: string) => {
    if (!info[id].level) info[id] = { ...info[id], level: 1, line }
  }
  if (missing > 0) soft('sites', t('всего {n}, без монитора {m}', { n: siteList.length, m: missing }))
  if (qHot > 0) soft('queues', t('переполнены: {n} (от {m} сообщений)', { n: qHot, m: QUEUE_HOT }))
  markMuted(info, srvMuted(s, t).filter((i) => i.section === 'services').map((i) => ({
    sec: SVC_SEC_IDS.has(i.sec ?? '') ? i.sec! : (SVC_SEC_OF_KIND[i.kind ?? ''] ?? 'overview'), text: i.text,
  })), t)

  const rows: OvRow[] = SVC_SECTIONS.flatMap(({ id, label }) =>
    (items[id] ?? []).map((x) => ({
      tone: x.level >= 2 ? ('t-down' as const) : ('t-degraded' as const),
      text: x.text,
      since: x.since,
      act: id !== 'overview' && shown[id] ? { label: t(label), onClick: () => go(id) } : undefined,
    })),
  ).sort((a, b) => Number(b.tone === 't-down') - Number(a.tone === 't-down'))
  const tileTone = (sec: string, base?: 'bad' | 'warn' | 'ok') =>
    info[sec].n > 0 ? (info[sec].level >= 2 ? 'bad' : 'warn') : base
  const tileLabel = (sec: string, label: string) =>
    info[sec].fresh ? <>{label} <FreshChip since={info[sec].fresh!} /></> : label
  // версия движка, если ее знает инвентарь баз
  const verOf = (it: SvcItem) => it.db?.map((d) => d.version).find(Boolean)
  // строка под именем: по пункту на вид сервиса, сколько их и где - в подсказке
  const metaKinds = [...new Set(srv.items.map((it) => it.kind))].map((kind) => {
    const xs = srv.items.filter((it) => it.kind === kind)
    return { it: xs.find(verOf) ?? xs[0], n: xs.length, where: xs.map((x) => x.where).filter(Boolean).join('\n') }
  })

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
            <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
              ✕
            </button>
          </div>
        </div>
        <div className="srv-meta srv-meta-line">
          {metaKinds.map(({ it, n, where }) => (
            <span key={it.kind} className="srv-meta-item" title={where || undefined}>
              <span className="svc-ico"><EngineIcon name={it.kind} /></span>
              {it.kind}
              {n > 1 && ` (${n})`}
              {verOf(it) && <span className="srv-meta-k mono">{verOf(it)}</span>}
            </span>
          ))}
          <span className="srv-meta-item">
            <span className="srv-meta-k">{t('обновлено')}</span> {fmtRel(s.last_seen)}
          </span>
        </div>
        <div className="srv-layout" ref={layoutRef}>
          <CardNav secs={secs} active={cur.id} info={info} onGo={go} />
          <div className="srv-main">
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
              {cur.id === 'sites' && sites.canAdd && missing > 0 && (
                <button className="svc-act svc-act-primary srv-head-btn" onClick={() => sites.openWizard(siteList)}>
                  {t('на мониторинг: {n}', { n: missing })}
                </button>
              )}
            </div>

            {cur.id === 'overview' && (
              <div className="srv-sec">
                <OvRows rows={rows} />
                <div className="net-tiles srv-tiles">
                  {siteList.length > 0 && (
                    <button className="srv-tile-btn" onClick={() => go('sites')} title={t('к разделу')}>
                      <NetTile
                        label={tileLabel('sites', t('Сайтов'))}
                        value={String(siteList.length)}
                        sub={sites.hosts ? (missing ? t('без монитора: {n}', { n: missing }) : t('все на мониторинге')) : undefined}
                        tone={sites.hosts && !missing ? 'ok' : undefined}
                      />
                    </button>
                  )}
                  {rate && (
                    <button className="srv-tile-btn" onClick={() => go('web')} title={t('к разделу')}>
                      <NetTile
                        label={tileLabel('web', t('Запросов в минуту'))}
                        value={rpmText(rate.rpm)}
                        sub={(rate.e5 ?? 0) > 0
                          ? t('5xx: {n} ({p}%)', { n: rpmText(rate.e5 ?? 0), p: (((rate.e5 ?? 0) / Math.max(rate.rpm, 1)) * 100).toFixed(2) })
                          : t('без 5xx')}
                        tone={tileTone('web')}
                      />
                    </button>
                  )}
                  {/* Без инвентаря (базы в подах, нет dbstat-setup, база не ответила) число баз
                      неизвестно: плитка считает СУБД, а не ноль баз рядом с PostgreSQL на карте */}
                  {dbItems.length > 0 && (dbRows.length > 0 || dbItems.some(isDb)) && (
                    <button className="srv-tile-btn" onClick={() => go('db')} title={t('к разделу')}>
                      <NetTile
                        label={tileLabel('db', dbRows.length ? t('Базы') : t('СУБД'))}
                        value={String(dbRows.length || dbItems.filter(isDb).length)}
                        sub={(dbRows.length
                          ? subOf(
                              dbs.length > 1 && t('инстансов: {n}', { n: dbs.length }),
                              fmtBytes(dbRows.reduce((a, r) => a + r.size, 0)),
                            )
                          : t('состав баз не виден')) || undefined}
                        tone={tileTone('db', 'ok')}
                      />
                    </button>
                  )}
                  {rabbit.length > 0 && (
                    <button className="srv-tile-btn" onClick={() => go('queues')} title={t('к разделу')}>
                      <NetTile
                        label={tileLabel('queues', t('Очереди'))}
                        value={String(rabbit.reduce((n, it) => n + (it.svc?.total ?? 0), 0))}
                        sub={t('сообщений: {m}', { m: rpmText(qAll.reduce((a, x) => a + qDepth(x), 0)) })}
                        tone={tileTone('queues', qHot ? 'warn' : 'ok')}
                      />
                    </button>
                  )}
                </div>
                <OvAreas
                  cap={t('Сервисы по областям')}
                  items={SVC_SECTIONS.filter((x) => x.id !== 'overview' && shown[x.id]).map((x) => ({ id: x.id, label: x.label, info: info[x.id] }))}
                  onGo={go}
                />
              </div>
            )}

            {cur.id === 'web' && (
              <div className="net-cards">
                {web.map((it, i) => {
                  const cfgOf = cfg.filter((c) => it.kind.toLowerCase().startsWith(c.server))
                  const cfgBad = cfgOf.filter((c) => !c.ok)
                  return (
                    <div key={it.kind + i} className={`chart-card net-card net-card-wide${cfgBad.length ? ' net-card-bad' : ''}`}>
                      <div className="net-head">
                        <div className="hs-svc-title">
                          <span className="svc-ico"><EngineIcon name={it.kind} /></span>
                          {it.kind}
                        </div>
                      </div>
                      {it.where && <div className="net-sub mono">{it.where}</div>}
                      {cfgBad.map((c) => (
                        <div key={c.server + c.where} className="net-prob">
                          {c.where && c.where !== 'host'
                            ? t('конфиг в контейнере {c} не проходит проверку: {e}', { c: c.where, e: c.err })
                            : t('конфиг не проходит проверку: {e}', { e: c.err })}
                          {' '}{t('Следующий reload или перезапуск уронит сайты.')}
                        </div>
                      ))}
                      {cfgOf.length > 0 && !cfgBad.length && (
                        <div className="small t-up">{t('конфиг проходит проверку')}</div>
                      )}
                      {/* поток запросов: его считает helper по access-логам, поэтому он есть
                          только у хостового и контейнерного nginx */}
                      {it.kind === 'nginx' && rate && (
                        <div className="small muted">
                          {t('{n} запросов/мин', { n: rpmText(rate.rpm) })}
                          {(rate.logs ?? []).length > 1 && ` · ${t('самые нагруженные: {list}', {
                            list: rate.logs!.slice()
                              .sort((a, b) => b.rpm - a.rpm)
                              .slice(0, 3)
                              .map((l) => `${l.sites?.[0] || l.log.split('/').pop()} ${rpmText(l.rpm)}`)
                              .join(', '),
                          })}`}
                        </div>
                      )}
                      {it.sites && it.sites.length > 0 ? (
                        <div className="small muted">{t('сайтов: {n}', { n: it.sites.length })}</div>
                      ) : (
                        <div className="net-foot">{noSitesHint(it.kind, s, t)}</div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}

            {cur.id === 'sites' && (
              <div className="net-cards">
                {web.filter((it) => it.sites?.length).map((it, i) => (
                  <div key={it.kind + i} className="chart-card net-card net-card-wide">
                    <div className="net-head">
                      <div className="hs-svc-title">
                        <span className="svc-ico"><EngineIcon name={it.kind} /></span>
                        {it.kind}
                      </div>
                    </div>
                    {it.where && <div className="net-sub mono">{it.where}</div>}
                    <SitesList
                      full
                      sites={it.sites!}
                      t={t}
                      hosts={sites.hosts}
                      skip={sites.skip}
                      onOpenCheck={sites.onOpenCheck}
                    />
                  </div>
                ))}
              </div>
            )}

            {cur.id === 'certs' && (
              <div className="loc-results docker-clist">
                {certs.map((c) => {
                  const tone = expiryTone(c.days, EXPIRY_WARN_DAYS) || 't-up'
                  return (
                    <div key={c.where + c.file} className={`loc-res docker-row ${tone}`}>
                      <span className={`lk-dot${DOT[tone]}`} />
                      <div className="docker-c-main">
                        <div className="docker-c-name mono">{c.label}</div>
                        <div className="docker-c-img mono muted small">
                          {c.file}{c.where && c.where !== 'host' ? ` · ${c.where}` : ''}
                        </div>
                      </div>
                      <div className={`docker-c-status mono small ${tone}`}>
                        {c.days < 0 ? t('истек {n} дн назад', { n: -c.days }) : t('{n} дн.', { n: c.days })}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}

            {cur.id === 'db' && (
              <div className="net-cards">
                {dbItems.map((it, i) => (
                  <div key={it.kind + i} className="chart-card net-card net-card-wide">
                    <div className="net-head">
                      <div className="hs-svc-title">
                        <span className="svc-ico"><EngineIcon name={it.kind} /></span>
                        {it.kind}
                      </div>
                    </div>
                    {it.where && <div className="net-sub mono">{it.where}</div>}
                    {it.db ? (
                      <DbInventory db={it.db} t={t}
                        health={(s.db_health ?? []).filter((h) => DB_ENGINE_LABEL[h.engine] === it.kind)} />
                    ) : (
                      noDbHint(it.kind, !!it.inKube, s, t) && <div className="net-foot">{noDbHint(it.kind, !!it.inKube, s, t)}</div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {cur.id === 'queues' && (
              <div className="srv-sec">
                {/* Порог общий для всех очередей ноды; у отдельной очереди его можно
                    переопределить или выключить — полем в её строке. */}
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
                {rabbit.map((it, i) => (
                  <QueueList key={(it.svc?.source ?? '') + i} server={s} svc={it.svc!} busy={busy} save={save} />
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

// Строка ноды: иконки ее сервисов (сразу видно, что где крутится) и чипы бед - переполненные
// очереди, конфиг веб-сервера с ошибкой, проблемы баз. Справа число сервисов, цвет по худшему
function HostRow({ srv, showGroup, onOpen }: { srv: SrvServices; showGroup?: boolean; onOpen: () => void }) {
  const { t } = useI18n()
  const s = srv.server
  const hot = hotQueues(srv.items)
  const cfg = (s.web_config ?? []).filter((c) => !c.ok).length
  const dbp = (s.db_health ?? []).flatMap((h) => h.problems)
  const tone = cfg || dbp.some((p) => p.level >= 2) ? 't-down' : hot || dbp.length ? 't-degraded' : ''
  return (
    <button className="loc-res loc-res-btn hs-host" onClick={onOpen}>
      <span className={`lk-dot ${s.online ? 'lk-dot-ok' : 'lk-dot-bad'}`} title={s.online ? t('на связи') : t('не на связи')} />
      <div className="hs-host-main">
        <div className="hs-host-name">
          <OsIcon os={s.os} />
          <CountryFlag code={s.country} />
          {s.name}
          {showGroup && s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          {hot > 0 && (
            <span className="type-chip off" title={t('переполненные очереди RabbitMQ')}>📨 {hot}</span>
          )}
          {cfg > 0 && (
            <span className="type-chip off" title={t('конфиг веб-сервера не проходит проверку')}>🧾 {cfg}</span>
          )}
          {dbp.length > 0 && (
            <span className={`type-chip${dbp.some((p) => p.level >= 2) ? ' off' : ' t-degraded'}`}
              title={dbp.map((p) => p.text).join('\n')}>
              🛢 {dbp.length}
            </span>
          )}
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
      <div className={`hs-host-count mono ${tone}`}>
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

export function ServicesPage({ onUnauthorized, openServerId, openSec, onConsumed, onOpenCheck }: {
  onUnauthorized: () => void
  openServerId?: number | null
  openSec?: string | null // раздел карточки из ссылки: ?services=id&sec=queues
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
  // раздел из ссылки - до первого открытия карточки этой ноды
  const [linkSec, setLinkSec] = useState<{ id: number | null; sec: string | null }>({ id: null, sec: null })
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
    setLinkSec({ id: openServerId, sec: openSec ?? null })
    onConsumed?.()
  }, [openServerId, openSec, servers, onConsumed])
  useUrlCard('services', openId)

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
  // Сводка по парку: базы, веб-серверы и очереди - и что с ними не так. Сколько чего стоит -
  // по всем нодам, а что сломано - только по нодам на связи: у молчащих данные старые, и "все в
  // порядке" по ним было бы неправдой. Молчащие названы в первой плитке, проблемы - по именам
  const live = all.filter((r) => r.server.online)
  const names = (xs: string[]) => shortList([...new Set(xs)], t, 2)
  const offNames = all.filter((r) => !r.server.online).map((r) => r.server.name)
  const kindsAll = new Set(all.flatMap((r) => r.items.map((i) => i.kind)))
  const svcAll = all.flatMap((r) => r.items.map((it) => ({ it, s: r.server })))
  const dbItems = svcAll.filter(({ it }) => !it.web && !it.svc)
  const dbBad = live.filter((r) => (r.server.db_health ?? []).some((h) => h.problems.some((p) => p.level >= 2))).map((r) => r.server.name)
  const dbWarn = live.filter((r) => (r.server.db_health ?? []).some((h) => h.problems.some((p) => p.level < 2))).map((r) => r.server.name)
  const webItems = svcAll.filter(({ it }) => it.web)
  const siteN = webItems.reduce((n, { it }) => n + (it.sites?.length ?? 0), 0)
  const missN = all.reduce((n, r) => n + missingSites([...new Set(r.items.flatMap((it) => it.sites ?? []))], sitesCtx), 0)
  const cfgBad = live.filter((r) => (r.server.web_config ?? []).some((c) => !c.ok)).map((r) => r.server.name)
  // сертификаты ближе месяца - видно; алертный порог (неделя, день) - цветом плитки. Срок не
  // устаревает, поэтому по всем нодам
  const certs = all.flatMap((r) => (r.server.web_certs ?? []).filter((c) => c.days <= EXPIRY_SHOW_DAYS))
  const rabbit = svcAll.filter(({ it, s }) => it.svc?.kind === 'rabbitmq' && s.online)
  const qAll = rabbit.flatMap(({ it }) => it.svc?.queues ?? [])
  const qHot = qAll.filter((x) => qDepth(x) >= QUEUE_HOT)

  return (
    <div>
      <div className="page-head">
        <h2>{t('Сервисы')}</h2>
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
      {all.length > 0 && (
        <div className="net-tiles hs-tiles">
          <NetTile
            label={t('Ноды с сервисами')}
            value={String(all.length)}
            sub={subOf(
              t('типов сервисов: {n}', { n: kindsAll.size }),
              offNames.length > 0 && t('не на связи: {list}', { list: names(offNames) }),
            )}
            tone={offNames.length ? 'warn' : undefined}
          />
          <NetTile
            label={t('Базы данных')}
            value={String(dbItems.length)}
            sub={live.length ? subOf(
              dbBad.length > 0 && t('проблемы: {list}', { list: names(dbBad) }),
              dbWarn.length > 0 && t('предупреждения: {list}', { list: names(dbWarn) }),
            ) || t('все в порядке') : t('свежих данных нет')}
            tone={!live.length ? undefined : dbBad.length ? 'bad' : dbWarn.length ? 'warn' : 'ok'}
          />
          <NetTile
            label={t('Веб-серверы')}
            value={String(webItems.length)}
            sub={subOf(
              t('сайтов: {n}', { n: siteN }),
              sitesCtx.hosts != null && missN > 0 && t('без монитора: {n}', { n: missN }),
              cfgBad.length > 0 && t('конфиг с ошибкой: {list}', { list: names(cfgBad) }),
              certs.length > 0 && t('сертификаты истекают: {list}', { list: names(certs.map((c) => c.label)) }),
            )}
            tone={cfgBad.length || certs.some((c) => c.days <= 1) ? 'bad' : certs.some((c) => c.days <= 7) ? 'warn' : 'ok'}
          />
          {rabbit.length > 0 && (
            <NetTile
              label={t('Очереди RabbitMQ')}
              value={String(rabbit.reduce((n, { it }) => n + (it.svc?.total ?? 0), 0))}
              sub={subOf(
                t('сообщений: {m}', { m: rpmText(qAll.reduce((a, x) => a + qDepth(x), 0)) }),
                qHot.length > 0 && t('переполнены: {list}', { list: names(qHot.map((x) => x.name)) }),
              )}
              tone={qHot.length ? 'warn' : 'ok'}
            />
          )}
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
        <div className="net-cards hs-groups">
          {groups.map((g) => (
            <div key={g.key} className="chart-card net-card net-card-wide">
              <div className="net-head">
                <div className="chart-cap net-cap">
                  {groupBy === 'none' ? t('Ноды') : g.label || t('Без группы')}
                  <span className="net-n">{g.items.length}</span>
                </div>
              </div>
              <div className="loc-results">
                {g.items.map((r) => (
                  <HostRow key={r.server.id} srv={r} showGroup={groupBy !== 'group'} onOpen={() => setOpenId(r.server.id)} />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
      {open && (
        <ServerServicesModal
          key={open.server.id}
          srv={open}
          onClose={() => {
            setOpenId(null)
            setLinkSec({ id: null, sec: null })
          }}
          onChanged={load}
          initialSec={linkSec.id === open.server.id ? linkSec.sec : null}
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
