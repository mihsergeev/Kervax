import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  knownHosts,
  listServers,
  snoozeServerAlert,
  updateServer,
  type DBStat,
  type DbAdvice,
  type ChHealth,
  type DbHealth,
  type RdHealth,
  type DbLoad,
  type MyLoad,
  type OsFixStep,
  type QueueStat,
  type Server,
  type QueueFailed,
  type ServiceInfo,
  type WorkerDaemon,
  type CronJob,
  type LogErrSource,
  type FpmPool,
  type WorkerProg,
} from './api'
import { LlmSection } from './Gpu'
import { currentLang, useI18n } from './i18n'
import { useAuth } from './auth'
import { useUrlCard, useUrlParam } from './deeplink'
import { shortList, srvIssues, srvMuted, webRate } from './serverUtils'
import { fmtRel } from './ServersPage'
import { FixSteps } from './FixSteps'
import { CardNav, FreshChip, markMuted, OvAreas, OvRows, secInfoOf, type OvRow, type SecDef, type SecInfo, type SecItem } from './cardLayout'
import { AdoptSitesModal, adoptable } from './AdoptSitesModal'
import { EngineIcon } from './engineIcon'
import { CRON_MUTE, FPM_MUTE, LOGERR_MUTE, QFAIL_MUTE, QSTUCK_MUTE, WORKER_MUTE } from './mutes'
import { OsIcon } from './osIcon'
import { CountryFlag } from './CountryFlag'
import { byteUnits } from './units'
import { NetTile, TileFilterChip, useTileFilter } from './NetLinks'
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
  llm?: boolean // LLM-сервис (vLLM, SGLang, TEI): метрики в s.llm, свой раздел
  workers?: boolean // демон воркеров (supervisor, pm2): программы в s.workers, свой раздел
  cron?: boolean // кроны (helper cron-setup): задачи в s.cron, свой раздел; ноду в список не добавляет
}
type SrvServices = { server: Server; items: SvcItem[] }

const QUEUE_HOT = 100 // очередь глубже — подсвечиваем: обычно отставший консьюмер
// иконка брокера очередей по тому, где они лежат (helper queues-setup); без engine - RabbitMQ
const QUEUE_ENGINE_ICON: Record<string, string> = { redis: 'Redis', pg: 'PostgreSQL', mysql: 'MySQL/MariaDB' }
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

// порог очереди: свое значение важнее общего по ноде; 0 = не алертить
function queueThr(s: Server, key: string): number {
  const over = s.queue_alert_over ?? {}
  return key in over ? over[key] : (s.queue_alert_depth ?? 0)
}

// Беда очереди для сортировки и фильтра: 0 - растет и не разбирается, 1 - выше своего порога,
// 2 - глубокая или без потребителей, null - в порядке. Заглушенная (не следить) - не беда
function queueRank(s: Server, svc: ServiceInfo, q: QueueStat): number | null {
  const key = queueKey(svc.source || '', q)
  if (s.queue_mutes?.[key]) return null
  const d = qDepth(q)
  if (s.queue_watch?.[key]) return 0
  const thr = queueThr(s, key)
  if (thr > 0 && d >= thr) return 1
  if (d >= QUEUE_HOT || (q.consumers === 0 && d > 0)) return 2
  return null
}

// сколько очередь растет и не разбирается: 40 мин, 2 ч 10 мин
function qAge(sec: number, t: (s: string, v?: Record<string, string | number>) => string): string {
  if (sec >= 3600) return t('{h} ч {m} мин', { h: Math.floor(sec / 3600), m: Math.floor((sec % 3600) / 60) })
  return t('{n} мин', { n: Math.max(1, Math.floor(sec / 60)) })
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

function fmtCount(n: number): string {
  return Math.round(n).toLocaleString(currentLang() === 'ru' ? 'ru-RU' : 'en-US')
}

// Совет по настройкам (backend db_health.advice) словами: что не так, на сколько поправить и нужен ли перезапуск
function adviceText(a: DbAdvice, t: (s: string, v?: Record<string, string | number>) => string): string {
  const n = (k: string) => Number(a[k] ?? 0)
  const b = (k: string) => fmtBytes(n(k))
  switch (a.k) {
    case 'hugepages':
      return a.hp === 'try' && n('have') >= n('need')
        ? t('Ядро держит {have} huge pages, а postgres работает без них: видимо, страницы выделены после его запуска. Нужен перезапуск postgres', { have: fmtCount(n('have')) })
        : t('shared_buffers {sb} без huge pages: таблицы страниц на сервере занимают {pt}, каждое подключение отображает все буферы страницами по 4 КБ. Нужно vm.nr_hugepages = {need} в /etc/sysctl.d{hp} и перезапуск postgres. На работающем сервере ядро может не найти столько свободных блоков, надежнее выделять при остановленном postgres', { sb: b('sb'), pt: b('pt'), need: n('need'), hp: a.hp === 'off' ? t(', huge_pages = try') : '' })
    case 'max_wal_size':
      return t('Чекпоинты чаще идут по объему WAL, чем по времени: при {wal} WAL в час max_wal_size {cur} мал, а после каждого чекпоинта страницы снова пишутся в WAL целиком. Примерно {rec} хватит, чтобы чекпоинты шли по checkpoint_timeout, перезапуск не нужен', { wal: b('wal'), cur: b('cur'), rec: b('rec') })
    case 'wal_buffers':
      return t('Буфер WAL переполнялся {n} раз за час: запросы ждали записи WAL на диск. wal_buffers {cur}, обычно хватает 64-128 МБ (нужен перезапуск)', { n: fmtCount(n('n')), cur: b('cur') })
    case 'wal_compression':
      return t('wal_compression = {cur}{alias}: pglz самый медленный вариант, lz4 сжимает почти так же и в разы быстрее. Поставить lz4 можно без перезапуска', { cur: String(a.cur), alias: a.cur === 'on' ? t(' (это pglz)') : '' })
    case 'wal_compression_off':
      return t('Сжатие WAL выключено, а WAL {wal} в час, полные копии страниц - {fpi}% записей: wal_compression = lz4 уменьшит WAL и запись на диск, перезапуск не нужен', { wal: b('wal'), fpi: n('fpi') })
    case 'stop_timeout':
      return [
        n('to') < 60 && n('sb') >= 1024 ** 3
          ? t('Docker при остановке ждет postgres {to} с, потом убивает: с буферами {sb} остановка может не успеть, и тогда после docker stop или перезагрузки сервера база восстанавливается как после аварии. В compose stop_grace_period: 5m (контейнер пересоздается), для ручной остановки docker stop -t 600', { to: n('to'), sb: b('sb') })
          : '',
        a.sig === 'SIGTERM'
          ? t('Сигнал остановки SIGTERM: postgres ждет, пока отключатся все клиенты, а пул соединений сам не отключится. Нужен stop_signal: SIGINT')
          : '',
      ].filter(Boolean).join(' ')
    case 'temp':
      return t('Запросы пишут временные файлы {s} в час: сортировки и хеши не помещаются в work_mem ({wm}), это запись на диск и его износ. Поднимать work_mem осторожно: он выделяется на каждую операцию каждого подключения, а max_connections {mc}. Обычно дешевле поправить запросы с временными файлами', { s: b('temp'), wm: b('wm'), mc: n('mc') })
    case 'random_page_cost':
      return t('random_page_cost = {v}, а диски SSD: планировщик считает чтение по индексу в разы дороже последовательного и зря выбирает полный проход таблицы. Для SSD обычно 1.1, перезапуск не нужен', { v: n('v') })
    case 'effective_cache_size':
      return t('effective_cache_size {ecs} при {mem} памяти: планировщик думает, что кэша мало, и реже выбирает индексы. Обычно 50-75% памяти, перезапуск не нужен', { ecs: b('ecs'), mem: b('mem') })
    case 'shared_buffers_low':
      return t('shared_buffers {sb} при {mem} памяти{lim}, а база намного больше буферов: обычно ставят около 25% памяти (нужен перезапуск)', { sb: b('sb'), mem: b('mem'), lim: a.lim ? t(' (лимит контейнера)') : '' })
    case 'shared_buffers_high':
      return t('shared_buffers {sb}, а это больше половины памяти ({mem}{lim}): остальному серверу и кэшу ОС остается мало, обычно ставят около 25% (нужен перезапуск)', { sb: b('sb'), mem: b('mem'), lim: a.lim ? t(', лимит контейнера') : '' })
    // MySQL/MariaDB (dbstat-setup 0.11)
    case 'my_bp_low':
      return t('innodb_buffer_pool_size {bp} при {mem} памяти{lim}, а данных {data}: InnoDB читает с диска {rd} в час. Если машина только под базу, обычно ставят 50-70% памяти. Меняется без перезапуска (SET GLOBAL innodb_buffer_pool_size), значение надо записать и в конфиг', { bp: b('bp'), mem: b('mem'), lim: a.lim ? t(' (лимит контейнера)') : '', data: b('data'), rd: b('rd') })
    case 'my_bp_high':
      return t('innodb_buffer_pool_size {bp}, а это больше 80% памяти ({mem}{lim}): подключениям, временным таблицам и самому серверу остается мало, на пике процесс убьет OOM', { bp: b('bp'), mem: b('mem'), lim: a.lim ? t(', лимит контейнера') : '' })
    case 'my_redo':
      return t('Redo пишется {redo} в час, а журнал {cur} ({v}): он переписывается по кругу чаще раза в полчаса, и InnoDB приходится сбрасывать страницы на диск раньше времени. Обычно журнала хватает на час записи, примерно {rec}', { redo: b('redo'), cur: b('cur'), v: String(a.var), rec: b('rec') })
        + ', ' + (a.online ? t('меняется без перезапуска') : t('нужен перезапуск'))
    case 'my_log_buffer':
      return t('Запись ждала свободного места в буфере redo {n} раз за час: innodb_log_buffer_size {cur} мал, обычно ставят 64 МБ', { n: fmtCount(n('n')), cur: b('cur') })
        + ' (' + (a.online ? t('без перезапуска') : t('нужен перезапуск')) + ')'
    case 'my_tmp':
      return t('Временных таблиц на диске {n} в час из {all}: они не помещаются в память, а работает меньший из лимитов tmp_table_size {tts} и max_heap_table_size {mhts}. Поднимать осторожно, лимит действует на каждую таблицу каждого запроса. Обычно дешевле поправить запросы с временными таблицами на диске', { n: fmtCount(n('n')), all: fmtCount(n('all')), tts: b('tts'), mhts: b('mhts') })
    case 'my_binlog':
      return t('Бинлоги занимают {b} и не удаляются: binlog_expire_logs_seconds и expire_logs_days равны 0. Поставьте срок, например binlog_expire_logs_seconds = 604800 (7 дней), перезапуск не нужен. Реплика, если она есть, должна успевать забирать их за этот срок', { b: b('b') })
    case 'my_stop_timeout':
      return t('Docker при остановке ждет MySQL {to} с, потом убивает: с буфером {bp} InnoDB может не успеть сбросить страницы, и после docker stop или перезагрузки сервера база восстанавливается по redo как после аварии. В compose stop_grace_period: 5m (контейнер пересоздается), для ручной остановки docker stop -t 600', { to: n('to'), bp: b('bp') })
    default:
      return ''
  }
}

// Шаг включения pg_stat_statements словами: команды под экземпляр собирает бэкенд (db_health.pgss_fix),
// тексты здесь по id, как у шагов обновлений ОС
function pgssFixText(st: OsFixStep, t: TFn): string {
  const a = st.arg ?? ''
  switch (st.id) {
    case 'pgss_look':
      return a === 'docker'
        ? t('Сначала посмотреть, что уже загружено и откуда задано. source = command line - параметр в command контейнера, ALTER SYSTEM его не перебьет, тогда путь через compose ниже. Вторая строка - команда контейнера, каталог compose, проект и сервис.')
        : t('Сначала посмотреть, что уже загружено и откуда задано. source = command line - параметр в команде запуска сервиса, ALTER SYSTEM его не перебьет.')
    case 'pgss_create':
      return t('Создать расширение в базе {db}: панель ищет его там первым, а статистика в нем общая на весь сервер, одной базы достаточно. Работает и до перезапуска и заодно проверяет, что расширение есть в сборке: при ошибке is not available дальше не идти, без него PostgreSQL после перезапуска не запустится (на RHEL оно в пакете postgresql-contrib).', { db: a })
    case 'pgss_alter':
      return a
        ? t('Дописать pg_stat_statements к shared_preload_libraries, сейчас там {cur}. ALTER SYSTEM пишет в postgresql.auto.conf в каталоге данных, база работает дальше, значение вступит в силу после перезапуска. Каждая библиотека в своих кавычках: строку через запятую PostgreSQL принял бы за одну библиотеку и не запустился бы.', { cur: a })
        : t('Добавить pg_stat_statements в shared_preload_libraries, сейчас там пусто. ALTER SYSTEM пишет в postgresql.auto.conf в каталоге данных, база работает дальше, значение вступит в силу после перезапуска.')
    case 'pgss_alter_hand':
      return t('Если source не command line: добавить в shared_preload_libraries. Если в setting уже что-то есть, перечислите все через запятую, каждую в своих кавычках: {ok}. Одной строкой {bad} нельзя: PostgreSQL примет ее за одну библиотеку и не запустится.', { ok: "= 'timescaledb', 'pg_stat_statements'", bad: "'timescaledb,pg_stat_statements'" })
    case 'pgss_restart':
      return t('Перезапустить контейнер, чтобы библиотека загрузилась: база недоступна несколько секунд, приложения теряют соединения и должны переподключиться, лучше в тихое время. -t 120 дает базе спокойно завершиться, обычно это секунды.')
    case 'pgss_restart_host':
      return a
        ? t('Перезапустить PostgreSQL ({unit}), чтобы библиотека загрузилась: база недоступна несколько секунд, приложения теряют соединения и должны переподключиться, лучше в тихое время.', { unit: a })
        : t('Перезапустить PostgreSQL, чтобы библиотека загрузилась: база недоступна несколько секунд, приложения теряют соединения и должны переподключиться. На Debian и Ubuntu postgresql перезапускает все кластеры, один - postgresql@<версия>-<кластер> (systemctl list-units postgresql*), на RHEL юнит бывает postgresql-<версия>.')
    case 'pgss_compose':
      return t('Параметр задан в command сервиса {svc} в compose, и ALTER SYSTEM его не перебьет: правится compose. Копия файла рядом (.bak-kervax), sed дописывает pg_stat_statements к shared_preload_libraries, diff показывает измененные строки. Должна поменяться одна, у {svc}: если поменялись и чужие или diff пустой (значение задано иначе, через переменную), верните копию и поправьте руками.', { svc: a })
    case 'pgss_compose_grep':
      return t('Параметр задан в command сервиса в compose, и ALTER SYSTEM его не перебьет: правится compose. Найдите строку и поставьте shared_preload_libraries={v}, файл перед правкой скопируйте.', { v: a })
    case 'pgss_compose_up':
      return t('Пересоздать контейнер с новой командой: данные в томе остаются, база недоступна несколько секунд, приложения теряют соединения и должны переподключиться. --no-deps не трогает остальные сервисы проекта.')
    case 'pgss_compose_hand':
      return t('Если source = command line: вместо ALTER SYSTEM и перезапуска правится compose. В файле из второй строки первого шага у этого сервиса в command дописать ,pg_stat_statements к shared_preload_libraries=, файл перед правкой скопировать и пересоздать контейнер (каталог, проект и сервис оттуда же):')
    case 'pgss_cmdline':
      return t('Параметр задан в команде запуска, и ALTER SYSTEM его не перебьет. Поставьте там shared_preload_libraries={v}: контейнер не из compose пересоздается с новой командой (данные в томе остаются), у сервиса на хосте команда в юните или в pg_ctl.conf. Текущая команда:', { v: a })
    case 'pgss_check':
      return t('Проверить: в списке есть pg_stat_statements, а запрос отвечает числом, а не ошибкой.')
    case 'pgss_undo':
      return a
        ? t('Удалить расширение, вернуть прежний список ({cur}) и перезапустить.', { cur: a })
        : t('Удалить расширение, вернуть shared_preload_libraries как было (пусто) и перезапустить.')
    case 'pgss_undo_compose':
      return t('Удалить расширение, вернуть compose из копии и пересоздать контейнер.')
    case 'pgss_undo_hand':
      return t('Удалить расширение, вернуть shared_preload_libraries и перезапустить. Если в setting было что-то еще, вместо RESET верните прежний список: {ex}. Если меняли compose, верните файл и пересоздайте контейнер.', { ex: "ALTER SYSTEM SET shared_preload_libraries = 'timescaledb'" })
    default:
      return st.id
  }
}

function DbLoadBlock({ load, advice, fix, name, t }: {
  load: DbLoad | null | undefined
  advice: DbAdvice[]
  fix: OsFixStep[]
  name: string
  t: (s: string, v?: Record<string, string | number>) => string
}) {
  const facts: string[] = []
  const hints: string[] = []
  const [fixOpen, setFixOpen] = useState(false)
  const tips = advice.map((a) => adviceText(a, t)).filter(Boolean)
  // без блока нагрузки (не PostgreSQL 10+ или helper не дал) - только советы
  const ld: DbLoad = load ?? { win: -1, pss: '', qtext: 0, cut: false, top: [] }
  if (ld.win > 0) {
    if (ld.temp_b) facts.push(t('временные файлы {s} в час', { s: fmtBytes(ld.temp_b) }))
    if (ld.rd) facts.push(t('чтение мимо shared_buffers {s} в час', { s: fmtBytes(ld.rd) }))
    if (ld.wal_b) {
      const fpi = ld.wal_rec && ld.fpi != null ? Math.round((ld.fpi * 100) / ld.wal_rec) : null
      facts.push(t('WAL {s} в час', { s: fmtBytes(ld.wal_b) }) + (fpi != null ? `, ${t('полные копии страниц: {p}% записей', { p: fpi })}` : ''))
    }
    if (ld.ck_t != null && ld.ck_r != null) facts.push(t('чекпоинты: {a} по времени, {b} внеплановых', { a: ld.ck_t, b: ld.ck_r }))
    if (ld.wbf) facts.push(t('буфер WAL переполнялся {n} раз за час', { n: fmtCount(ld.wbf) }))
    if (ld.xact != null) facts.push(t('{n} транзакций в секунду', { n: (ld.xact / 3600).toFixed(ld.xact < 36000 ? 1 : 0) }))
  }
  // плашка с готовыми командами под этот экземпляр, как у пунктов обновлений ОС
  const pssHint = ld.pss === 'off'
    ? t('Самые долгие запросы покажет pg_stat_statements: добавьте его в shared_preload_libraries (нужен перезапуск PostgreSQL) и выполните CREATE EXTENSION pg_stat_statements')
    : ld.pss === 'noext'
      ? t('pg_stat_statements загружен, но расширение не создано ни в одной базе: выполните CREATE EXTENSION pg_stat_statements, перезапуск не нужен. Панель увидит его в течение часа')
      : ''
  if (ld.qtext > 1024 ** 3)
    hints.push(t('Тексты запросов pg_stat_statements занимают {s}: так бывает, когда один запрос приходит с разным числом значений (многострочный INSERT, длинный IN), и каждый вариант хранится отдельно со своим текстом. Они вытесняют из статистики другие запросы, а каждое чтение текстов поднимает файл в память целиком, поэтому новые тексты панель читает раз в 6 часов', { s: fmtBytes(ld.qtext) }))
  if (ld.cut) hints.push(t('Запросы не показаны: на сервере много инстансов, и отчет не вместил бы все'))
  const [open, setOpen] = useState<number | null>(null) // текст запроса целиком по нажатию (на телефоне нет подсказки)
  const quiet = (ld.ms_all ?? 0) < LOAD_QUIET_MS
  const multiDb = new Set(ld.top.map((q) => q.db).filter(Boolean)).size > 1
  return (
    <div className="svc-db-load">
      <div className="svc-db-load-head">
        <span className="svc-db-load-title">{load ? t('Что грузит базу') : t('Настройки PostgreSQL')}</span>
        {name && <span className="mono muted small">{name}</span>}
        {ld.win > 0 && (
          <span className="muted small">
            {ld.win >= 3000 ? t('за последний час') : t('за {n} мин, в пересчете на час', { n: Math.max(1, Math.round(ld.win / 60)) })}
          </span>
        )}
      </div>
      {ld.win === 0 && ld.pss !== 'off' && ld.pss !== 'noext' && (
        <div className="muted small">{t('Собираю данные: первая картина через 15 минут, за полный час - через час')}</div>
      )}
      {facts.length > 0 && <div className="svc-db-load-facts small">{facts.map((f) => <span key={f}>{f}</span>)}</div>}
      {ld.win > 0 && ld.pss === 'on' && !ld.cut && ld.pbase === 0 && (
        <div className="muted small">{t('Самые долгие запросы появятся примерно через час после включения pg_stat_statements: панель сравнивает их со снимком часовой давности')}</div>
      )}
      {ld.win > 0 && ld.pss === 'on' && !ld.cut && ld.pbase !== 0 && quiet && (
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
      {pssHint && (
        <div>
          <div className="osupd-adv net-note">
            <span className="osupd-adv-text">{pssHint}</span>
            {fix.length > 0 && (
              <button className="ghost small row-act" onClick={() => setFixOpen(!fixOpen)}>
                {fixOpen ? t('скрыть') : t('как включить')}
              </button>
            )}
          </div>
          {fixOpen && fix.length > 0 && (
            <FixSteps steps={fix} text={(st) => pgssFixText(st, t)} foot={ld.pss === 'noext'
              ? t('Панель увидит расширение в течение часа, самые долгие запросы - еще примерно через час: она сравнивает со снимком часовой давности.')
              : t('Плашка уйдет при следующем сборе, до 15 минут. Самые долгие запросы появятся примерно через час: панель сравнивает со снимком часовой давности.')} />
          )}
        </div>
      )}
      {hints.map((h) => <div key={h} className="net-note">{h}</div>)}
      {tips.length > 0 && <div className="svc-db-load-sub">{t('Что поправить в настройках')}</div>}
      {tips.map((h) => <div key={h} className="net-note">{h}</div>)}
    </div>
  )
}

// Что грузит MySQL/MariaDB за последний час (dbstat-setup 0.11): тот же разбор, что у PostgreSQL, на
// счетчиках SHOW GLOBAL STATUS и запросах performance_schema. Отказы по max_connections здесь не
// повторяем: они строкой проблемы в той же карточке
function MyLoadBlock({ load, advice, name, t }: {
  load: MyLoad | null | undefined
  advice: DbAdvice[]
  name: string
  t: TFn
}) {
  const [open, setOpen] = useState<number | null>(null)
  const ld: MyLoad = load ?? { win: -1, ps: '', cut: false, run: 0, max_used: 0, top: [] }
  const tips = advice.map((a) => adviceText(a, t)).filter(Boolean)
  const facts: string[] = []
  const hints: string[] = []
  const perSec = (v: number) => (v / 3600).toFixed(v < 36000 ? 1 : 0)
  if (ld.win > 0) {
    if (ld.q != null)
      facts.push(t('{n} запросов в секунду', { n: perSec(ld.q) }) + (ld.wr
        ? `, ${ld.wr >= 360 ? t('изменений {n} в секунду', { n: perSec(ld.wr) }) : t('изменений {n} в час', { n: fmtCount(ld.wr) })}`
        : ''))
    if (ld.slow) facts.push(t('медленных запросов за час: {n}', { n: fmtCount(ld.slow) }))
    if (ld.rd)
      facts.push(t('чтение с диска мимо буфера InnoDB {s} в час', { s: fmtBytes(ld.rd) }) + (ld.hit != null ? `, ${t('попаданий в буфер {p}%', { p: ld.hit })}` : ''))
    if (ld.redo) facts.push(t('redo {s} в час', { s: fmtBytes(ld.redo) }))
    if (ld.tmp_disk) facts.push(t('временных таблиц на диске {n} в час из {m}', { n: fmtCount(ld.tmp_disk), m: fmtCount(ld.tmp ?? 0) }))
    if (ld.lock_waits) facts.push(t('ожиданий блокировок строк {n} в час, всего {d}', { n: fmtCount(ld.lock_waits), d: durMs(ld.lock_ms ?? 0, t) }))
    if (ld.deadlocks) facts.push(t('дедлоков за час: {n}', { n: fmtCount(ld.deadlocks) }))
    if ((ld.aborted ?? 0) >= 10) facts.push(t('неудачных подключений {n} в час: неверный пароль или обрыв при входе', { n: fmtCount(ld.aborted ?? 0) }))
  }
  if (ld.ps === 'off')
    hints.push(t('Самые долгие запросы покажет performance_schema: в MariaDB он по умолчанию выключен. Включается строкой performance_schema = ON в разделе [mysqld], нужен перезапуск'))
  if (ld.cut) hints.push(t('Запросы не показаны: на сервере много инстансов, и отчет не вместил бы все'))
  const quiet = (ld.ms_all ?? 0) < LOAD_QUIET_MS
  const multiDb = new Set(ld.top.map((q) => q.db).filter(Boolean)).size > 1
  return (
    <div className="svc-db-load">
      <div className="svc-db-load-head">
        <span className="svc-db-load-title">{load ? t('Что грузит базу') : t('Настройки MySQL')}</span>
        {name && <span className="mono muted small">{name}</span>}
        {ld.win > 0 && (
          <span className="muted small">
            {ld.win >= 3000 ? t('за последний час') : t('за {n} мин, в пересчете на час', { n: Math.max(1, Math.round(ld.win / 60)) })}
          </span>
        )}
      </div>
      {ld.win === 0 && <div className="muted small">{t('Собираю данные: первая картина через 15 минут, за полный час - через час')}</div>}
      {facts.length > 0 && <div className="svc-db-load-facts small">{facts.map((f) => <span key={f}>{f}</span>)}</div>}
      {ld.win > 0 && ld.ps === 'on' && !ld.cut && quiet && (
        <div className="muted small">{t('Запросы заняли {s} с за час - база почти не нагружена', { s: Math.round((ld.ms_all ?? 0) / 1000) })}</div>
      )}
      {ld.win > 0 && !quiet && ld.top.length > 0 && (
        <div className="svc-db-load-top">
          {ld.top.map((q, i) => (
            <div key={i} className="svc-db-load-q">
              <div className={q.q ? `mono svc-db-load-text${open === i ? ' open' : ''}` : 'muted small'} title={q.q || undefined}
                onClick={() => q.q && setOpen(open === i ? null : i)}>
                {q.q || t('текст скрыт: в нем пароль или секрет')}
              </div>
              <div className="muted small">
                {[
                  t('{p}% времени запросов', { p: q.share }),
                  t('{n} вызовов', { n: fmtCount(q.calls) }),
                  t('{d} в среднем', { d: durMs(q.avg_ms, t) }),
                  q.rows_call >= 1000 ? t('просматривает {n} строк за вызов', { n: fmtCount(q.rows_call) }) : '',
                  q.noidx > 0 ? t('без индекса') : '',
                  q.tmp > 0 ? t('временных таблиц на диске {n}', { n: fmtCount(q.tmp) }) : '',
                  multiDb && q.db ? t('база {db}', { db: q.db }) : '',
                ].filter(Boolean).join(' · ')}
              </div>
            </div>
          ))}
        </div>
      )}
      {hints.map((h) => <div key={h} className="net-note">{h}</div>)}
      {tips.length > 0 && <div className="svc-db-load-sub">{t('Что поправить в настройках')}</div>}
      {tips.map((h) => <div key={h} className="net-note">{h}</div>)}
    </div>
  )
}

// ClickHouse и Redis (dbstat-setup 0.10): по строке на инстанс, проблемные сверху. На одной ноде их
// бывают десятки, полный блок на каждый растянул бы карточку на несколько экранов
const ENG_ROWS = 6
type TFn = (s: string, v?: Record<string, string | number>) => string

function durMs(ms: number, t: TFn): string {
  if (ms < 1000) return t('{ms} мс', { ms: ms >= 10 ? Math.round(ms) : +ms.toFixed(1) })
  if (ms < 60_000) return t('{n} с', { n: +(ms / 1000).toFixed(ms < 10_000 ? 1 : 0) })
  return t('{n} мин', { n: Math.round(ms / 60_000) })
}

type EngFact = { text: string; tone?: string }

function chFacts(ch: ChHealth, t: TFn): EngFact[] {
  const f: EngFact[] = []
  const p = ch.parts
  if (p && p.max > 0)
    f.push({
      text: `${t('кусков в партиции до {n}', { n: p.max })} (${p.tbl})`,
      tone: p.throw && p.max >= p.throw * 0.8 ? 't-down' : p.delay && p.max >= p.delay ? 't-degraded' : undefined,
    })
  const ins = ch.ins
  if (ins && ins.win > 0) {
    f.push({ text: t('вставок за {m} мин: {n}', { m: Math.max(1, Math.round(ins.win / 60)), n: fmtCount(ins.n) }) })
    if (ins.rej > 0) f.push({ text: t('отклонено вставок: {n}', { n: fmtCount(ins.rej) }), tone: 't-down' })
    if (ins.del > 0) f.push({ text: t('замедлено вставок: {n}', { n: fmtCount(ins.del) }), tone: 't-degraded' })
  }
  if (ch.merges) f.push({ text: t('слияний сейчас: {n}', { n: ch.merges }) })
  if (ch.mut?.n) f.push({ text: t('мутаций в работе: {n}', { n: ch.mut.n }), tone: ch.mut.err ? 't-degraded' : undefined })
  if (ch.rep?.n)
    f.push({ text: t('реплицируемых таблиц: {n}', { n: ch.rep.n }) + (ch.rep.q_n ? `, ${t('в очереди репликации {n}', { n: ch.rep.q_n })}` : '') })
  if (ch.detached) f.push({ text: t('отцепленных кусков: {n}', { n: ch.detached }) })
  if (ch.qerr) f.push({ text: t('запросов с ошибкой за час: {n}', { n: fmtCount(ch.qerr) }) })
  const ms = (ch.top ?? []).reduce((a, q) => a + q.ms, 0)
  if (ms) f.push({ text: t('самые долгие запросы заняли {d} за час', { d: durMs(ms, t) }) })
  if (!ch.qlog) f.push({ text: t('query_log выключен: запросы не видны') })
  if (ch.cut) f.push({ text: t('запросы не вместились в отчет') })
  return f
}

function ChTop({ top, t }: { top: ChHealth['top']; t: TFn }) {
  const [open, setOpen] = useState<number | null>(null) // текст запроса целиком по нажатию
  return (
    <div className="svc-db-load-top">
      {top.map((q, i) => (
        <div key={i} className="svc-db-load-q">
          <div className={q.q ? `mono svc-db-load-text${open === i ? ' open' : ''}` : 'muted small'} title={q.q || undefined}
            onClick={() => q.q && setOpen(open === i ? null : i)}>
            {q.q || t('текст скрыт: в нем пароль или секрет')}
          </div>
          <div className="muted small">
            {[
              t('{n} вызовов', { n: fmtCount(q.n) }),
              t('всего {d}', { d: durMs(q.ms, t) }),
              q.n ? t('{d} в среднем', { d: durMs(q.ms / q.n, t) }) : '',
              q.rd ? t('прочитано {s}', { s: fmtBytes(q.rd) }) : '',
              q.mem ? t('память до {s}', { s: fmtBytes(q.mem) }) : '',
            ].filter(Boolean).join(' · ')}
          </div>
        </div>
      ))}
    </div>
  )
}

function rdFacts(rd: RdHealth, t: TFn): EngFact[] {
  const f: EngFact[] = []
  if (rd.maxmem > 0) {
    const pct = rd.mem / rd.maxmem
    f.push({
      text: `${t('память {u} из {m}', { u: fmtBytes(rd.mem), m: fmtBytes(rd.maxmem) })} (${rd.policy})`,
      tone: rd.policy === 'noeviction' ? (pct >= 0.98 ? 't-down' : pct >= 0.9 ? 't-degraded' : undefined) : undefined,
    })
  } else f.push({ text: t('память {u}, без лимита', { u: fmtBytes(rd.mem) }) })
  if (rd.rdb === 'err') f.push({ text: t('снимок на диск не сохраняется'), tone: 't-down' })
  else if (rd.rdb_age >= 0)
    f.push({ text: t('снимок {d} назад', { d: lagText(rd.rdb_age, t) }) + (rd.rdb_changes ? `, ${t('изменений после него: {n}', { n: fmtCount(rd.rdb_changes) })}` : '') })
  if (rd.aof) f.push(rd.aof_status === 'err' ? { text: t('AOF не пишется'), tone: 't-down' } : { text: 'AOF' })
  if (rd.win > 0 && rd.evicted > 0)
    f.push({ text: t('вытеснено ключей за {m} мин: {n}', { m: Math.max(1, Math.round(rd.win / 60)), n: fmtCount(rd.evicted) }) })
  if (rd.win > 0 && rd.rejected > 0) f.push({ text: t('отклонено подключений: {n}', { n: fmtCount(rd.rejected) }), tone: 't-degraded' })
  // меньше секунды на команды за окно - Redis почти не нагружен, топ ни о чем не говорит
  const cmds = (rd.cmds ?? []).slice(0, 3)
  if (cmds.length && (rd.cmds ?? []).reduce((a, c) => a + c.us, 0) >= 1_000_000)
    f.push({
      text: t('тяжелее всех за {m} мин: {list}', {
        m: Math.max(1, Math.round(rd.win / 60)),
        list: cmds.map((c) => t('{c} {d} на {n} вызовов', { c: c.c, d: durMs(c.us / 1000, t), n: fmtCount(c.n) })).join(', '),
      }),
    })
  return f
}

// Список инстансов движка: имя, факты строкой, у ClickHouse по нажатию - самые долгие запросы
function EngineList({ items, kind, t }: { items: DbHealth[]; kind: 'ch' | 'rd'; t: TFn }) {
  const [all, setAll] = useState(false)
  const [open, setOpen] = useState<string | null>(null)
  const weight = (h: DbHealth) =>
    (h.problems.length ? 1e18 : 0) + (kind === 'ch' ? (h.ch?.top ?? []).reduce((a, q) => a + q.ms, 0) : h.rd?.mem ?? 0)
  const rows = [...items].sort((a, b) => weight(b) - weight(a))
  const shown = all ? rows : rows.slice(0, ENG_ROWS)
  const multi = rows.length > 1
  return (
    <div className="svc-db-load">
      <div className="svc-db-load-head">
        <span className="svc-db-load-title">
          {kind === 'ch' ? t('ClickHouse: куски, вставки, запросы') : t('Redis: память, снимки, команды')}
        </span>
        {rows.length > ENG_ROWS && (
          <button className="svc-act" onClick={() => setAll(!all)}>{all ? t('свернуть') : t('показать все')}</button>
        )}
      </div>
      {shown.map((h) => {
        const facts = kind === 'ch' && h.ch ? chFacts(h.ch, t) : h.rd ? rdFacts(h.rd, t) : []
        const top = kind === 'ch' ? h.ch?.top ?? [] : []
        const isOpen = !multi || open === h.container
        return (
          <div key={h.container} className="svc-eng-row">
            {multi ? (
              <div className="svc-eng-head">
                <span className="mono svc-eng-name" title={h.container || undefined}>{h.container || t('на хосте')}</span>
                <span className="svc-db-load-facts small">
                  {facts.map((f) => <span key={f.text} className={f.tone}>{f.text}</span>)}
                </span>
                {top.length > 0 ? (
                  <button className="svc-act" onClick={() => setOpen(isOpen ? null : h.container)}>
                    {isOpen ? t('скрыть запросы') : t('запросы')}
                  </button>
                ) : <span />}
              </div>
            ) : (
              <span className="svc-db-load-facts small">
                {facts.map((f) => <span key={f.text} className={f.tone}>{f.text}</span>)}
              </span>
            )}
            {isOpen && top.length > 0 && <ChTop top={top} t={t} />}
          </div>
        )
      })}
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
      {health.filter((h) => h.engine !== 'mysql' && (h.load || (h.advice ?? []).length > 0)).map((h) => (
        <DbLoadBlock key={'l' + h.container} load={h.load} advice={h.advice ?? []} fix={h.pgss_fix ?? []} name={health.length > 1 ? h.container : ''} t={t} />
      ))}
      {health.filter((h) => h.engine === 'mysql' && (h.my || (h.advice ?? []).length > 0)).map((h) => (
        <MyLoadBlock key={'m' + h.container} load={h.my} advice={h.advice ?? []} name={health.length > 1 ? h.container || t('на хосте') : ''} t={t} />
      ))}
      {health.some((h) => h.ch) && <EngineList items={health.filter((h) => h.ch)} kind="ch" t={t} />}
      {health.some((h) => h.rd) && <EngineList items={health.filter((h) => h.rd)} kind="rd" t={t} />}
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

// версия helper'а меньше нужной: "0.9" < "0.11" по частям, а не строкой
function setupLt(v: string, target: string): boolean {
  const a = v.split('.').map(Number)
  const b = target.split('.').map(Number)
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] ?? 0
    const y = b[i] ?? 0
    if (x !== y) return x < y
  }
  return false
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
  const ver = server.last_report?.setup_versions?.['dbstat-setup']
  // MySQL в подах helper видит с 0.11 (заходит в под своей ноды), остальные движки - только на хосте и в docker
  if (inKube) {
    if (kind !== DB_ENGINE_LABEL.mysql)
      return t('инвентарь снимается с хостовых и docker-баз — до баз в кластере хелпер не дотягивается')
    if (ver && setupLt(String(ver), '0.11')) return t('MySQL в подах helper видит с версии 0.11: обновите dbstat-setup')
    if (ver) return t('под на другой ноде (helper видит поды только своей ноды) или база не ответила')
  }
  if (!ver) {
    return t('базы и размеры не собраны: на ноде нет хелпера dbstat-setup')
  }
  if (kind === DB_ENGINE_LABEL.mysql)
    return t('база не ответила: helper входит как root через сокет, /etc/mysql/debian.cnf или /root/.my.cnf')
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
    // очереди от helper'а queues-setup: в Redis (Celery, Sidekiq, RQ, BullMQ, Laravel, Yii) и в таблицах
    // баз (Yii, Laravel, Symfony, Delayed Job, Solid Queue, GoodJob, Que, Oban, River, pg-boss, Hangfire)
    for (const svc of s.queue_brokers ?? []) items.push({ kind: svc.label || svc.kind, where: svc.where || '', svc })
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
    // LLM-сервисы (агент 2.36): свой раздел, по строке на сервис
    for (const x of s.llm ?? []) items.push({ kind: x.engine_name, where: x.where, llm: true })
    // демоны воркеров (helper workers-setup): supervisor и pm2, свой раздел
    for (const d of s.workers ?? []) items.push({ kind: d.label, where: d.where, workers: true })
    // кроны (helper cron-setup): cron есть почти на каждой ноде, в список нод он ее не добавляет,
    // но окно сервисов по ссылке из алерта открывается и у ноды без других сервисов
    const hasCron = (s.cron?.jobs ?? []).length > 0 || (s.log_errors?.sources ?? []).length > 0 || (s.fpm?.pools ?? []).length > 0
    // веб-серверы/прокси (nginx/envoy/…): их не бэкапят, поэтому идут отдельным источником
    for (const w of s.last_report?.web_services ?? []) {
      if (seen.has(w.kind)) continue
      seen.add(w.kind)
      items.push({ kind: w.kind, where: w.source || '', sites: w.sites, web: true })
    }
    const stats = s.last_report?.db_stats ?? []
    if (stats.length)
      for (const it of items) {
        if (it.web || it.llm || it.workers || it.cron) continue
        const m = matchDbStats(it.kind, stats)
        if (m.length) it.db = m
      }
    items.sort((a, b) => a.kind.localeCompare(b.kind))
    if (items.length || hasCron) out.push({ server: s, items })
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
// Упавшие задачи брокера (queues-setup 0.4): за час и сутки, что падало чаще, последняя ошибка;
// всплеск - красным, его можно не алертить (qfail:<источник>) на день, неделю или постоянно
function FailedJobs({ server, svc, f, busy, picking, onPick, mute, fmt }: {
  server: Server
  svc: ServiceInfo
  f: QueueFailed
  busy: boolean
  picking: boolean
  onPick: (v: boolean) => void
  mute: (kind: string, hours: number) => void
  fmt: (ts: string) => string
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const key = QFAIL_MUTE + (svc.source || '')
  const fm = server.queue_fail_mutes?.[svc.source || '']
  const top = f.top.filter((x) => x.job || x.q)
  const name = (x: { q: string; job: string }) =>
    x.job ? (x.q ? t('{job} в очереди {q}', { job: x.job, q: x.q }) : x.job) : t('очередь {q}', { q: x.q })
  return (
    <div className={`q-failed${f.spike && !fm ? ' q-failed-spike' : ''}`}>
      <div className="q-failed-head small">
        <span className={f.spike && !fm ? 'q-stuck' : f.h1 > 0 ? '' : 'muted'}>
          {subOf(
            t('упало за час: {n}', { n: rpmText(f.h1) }),
            f.span >= 86400 ? t('за сутки: {n}', { n: rpmText(f.d1) }) : t('за {p}: {n}', { p: qAge(f.span, t), n: rpmText(f.d1) }),
            f.spike && (f.base != null ? t('всплеск, обычно около {n} в час', { n: rpmText(Math.round(f.base)) }) : t('всплеск')),
          )}
        </span>
        {!isViewer && !fm && (f.h1 > 0 || f.spike) && (
          <button className="ghost small q-mute-btn" disabled={busy} title={t('Не алертить о всплеске упавших задач этого брокера: на день, неделю или постоянно')}
            onClick={() => onPick(!picking)}>
            🔕
          </button>
        )}
      </div>
      {top.length > 0 && f.h1 > 0 && (
        <div className="small muted">{t('чаще всего: {list}', { list: top.map((x) => `${name(x)} - ${x.n}`).join(', ') })}</div>
      )}
      {f.err && f.h1 > 0 && (
        <div className="docker-c-img w-log mono muted small" title={f.err}>
          ↳ {f.err_job && !f.err.includes(f.err_job) ? `${f.err_job}: ` : ''}{f.err}
        </div>
      )}
      {fm && (
        <div className="small">
          <span className="snooze-active-chip snooze-perm">
            🔇 {fm.perm ? t('не алертит, постоянно') : t('не алертит {t}', { t: t('до {t}', { t: fmt(fm.until!) }) })}
            {!isViewer && (
              <button className="chip-x" disabled={busy} title={t('Снять')} onClick={() => mute(key, 0)}>✕</button>
            )}
          </span>
        </div>
      )}
      {!fm && picking && (
        <div className="small q-mute-pick">
          <span className="muted">🔕 {t('не алертить о всплеске упавших:')}</span>
          {[
            { h: 24, l: t('1 день') },
            { h: 24 * 7, l: t('1 неделя') },
            { h: -1, l: t('постоянно') },
          ].map(({ h, l }) => (
            <button key={h} className="ghost small" disabled={busy} onClick={() => mute(key, h)}>{l}</button>
          ))}
          <button className="ghost small" disabled={busy} onClick={() => onPick(false)}>{t('Отмена')}</button>
        </div>
      )}
    </div>
  )
}

// вторая модалка поверх карточки
function QueueList({ server, svc, busy, save, onChanged, onlyProblems }: {
  server: Server
  svc: ServiceInfo
  busy: boolean
  save: (body: Parameters<typeof updateServer>[1]) => void
  onChanged: () => void
  onlyProblems: boolean
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [q, setQ] = useState('')
  // выключить алерт по очереди: какую очередь сейчас выбирают и идет ли запрос
  const [picking, setPicking] = useState<string | null>(null)
  const [mBusy, setMBusy] = useState(false)
  const mute = async (kind: string, hours: number) => {
    setMBusy(true)
    try {
      if (hours < 0) {
        await updateServer(server.id, { alert_mutes: [...new Set([...(server.alert_mutes ?? []), kind])] })
      } else if (hours === 0) {
        if ((server.alert_mutes ?? []).includes(kind))
          await updateServer(server.id, { alert_mutes: (server.alert_mutes ?? []).filter((x) => x !== kind) })
        if (server.alert_snoozes?.[kind]) await snoozeServerAlert(server.id, kind, 0)
      } else await snoozeServerAlert(server.id, kind, hours)
      setPicking(null)
      onChanged()
    } catch {
      /* кнопка остается, можно повторить */
    } finally {
      setMBusy(false)
    }
  }
  const fmt = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
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
  // с бедой - наверх (растет и не разбирается, выше порога, глубокая), заглушенные - вниз,
  // остальные в порядке агента (самые глубокие первыми)
  const order = (x: QueueStat) => {
    const r = queueRank(server, svc, x)
    return r ?? (server.queue_mutes?.[queueKey(svc.source || '', x)] ? 4 : 3)
  }
  const queues = all
    .filter((x) => !ql || x.name.toLowerCase().includes(ql))
    .filter((x) => !onlyProblems || queueRank(server, svc, x) != null)
    .map((x, i) => ({ x, i, o: order(x) }))
    .sort((a, b) => a.o - b.o || a.i - b.i)
    .map(({ x }) => x)
  const withMsgs = all.filter((x) => qDepth(x) > 0).length
  const hotN = all.filter((x) => qDepth(x) >= QUEUE_HOT).length
  const msgsAll = all.reduce((a, x) => a + qDepth(x), 0)
  // таблица failed_jobs Laravel - не очередь: только упавшие задачи
  const failOnly = svc.kind === 'laravel_failed'
  const now = Date.now() / 1000
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="hs-svc-title">
          <span className="svc-ico"><EngineIcon name={QUEUE_ENGINE_ICON[svc.engine ?? ''] ?? 'RabbitMQ'} /></span>
          {svc.label || 'RabbitMQ'}
          {!failOnly && <span className="net-n">{svc.total ?? all.length}</span>}
        </div>
      </div>
      {(svc.where || svc.source) && <div className="net-sub mono">{svc.where || svc.source}</div>}
      {!failOnly && (
        <div className="small muted">
          {subOf(
            t('с сообщениями: {n}', { n: withMsgs }),
            t('сообщений: {m}', { m: rpmText(msgsAll) }),
            hotN > 0 && t('переполнены: {n} (от {m} сообщений)', { n: hotN, m: QUEUE_HOT }),
          )}
        </div>
      )}
      {svc.failed && (
        <FailedJobs server={server} svc={svc} f={svc.failed} busy={mBusy} picking={picking === 'qfail'}
          onPick={(v) => setPicking(v ? 'qfail' : null)} mute={mute} fmt={fmt} />
      )}
      {!failOnly && all.length > 8 && (
        <div className="modal-toolbar">
          <input className="checks-search modal-search" placeholder={t('Фильтр: имя очереди…')}
            value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      )}
      {!failOnly && <div className="loc-results docker-clist">
        {queues.length === 0 ? (
          // в таблице очередей пусто - все задачи разобраны (выполненные система удаляет)
          <div className="muted small">{all.length === 0 ? t('Задач нет: все разобраны.') : t('Ничего не найдено.')}</div>
        ) : (
          queues.map((x) => {
            const d = qDepth(x)
            const key = queueKey(svc.source || '', x)
            // растет и не разбирается (queue_watch на бэкенде): красная, даже если еще неглубокая
            const stuck = server.queue_watch?.[key]
            const qm = server.queue_mutes?.[key]
            const tone = qm ? 't-up' : stuck || d >= QUEUE_HOT ? 't-down' : d > 0 ? 't-degraded' : 't-up'
            const eff = thrOf(key)
            return (
              <div key={(x.vhost || '') + '/' + x.name} className={`loc-res docker-row ${tone}${qm ? ' q-muted' : ''}`}>
                <span className={`lk-dot${qm ? '' : DOT[tone]}`} />
                <div className="docker-c-main">
                  <div className="docker-c-name mono">{x.name}</div>
                  {x.vhost && x.vhost !== '/' && (
                    <div className="docker-c-img mono muted small">vhost: {x.vhost}</div>
                  )}
                  {stuck && (
                    <div className={`docker-c-img small${stuck.muted ? ' muted' : ' q-stuck'}`}>
                      {stuck.stall
                        ? t('стоит: самая старая задача ждет {a}', { a: qAge(stuck.wait ?? stuck.age, t) })
                        : t('растет {a} и не разбирается, было {n}', { a: qAge(stuck.age, t), n: rpmText(stuck.d0) })}
                      {stuck.muted ? ` · ${t('заглушено')}` : ''}
                      {x.consumers === 0 ? ` · ${t('нет потребителей')}` : ''}
                    </div>
                  )}
                  {((!stuck && x.consumers === 0 && d > 0) || (!stuck?.stall && x.oldest && x.ready > 0)) && (
                    <div className="docker-c-img muted small">
                      {subOf(
                        !stuck && x.consumers === 0 && d > 0 && t('нет потребителей'),
                        !stuck?.stall && x.oldest && x.ready > 0 && t('самая старая ждет {a}', { a: qAge(Math.max(0, now - x.oldest), t) }),
                      )}
                    </div>
                  )}
                  {qm && (
                    <div className="small">
                      <span className="snooze-active-chip snooze-perm">
                        🔇 {qm.perm ? t('не алертит, постоянно') : t('не алертит {t}', { t: t('до {t}', { t: fmt(qm.until!) }) })}
                        {!isViewer && (
                          <button className="chip-x" disabled={mBusy} title={t('Снять')} onClick={() => mute(QSTUCK_MUTE + key, 0)}>
                            ✕
                          </button>
                        )}
                      </span>
                    </div>
                  )}
                  {!qm && picking === key && (
                    <div className="small q-mute-pick">
                      <span className="muted">🔕 {t('не алертить по этой очереди:')}</span>
                      {[
                        { h: 24, l: t('1 день') },
                        { h: 24 * 7, l: t('1 неделя') },
                        { h: -1, l: t('постоянно') },
                      ].map(({ h, l }) => (
                        <button key={h} className="ghost small" disabled={mBusy} onClick={() => mute(QSTUCK_MUTE + key, h)}>
                          {l}
                        </button>
                      ))}
                      <button className="ghost small" disabled={mBusy} onClick={() => setPicking(null)}>
                        {t('Отмена')}
                      </button>
                    </div>
                  )}
                </div>
                <div className="queue-nums mono">
                  <span title={t('в очереди, ждут доставки')}>{x.ready}</span>
                  {(x.unacked ?? 0) > 0 && (
                    <span className="muted" title={t('взяты консьюмером, ещё не подтверждены')}>+{x.unacked}</span>
                  )}
                </div>
                {!isViewer && !qm ? (
                  <button className="ghost small q-mute-btn" disabled={mBusy} title={t('Не алертить по этой очереди: на день, неделю или постоянно')}
                    onClick={() => setPicking(picking === key ? null : key)}>
                    🔕
                  </button>
                ) : (
                  <span className="q-mute-btn" />
                )}
                <input
                  className={`mono queue-thr-cell${eff > 0 ? ' queue-alert-on' : ''}`}
                  type="number"
                  min={0}
                  disabled={busy}
                  placeholder={t('порог')}
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
      </div>}
      {!failOnly && (svc.total ?? 0) > all.length && (
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
// Состояние программы словами: supervisor (RUNNING, FATAL...) и pm2 (online, errored...)
const WORKER_STATE: Record<string, string> = {
  RUNNING: 'работает', STARTING: 'запускается', BACKOFF: 'не может запуститься', STOPPING: 'останавливается',
  STOPPED: 'остановлен', EXITED: 'завершился', FATAL: 'не запускается', UNKNOWN: 'неизвестно',
  online: 'работает', launching: 'запускается', stopping: 'останавливается', stopped: 'остановлен',
  errored: 'не запускается', 'one-launch-status': 'отработал',
}
// остановленные руками: не беда, но и не работают
const WORKER_IDLE = new Set(['STOPPED', 'stopped'])
const WORKER_OK = new Set(['RUNNING', 'online'])

function wAge(sec: number, t: (s: string, v?: Record<string, string | number>) => string): string {
  if (sec >= 86400) return t('{d} дн {h} ч', { d: Math.floor(sec / 86400), h: Math.floor((sec % 86400) / 3600) })
  return qAge(sec, t)
}

// Программы одного демона supervisor или pm2 (helper workers-setup): с бедой наверху, у упавшей -
// последняя строка лога; 🔕 - не алертить по воркеру на день, неделю или постоянно
function WorkerList({ server, d, onChanged, onlyProblems }: {
  server: Server
  d: WorkerDaemon
  onChanged: () => void
  onlyProblems: boolean
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [picking, setPicking] = useState<string | null>(null)
  const [mBusy, setMBusy] = useState(false)
  const mute = async (key: string, hours: number) => {
    const kind = WORKER_MUTE + key
    setMBusy(true)
    try {
      if (hours < 0) {
        await updateServer(server.id, { alert_mutes: [...new Set([...(server.alert_mutes ?? []), kind])] })
      } else if (hours === 0) {
        if ((server.alert_mutes ?? []).includes(kind))
          await updateServer(server.id, { alert_mutes: (server.alert_mutes ?? []).filter((x) => x !== kind) })
        if (server.alert_snoozes?.[kind]) await snoozeServerAlert(server.id, kind, 0)
      } else await snoozeServerAlert(server.id, kind, hours)
      setPicking(null)
      onChanged()
    } catch {
      /* кнопка остается, можно повторить */
    } finally {
      setMBusy(false)
    }
  }
  const fmt = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const rank = (p: WorkerProg) => (p.mute ? 4 : p.level ? 2 - p.level : WORKER_IDLE.has(p.state) ? 3 : 2)
  const progs = d.progs
    .filter((p) => !onlyProblems || (p.level && !p.mute))
    .map((p, i) => ({ p, i }))
    .sort((a, b) => rank(a.p) - rank(b.p) || a.i - b.i)
    .map(({ p }) => p)
  const okN = d.progs.filter((p) => WORKER_OK.has(p.state)).length
  const badN = d.progs.filter((p) => p.level && !p.mute).length
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="hs-svc-title">
          {d.label}
          <span className="net-n">{d.progs.length}</span>
        </div>
      </div>
      <div className="net-sub mono">{d.where}</div>
      <div className="small muted">
        {subOf(
          t('работают: {n}', { n: okN }),
          badN > 0 && t('в беде: {n}', { n: badN }),
        )}
      </div>
      <div className="loc-results docker-clist">
        {progs.map((p) => {
          const tone = p.mute ? 't-up' : p.level ? (p.level >= 2 ? 't-down' : 't-degraded') : WORKER_IDLE.has(p.state) ? 't-soon' : 't-up'
          return (
            <div key={p.key} className={`loc-res docker-row ${tone}${p.mute ? ' q-muted' : ''}`}>
              <span className={`lk-dot${p.mute ? '' : DOT[tone]}`} />
              <div className="docker-c-main">
                <div className="docker-c-name mono">{p.name}</div>
                <div className="docker-c-img muted small">
                  {subOf(
                    // состояние словами - только когда его не сказали причина беды или время работы
                    !p.why && !(WORKER_OK.has(p.state) && p.up > 0) && t(WORKER_STATE[p.state] ?? p.state),
                    WORKER_OK.has(p.state) && p.up > 0 && t('работает {a}', { a: wAge(p.up, t) }),
                    p.exit != null && p.exit !== 0 && !WORKER_OK.has(p.state) && !p.why && t('код {n}', { n: p.exit }),
                    p.r60 > 0 && t('падений за час: {n}', { n: p.r60 }),
                    (p.p60 ?? 0) > 0 && t('плановых перезапусков за час: {n}', { n: p.p60 ?? 0 }),
                  )}
                </div>
                {p.why && (
                  <div className={`docker-c-img small${p.mute ? ' muted' : ' q-stuck'}`}>{p.why}</div>
                )}
                {p.log && (p.level || !WORKER_OK.has(p.state)) && (
                  <div className="docker-c-img w-log mono muted small" title={p.log}>{p.log}</div>
                )}
                {p.mute && (
                  <div className="small">
                    <span className="snooze-active-chip snooze-perm">
                      🔇 {p.mute.perm ? t('не алертит, постоянно') : t('не алертит {t}', { t: t('до {t}', { t: fmt(p.mute.until!) }) })}
                      {!isViewer && (
                        <button className="chip-x" disabled={mBusy} title={t('Снять')} onClick={() => mute(p.mkey ?? p.key, 0)}>
                          ✕
                        </button>
                      )}
                    </span>
                  </div>
                )}
                {!p.mute && picking === p.key && (
                  <div className="small q-mute-pick">
                    <span className="muted">🔕 {t('не алертить по этому воркеру:')}</span>
                    {[
                      { h: 24, l: t('1 день') },
                      { h: 24 * 7, l: t('1 неделя') },
                      { h: -1, l: t('постоянно') },
                    ].map(({ h, l }) => (
                      <button key={h} className="ghost small" disabled={mBusy} onClick={() => mute(p.mkey ?? p.key, h)}>
                        {l}
                      </button>
                    ))}
                    <button className="ghost small" disabled={mBusy} onClick={() => setPicking(null)}>
                      {t('Отмена')}
                    </button>
                  </div>
                )}
              </div>
              {!isViewer && !p.mute ? (
                <button className="ghost small q-mute-btn" disabled={mBusy} title={t('Не алертить по этому воркеру: на день, неделю или постоянно')}
                  onClick={() => setPicking(picking === p.key ? null : p.key)}>
                  🔕
                </button>
              ) : (
                <span className="q-mute-btn" />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// Задачи cron одного демона (хост, контейнер, под; helper cron-setup): с бедой и идущие наверху.
// У задачи расписание, последний запуск, запусков за сутки, обычная длительность и идущие копии;
// вывод, который доходит до cron без почты на сервере, выбрасывается. 🔕 - не алертить по задаче
function CronList({ server, where, kind, jobs, onChanged, filter }: {
  server: Server
  where: string
  kind: string
  jobs: CronJob[]
  onChanged: () => void
  filter: 'all' | 'run' | 'bad'
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [picking, setPicking] = useState<string | null>(null)
  const [mBusy, setMBusy] = useState(false)
  const mute = async (key: string, hours: number) => {
    const kind = CRON_MUTE + key
    setMBusy(true)
    try {
      if (hours < 0) {
        await updateServer(server.id, { alert_mutes: [...new Set([...(server.alert_mutes ?? []), kind])] })
      } else if (hours === 0) {
        if ((server.alert_mutes ?? []).includes(kind))
          await updateServer(server.id, { alert_mutes: (server.alert_mutes ?? []).filter((x) => x !== kind) })
        if (server.alert_snoozes?.[kind]) await snoozeServerAlert(server.id, kind, 0)
      } else await snoozeServerAlert(server.id, kind, hours)
      setPicking(null)
      onChanged()
    } catch {
      /* кнопка остается, можно повторить */
    } finally {
      setMBusy(false)
    }
  }
  const fmt = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const rank = (j: CronJob) => (j.mute ? 4 : j.level ? 2 - j.level : j.n > 0 ? 2 : 3)
  const rows = jobs
    .filter((j) => filter === 'all' || (filter === 'run' ? j.n > 0 : !!j.level && !j.mute))
    .map((j, i) => ({ j, i }))
    .sort((a, b) => rank(a.j) - rank(b.j) || a.i - b.i)
    .map(({ j }) => j)
  if (!rows.length) return null
  const runN = jobs.filter((j) => j.n > 0).length
  const badN = jobs.filter((j) => j.level && !j.mute).length
  const lost = (j: CronJob) =>
    j.order
      ? t('ошибки уходят в cron и выбрасываются: 2>&1 стоит перед перенаправлением в файл, а не после')
      : j.lost === 'err'
        ? t('ошибки (stderr) уходят в cron и выбрасываются: почты нет')
        : t('вывод уходит в cron и выбрасывается: почты нет')
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="net-head">
        <div className="hs-svc-title">
          {kind}
          <span className="net-n">{jobs.length}</span>
        </div>
      </div>
      <div className="net-sub mono">{where}</div>
      {(runN > 0 || badN > 0) && (
        <div className="small muted">
          {subOf(runN > 0 && t('идут сейчас: {n}', { n: runN }), badN > 0 && t('в беде: {n}', { n: badN }))}
        </div>
      )}
      <div className="loc-results docker-clist">
        {rows.map((j) => {
          const tone = j.mute ? 't-up' : j.level ? (j.level >= 2 ? 't-down' : 't-degraded') : j.n > 0 ? 't-up' : 't-soon'
          const oldest = j.run[0]?.age ?? 0
          const cpu = j.run.reduce((a, r) => a + r.cpu, 0)
          return (
            <div key={j.key} className={`loc-res docker-row ${tone}${j.mute ? ' q-muted' : ''}`}>
              <span className={`lk-dot${j.mute ? '' : DOT[tone]}`} />
              <div className="docker-c-main">
                <div className="docker-c-name mono cron-cmd" title={j.src ? `${j.src}\n${j.cmd}` : j.cmd}>{j.cmd}</div>
                <div className="docker-c-img muted small">
                  {subOf(
                    j.sched,
                    j.user && j.user !== 'root' && t('от {u}', { u: j.user }),
                    j.last > 0 && t('запуск {t}', { t: fmtRel(new Date(j.last * 1000).toISOString()) }),
                    j.runs > 0 && t('за сутки: {n}', { n: j.runs }),
                    j.usual === 1 ? t('обычно меньше минуты') : j.usual > 0 && t('обычно до {a}', { a: wAge(j.usual, t) }),
                  )}
                </div>
                {j.n > 0 && !j.why && (
                  <div className="docker-c-img small">
                    {j.n > 1
                      ? t('идут {n} копий, самая старая {a}', { n: j.n, a: wAge(oldest, t) })
                      : t('идет {a}', { a: wAge(oldest, t) })}
                    {cpu >= 60 && <span className="muted"> · {t('процессор {a}', { a: wAge(cpu, t) })}</span>}
                  </div>
                )}
                {j.why && <div className={`docker-c-img small${j.mute ? ' muted' : ' q-stuck'}`}>{j.why}</div>}
                {j.lost && <div className={`docker-c-img small${j.order ? ' cron-lost' : ' muted'}`}>{lost(j)}</div>}
                {j.mute && (
                  <div className="small">
                    <span className="snooze-active-chip snooze-perm">
                      🔇 {j.mute.perm ? t('не алертит, постоянно') : t('не алертит {t}', { t: t('до {t}', { t: fmt(j.mute.until!) }) })}
                      {!isViewer && (
                        <button className="chip-x" disabled={mBusy} title={t('Снять')} onClick={() => mute(j.key, 0)}>
                          ✕
                        </button>
                      )}
                    </span>
                  </div>
                )}
                {!j.mute && picking === j.key && (
                  <div className="small q-mute-pick">
                    <span className="muted">🔕 {t('не алертить по этой задаче:')}</span>
                    {[
                      { h: 24, l: t('1 день') },
                      { h: 24 * 7, l: t('1 неделя') },
                      { h: -1, l: t('постоянно') },
                    ].map(({ h, l }) => (
                      <button key={h} className="ghost small" disabled={mBusy} onClick={() => mute(j.key, h)}>
                        {l}
                      </button>
                    ))}
                    <button className="ghost small" disabled={mBusy} onClick={() => setPicking(null)}>
                      {t('Отмена')}
                    </button>
                  </div>
                )}
              </div>
              {!isViewer && !j.mute ? (
                <button className="ghost small q-mute-btn" disabled={mBusy} title={t('Не алертить по этой задаче: на день, неделю или постоянно')}
                  onClick={() => setPicking(picking === j.key ? null : j.key)}>
                  🔕
                </button>
              ) : (
                <span className="q-mute-btn" />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// Логи контейнеров и подов с ошибками за сутки (helper logerr-setup): всплеск наверху, у лога - за час
// из скольких строк, за сутки, обычный час и последняя строка с ошибкой. 🔕 - не алертить по логу
function LogErrList({ server, sources, onChanged, onlySpikes }: {
  server: Server
  sources: LogErrSource[]
  onChanged: () => void
  onlySpikes: boolean
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [picking, setPicking] = useState<string | null>(null)
  const [mBusy, setMBusy] = useState(false)
  const mute = async (key: string, hours: number) => {
    const kind = LOGERR_MUTE + key
    setMBusy(true)
    try {
      if (hours < 0) {
        await updateServer(server.id, { alert_mutes: [...new Set([...(server.alert_mutes ?? []), kind])] })
      } else if (hours === 0) {
        if ((server.alert_mutes ?? []).includes(kind))
          await updateServer(server.id, { alert_mutes: (server.alert_mutes ?? []).filter((x) => x !== kind) })
        if (server.alert_snoozes?.[kind]) await snoozeServerAlert(server.id, kind, 0)
      } else await snoozeServerAlert(server.id, kind, hours)
      setPicking(null)
      onChanged()
    } catch {
      /* кнопка остается, можно повторить */
    } finally {
      setMBusy(false)
    }
  }
  const fmt = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const rank = (x: LogErrSource) => (x.mute ? 3 : x.level ? 2 - x.level : 2)
  const rows = sources
    .filter((x) => !onlySpikes || (x.level && !x.mute))
    .map((x, i) => ({ x, i }))
    .sort((a, b) => rank(a.x) - rank(b.x) || a.i - b.i)
    .map(({ x }) => x)
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="loc-results docker-clist">
        {rows.map((x) => {
          const tone = x.mute ? 't-up' : x.level ? (x.level >= 2 ? 't-down' : 't-degraded') : 't-soon'
          return (
            <div key={x.key} className={`loc-res docker-row ${tone}${x.mute ? ' q-muted' : ''}`}>
              <span className={`lk-dot${x.mute ? '' : DOT[tone]}`} />
              <div className="docker-c-main">
                <div className="docker-c-name mono">{x.label}</div>
                <div className="docker-c-img muted small">
                  {subOf(
                    t('за час: {n}', { n: rpmText(x.h1) }) + (x.lines1 > 0 ? ' ' + t('из {m} строк', { m: rpmText(x.lines1) }) : ''),
                    t('за сутки: {n}', { n: rpmText(x.d1) }),
                    x.base != null && (x.base >= 1 ? t('обычно около {n} в час', { n: rpmText(Math.round(x.base)) }) : t('обычно почти нет')),
                  )}
                </div>
                {x.why && <div className={`docker-c-img small${x.mute ? ' muted' : ' q-stuck'}`}>{x.why}</div>}
                {x.last && (
                  <div className="docker-c-img w-log mono muted small" title={x.last}>
                    {x.last_ts > 0 && <span>{fmtRel(new Date(x.last_ts * 1000).toISOString())}: </span>}
                    {x.last}
                  </div>
                )}
                {x.mute && (
                  <div className="small">
                    <span className="snooze-active-chip snooze-perm">
                      🔇 {x.mute.perm ? t('не алертит, постоянно') : t('не алертит {t}', { t: t('до {t}', { t: fmt(x.mute.until!) }) })}
                      {!isViewer && (
                        <button className="chip-x" disabled={mBusy} title={t('Снять')} onClick={() => mute(x.key, 0)}>
                          ✕
                        </button>
                      )}
                    </span>
                  </div>
                )}
                {!x.mute && picking === x.key && (
                  <div className="small q-mute-pick">
                    <span className="muted">🔕 {t('не алертить о всплеске в этом логе:')}</span>
                    {[
                      { h: 24, l: t('1 день') },
                      { h: 24 * 7, l: t('1 неделя') },
                      { h: -1, l: t('постоянно') },
                    ].map(({ h, l }) => (
                      <button key={h} className="ghost small" disabled={mBusy} onClick={() => mute(x.key, h)}>
                        {l}
                      </button>
                    ))}
                    <button className="ghost small" disabled={mBusy} onClick={() => setPicking(null)}>
                      {t('Отмена')}
                    </button>
                  </div>
                )}
              </div>
              {!isViewer && !x.mute ? (
                <button className="ghost small q-mute-btn" disabled={mBusy} title={t('Не алертить о всплеске ошибок в этом логе: на день, неделю или постоянно')}
                  onClick={() => setPicking(picking === x.key ? null : x.key)}>
                  🔕
                </button>
              ) : (
                <span className="q-mute-btn" />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// Пулы PHP-FPM (helper fpm-setup): беда наверху, у пула - воркеры из предела, соединения и очередь
// сейчас, упоры, медленные и убитые запросы за час и сутки, последний пример. 🔕 - не алертить по пулу
function FpmList({ server, pools, onChanged, onlyProblems }: {
  server: Server
  pools: FpmPool[]
  onChanged: () => void
  onlyProblems: boolean
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [picking, setPicking] = useState<string | null>(null)
  const [mBusy, setMBusy] = useState(false)
  const mute = async (key: string, hours: number) => {
    const kind = FPM_MUTE + key
    setMBusy(true)
    try {
      if (hours < 0) {
        await updateServer(server.id, { alert_mutes: [...new Set([...(server.alert_mutes ?? []), kind])] })
      } else if (hours === 0) {
        if ((server.alert_mutes ?? []).includes(kind))
          await updateServer(server.id, { alert_mutes: (server.alert_mutes ?? []).filter((x) => x !== kind) })
        if (server.alert_snoozes?.[kind]) await snoozeServerAlert(server.id, kind, 0)
      } else await snoozeServerAlert(server.id, kind, hours)
      setPicking(null)
      onChanged()
    } catch {
      /* кнопка остается, можно повторить */
    } finally {
      setMBusy(false)
    }
  }
  const fmt = (ts: string) =>
    new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const rank = (x: FpmPool) => (x.mute ? 3 : x.level ? 2 - x.level : 2)
  const rows = pools
    .filter((x) => !onlyProblems || (x.level && !x.mute))
    .map((x, i) => ({ x, i }))
    .sort((a, b) => rank(a.x) - rank(b.x) || a.i - b.i)
    .map(({ x }) => x)
  const dh = (h: number, d: number) => t('за час {n}, за сутки {m}', { n: rpmText(h), m: rpmText(d) })
  return (
    <div className="chart-card net-card net-card-wide">
      <div className="loc-results docker-clist">
        {rows.map((x) => {
          const busy = x.reached_d > 0 || x.sat > 0
          const tone = x.mute ? 't-up' : x.level ? (x.level >= 2 ? 't-down' : 't-degraded') : busy || x.tout_d > 0 ? 't-soon' : 't-up'
          const ex = x.tout_last || x.slow_last
          const exTs = x.tout_last ? x.tout_ts : x.slow_ts
          return (
            <div key={x.key} className={`loc-res docker-row ${tone}${x.mute ? ' q-muted' : ''}`}>
              <span className={`lk-dot${x.mute ? '' : DOT[tone]}`} />
              <div className="docker-c-main">
                <div className="docker-c-name mono">{x.label}</div>
                <div className="docker-c-img muted small">
                  {subOf(
                    x.listen,
                    `pm = ${x.pm}`,
                    x.max > 0 && t('воркеров {n} из {m}', { n: x.children, m: x.max }),
                    x.conns > 0 && t('соединений сейчас: {n}', { n: x.conns }),
                    x.queue > 0 && t('в очереди: {n}', { n: x.queue }),
                  )}
                </div>
                {(busy || x.tout_d > 0 || x.slow_d > 0) && (
                  <div className="docker-c-img muted small">
                    {subOf(
                      x.sat > 0 && t('запросы ждали {n} мин за час', { n: x.sat }),
                      x.reached_d > 0 && `${t('упоры в pm.max_children')}: ${dh(x.reached, x.reached_d)}`,
                      x.tout_d > 0 && `${t('убито по таймауту')}: ${dh(x.tout, x.tout_d)}`,
                      x.slow_d > 0 && `${t('медленных')}${x.slowlog > 0 ? ` (${t('от {n} с', { n: x.slowlog })})` : ''}: ${dh(x.slow, x.slow_d)}`,
                    )}
                  </div>
                )}
                {x.why && <div className={`docker-c-img small${x.mute ? ' muted' : ' q-stuck'}`}>{x.why}</div>}
                {x.max === 5 && busy && (
                  <div className="docker-c-img muted small">{t('pm.max_children = 5 - значение по умолчанию: пул никто не настраивал')}</div>
                )}
                {ex && (
                  <div className="docker-c-img w-log mono muted small" title={ex}>
                    {exTs > 0 && <span>{fmtRel(new Date(exTs * 1000).toISOString())}: </span>}
                    {ex}
                  </div>
                )}
                {x.mute && (
                  <div className="small">
                    <span className="snooze-active-chip snooze-perm">
                      🔇 {x.mute.perm ? t('не алертит, постоянно') : t('не алертит {t}', { t: t('до {t}', { t: fmt(x.mute.until!) }) })}
                      {!isViewer && (
                        <button className="chip-x" disabled={mBusy} title={t('Снять')} onClick={() => mute(x.key, 0)}>
                          ✕
                        </button>
                      )}
                    </span>
                  </div>
                )}
                {!x.mute && picking === x.key && (
                  <div className="small q-mute-pick">
                    <span className="muted">🔕 {t('не алертить об этом пуле:')}</span>
                    {[
                      { h: 24, l: t('1 день') },
                      { h: 24 * 7, l: t('1 неделя') },
                      { h: -1, l: t('постоянно') },
                    ].map(({ h, l }) => (
                      <button key={h} className="ghost small" disabled={mBusy} onClick={() => mute(x.key, h)}>
                        {l}
                      </button>
                    ))}
                    <button className="ghost small" disabled={mBusy} onClick={() => setPicking(null)}>
                      {t('Отмена')}
                    </button>
                  </div>
                )}
              </div>
              {!isViewer && !x.mute ? (
                <button className="ghost small q-mute-btn" disabled={mBusy} title={t('Не алертить о пуле PHP-FPM: на день, неделю или постоянно')}
                  onClick={() => setPicking(picking === x.key ? null : x.key)}>
                  🔕
                </button>
              ) : (
                <span className="q-mute-btn" />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

const SVC_SECTIONS: SecDef[] = [
  { id: 'overview', label: 'Сводка', group: '' },
  { id: 'web', label: 'Веб-серверы', group: 'Веб' },
  { id: 'sites', label: 'Сайты', group: 'Веб' },
  { id: 'certs', label: 'Сертификаты', group: 'Веб' },
  { id: 'fpm', label: 'PHP-FPM', group: 'Веб' },
  { id: 'db', label: 'Базы и хранилища', group: 'Данные' },
  { id: 'queues', label: 'Очереди', group: 'Данные' },
  { id: 'workers', label: 'Воркеры', group: 'Процессы' },
  { id: 'cron', label: 'Кроны', group: 'Процессы' },
  { id: 'logs', label: 'Ошибки в логах', group: 'Процессы' },
  { id: 'llm', label: 'LLM', group: 'Модели' },
]
const SVC_SEC_IDS = new Set(SVC_SECTIONS.map((x) => x.id))
// раздел карточки по виду проблемы, если бэкенд его не назвал (старый бэкенд в кэше вкладки)
const SVC_SEC_OF_KIND: Record<string, string> = {
  queue: 'queues', queue_stuck: 'queues', workers: 'workers', cron_stuck: 'cron', log_errors: 'logs', fpm: 'fpm', web_cert: 'certs', web_config: 'web', db_health: 'db', db_conn: 'db', llm: 'llm',
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
  const dbItems = srv.items.filter((it) => !it.web && !it.svc && !it.llm && !it.workers && !it.cron).sort((a, b) => Number(isDb(b)) - Number(isDb(a)))
  const llm = s.llm ?? []
  const llmBad = llm.filter((x) => !x.ok && !x.no_metrics).length
  const dbs = dbItems.flatMap((it) => it.db ?? [])
  const dbRows = dbs.flatMap((d) => (d.engine === 'redis' ? [] : d.dbs ?? []))
  // брокеры очередей: RabbitMQ (агент), очереди в Redis и в таблицах баз (helper queues-setup)
  const rabbit = srv.items.filter((it) => !!it.svc)
  const qAll = rabbit.flatMap((it) => it.svc?.queues ?? [])
  // воркеры под supervisor и pm2: демоны с бедой наверху, фильтр "с проблемами"
  const wDaemons = s.workers ?? []
  const wProgs = wDaemons.flatMap((d) => d.progs)
  const wStopped = wProgs.filter((p) => WORKER_IDLE.has(p.state)).length
  const [wOnly, setWOnly] = useState(false)
  const wProbN = wProgs.filter((p) => p.level && !p.mute).length
  const wRank = (d: WorkerDaemon) => Math.min(9, ...d.progs.map((p) => (p.mute ? 9 : p.level ? 3 - p.level : 9)))
  const wShown = wDaemons
    .map((d, i) => ({ d, i, r: wRank(d) }))
    .filter(({ r }) => !wOnly || r < 9)
    .sort((a, b) => a.r - b.r || a.i - b.i)
    .map(({ d }) => d)
  // кроны: задачи по демонам (хост, контейнеры), фильтр "идут сейчас" и "с проблемами"
  const cron = s.cron
  const cJobs = cron?.jobs ?? []
  const [cFilter, setCFilter] = useState<'all' | 'run' | 'bad'>('all')
  const cRunN = cJobs.filter((j) => j.n > 0).length
  const cProbN = cJobs.filter((j) => j.level && !j.mute).length
  const cWhere = [...new Set(cJobs.map((j) => j.where))]
  const cRank = (w: string) => Math.min(9, ...cJobs.filter((j) => j.where === w).map((j) => (j.mute ? 9 : j.level ? 3 - j.level : j.n > 0 ? 5 : 9)))
  const cGroups = cWhere.map((w, i) => ({ w, i, r: cRank(w) })).sort((a, b) => a.r - b.r || a.i - b.i).map(({ w }) => w)
  const cNoMta = (cron?.daemons ?? []).filter((d) => !d.mta && d.jobs > 0 && d.kind !== 'supercronic').map((d) => d.where)
  // ошибки в логах: всплески наверху, фильтр "со всплеском"
  const lErr = s.log_errors
  const lSrc = lErr?.sources ?? []
  const [lOnly, setLOnly] = useState(false)
  const lSpikeN = lSrc.filter((x) => x.level && !x.mute).length
  // пулы PHP-FPM: беда наверху, фильтр "с проблемами"
  const fpm = s.fpm
  const fPools = fpm?.pools ?? []
  const [fOnly, setFOnly] = useState(false)
  const fProbN = fPools.filter((x) => x.level && !x.mute).length
  const dbNoAuth = (s.queue_db_skipped ?? []).filter((x) => x.why === 'auth')
  const dbBig = (s.queue_db_skipped ?? []).filter((x) => x.why === 'big')
  const qHot = qAll.filter((x) => qDepth(x) >= QUEUE_HOT).length
  // брокеры с бедой - наверх; фильтр "с проблемами" прячет остальное
  const [qOnly, setQOnly] = useState(false)
  const qProbs = (it: SvcItem) => [
    ...(it.svc?.failed?.spike && !s.queue_fail_mutes?.[it.svc.source || ''] ? [0] : []),
    ...((it.svc?.queues ?? []).map((x) => queueRank(s, it.svc!, x)).filter((r) => r != null) as number[]),
  ]
  const qProbN = rabbit.reduce((n, it) => n + qProbs(it).length, 0)
  const brokers = rabbit
    .map((it, i) => ({ it, i, p: qProbs(it) }))
    .filter(({ p }) => !qOnly || p.length > 0)
    .sort((a, b) => (a.p.length ? Math.min(...a.p) : 9) - (b.p.length ? Math.min(...b.p) : 9) || b.p.length - a.p.length || a.i - b.i)
    .map(({ it }) => it)
  const certs = (s.web_certs ?? []).slice().sort((a, b) => a.days - b.days)
  const cfg = s.web_config ?? []

  const shown: Record<string, boolean> = {
    overview: true,
    web: web.length > 0,
    sites: siteList.length > 0,
    certs: certs.length > 0,
    db: dbItems.length > 0,
    queues: rabbit.length > 0,
    workers: wDaemons.length > 0 || (s.workers_skipped ?? []).length > 0,
    cron: cJobs.length > 0,
    logs: lSrc.length > 0,
    fpm: fPools.length > 0,
    llm: llm.length > 0,
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
    llm: subOf(
      [...new Set(llm.map((x) => x.engine_name))].join(', '),
      llm.some((x) => x.gen_tps != null) && t('{n} ток/с', { n: rpmText(llm.reduce((a, x) => a + (x.gen_tps ?? 0), 0)) }),
      llm.some((x) => x.waiting != null) && t('в очереди {n}', { n: rpmText(llm.reduce((a, x) => a + (x.waiting ?? 0), 0)) }),
    ),
    workers: subOf(
      [...new Set(wDaemons.map((d) => d.label))].join(', '),
      t('программ: {n}', { n: wProgs.length }),
      wStopped > 0 && t('остановлены руками: {n}', { n: wStopped }),
    ),
    cron: subOf(
      t('задач: {n}', { n: cJobs.length }),
      cRunN > 0 && t('идут сейчас: {n}', { n: cRunN }),
    ),
    fpm: subOf(
      t('пулов: {n}', { n: fPools.length }),
      t('воркеров {n} из {m}', { n: fPools.reduce((a, x) => a + x.children, 0), m: fPools.reduce((a, x) => a + x.max, 0) }),
    ),
    logs: subOf(
      t('логов с ошибками: {n}', { n: lSrc.length }),
      t('за час: {n}', { n: rpmText(lSrc.reduce((a, x) => a + x.h1, 0)) }),
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
                  {llm.length > 0 && (
                    <button className="srv-tile-btn" onClick={() => go('llm')} title={t('к разделу')}>
                      <NetTile
                        label={tileLabel('llm', 'LLM')}
                        value={String(llm.length)}
                        sub={llmBad
                          ? t('не отвечают: {n}', { n: llmBad })
                          : subOf(
                              llm.some((x) => x.gen_tps != null) && t('{n} ток/с', { n: rpmText(llm.reduce((a, x) => a + (x.gen_tps ?? 0), 0)) }),
                              llm.some((x) => x.running != null) && t('в работе {n}', { n: rpmText(llm.reduce((a, x) => a + (x.running ?? 0), 0)) }),
                            ) || undefined}
                        tone={tileTone('llm', llmBad ? 'bad' : 'ok')}
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

            {cur.id === 'llm' && <LlmSection server={s} rows={items.llm?.map((x) => ({
              tone: x.level >= 2 ? ('t-down' as const) : ('t-degraded' as const), text: x.text, since: x.since,
            })) ?? []} />}

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

            {cur.id === 'workers' && (
              <div className="srv-sec">
                <div className="muted small">
                  {t('Программы под supervisor и pm2. Алерт, когда воркер не запускается (supervisor или pm2 перестал его поднимать, или три минуты не может запуститься), завершился с ошибкой или падает и поднимается: три перезапуска за 15 минут. Остановленные руками не судятся.')}
                </div>
                {(s.workers_skipped ?? []).length > 0 && (
                  <div className="muted small">
                    {t('Не видны программы этих демонов: {list}.', { list: (s.workers_skipped ?? []).map((x) => `${x.where} - ${x.why}`).join('; ') })}
                  </div>
                )}
                {wProbN > 0 && wDaemons.length > 1 && (
                  <div className="win-switch q-filter">
                    <button className={`win-btn${!wOnly ? ' win-btn-active' : ''}`} onClick={() => setWOnly(false)}>
                      {t('Все воркеры')}
                    </button>
                    <button className={`win-btn${wOnly ? ' win-btn-active' : ''}`} onClick={() => setWOnly(true)}>
                      {t('С проблемами: {n}', { n: wProbN })}
                    </button>
                  </div>
                )}
                {wShown.map((d) => (
                  <WorkerList key={d.source} server={s} d={d} onChanged={onChanged} onlyProblems={wOnly} />
                ))}
              </div>
            )}
            {cur.id === 'fpm' && fpm && (
              <div className="srv-sec">
                <div className="muted small">
                  {t('Пулы PHP-FPM: сколько воркеров занято и сколько запросов ждет, упоры в pm.max_children, медленные и убитые по таймауту запросы из лога FPM. Алерт, когда пул не справляется подолгу: запросы ждали воркера от 15 минут из часа, пул упирался в предел от 60 раз за час или по таймауту убито от 20 запросов за час.')}
                </div>
                {fpm.skipped.length > 0 && (
                  <div className="muted small">{t('Конфиг не найден: {w}', { w: fpm.skipped.map((x) => x.where).join(', ') })}</div>
                )}
                {fProbN > 0 && (
                  <div className="win-switch q-filter">
                    <button className={`win-btn${!fOnly ? ' win-btn-active' : ''}`} onClick={() => setFOnly(false)}>
                      {t('Все пулы')}
                    </button>
                    <button className={`win-btn${fOnly ? ' win-btn-active' : ''}`} onClick={() => setFOnly(true)}>
                      {t('С проблемами: {n}', { n: fProbN })}
                    </button>
                  </div>
                )}
                <FpmList server={s} pools={fPools} onChanged={onChanged} onlyProblems={fOnly} />
              </div>
            )}
            {cur.id === 'logs' && lErr && (
              <div className="srv-sec">
                <div className="muted small">
                  {t('Строки с ошибками в логах контейнеров и подов: ERROR, FATAL, Exception, трейсбеки, [error] nginx, level=error. Алерт только на всплеск: за час от 200 строк и в пять раз больше обычного часа этого же лога; пока истории меньше 4 часов - от 20 000. Лог, где ошибки есть всегда, не алертит, строки стека за ошибки не считаются.')}
                </div>
                {lErr.skipped > 0 && (
                  <div className="muted small">{t('Логов пропущено за последнюю минуту: {n} (за минуту прочитано больше 64 МБ).', { n: lErr.skipped })}</div>
                )}
                {lSpikeN > 0 && (
                  <div className="win-switch q-filter">
                    <button className={`win-btn${!lOnly ? ' win-btn-active' : ''}`} onClick={() => setLOnly(false)}>
                      {t('Все логи')}
                    </button>
                    <button className={`win-btn${lOnly ? ' win-btn-active' : ''}`} onClick={() => setLOnly(true)}>
                      {t('Со всплеском: {n}', { n: lSpikeN })}
                    </button>
                  </div>
                )}
                <LogErrList server={s} sources={lSrc} onChanged={onChanged} onlySpikes={lOnly} />
              </div>
            )}
            {cur.id === 'cron' && cron && (
              <div className="srv-sec">
                <div className="muted small">
                  {t('Задачи cron на хосте и в контейнерах. Алерт, когда задача наслаивается (новая копия запустилась, пока старая не кончилась, и обе идут дольше 15 минут) или висит в разы дольше обычного: обычное - самая долгая из прошлых копий, без истории - 6 часов. Копии с детьми и их процессор видны в строке.')}
                </div>
                {cNoMta.length > 0 && (
                  <div className="muted small">
                    {cron.disc24 > 0
                      ? t('Почты нет ({w}): что задачи выводят, cron выбрасывает, за сутки {n} раз. Перенаправьте вывод в лог: >> /var/log/x.log 2>&1.', { w: cNoMta.join(', '), n: cron.disc24 })
                      : t('Почты нет ({w}): что задачи выводят, cron выбрасывает. Перенаправьте вывод в лог: >> /var/log/x.log 2>&1.', { w: cNoMta.join(', ') })}
                  </div>
                )}
                {(cRunN > 0 || cProbN > 0) && (
                  <div className="win-switch q-filter">
                    <button className={`win-btn${cFilter === 'all' ? ' win-btn-active' : ''}`} onClick={() => setCFilter('all')}>
                      {t('Все задачи')}
                    </button>
                    {cRunN > 0 && (
                      <button className={`win-btn${cFilter === 'run' ? ' win-btn-active' : ''}`} onClick={() => setCFilter('run')}>
                        {t('Идут сейчас: {n}', { n: cRunN })}
                      </button>
                    )}
                    {cProbN > 0 && (
                      <button className={`win-btn${cFilter === 'bad' ? ' win-btn-active' : ''}`} onClick={() => setCFilter('bad')}>
                        {t('С проблемами: {n}', { n: cProbN })}
                      </button>
                    )}
                  </div>
                )}
                {cGroups.map((w) => {
                  const js = cJobs.filter((j) => j.where === w)
                  return <CronList key={w} server={s} where={w} kind={js[0]?.kind || 'cron'} jobs={js} onChanged={onChanged} filter={cFilter} />
                })}
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
                <div className="muted small">
                  {t('Без порога панель сама алертит очередь от сотни сообщений, которая полчаса растет и не разбирается, стоит больше часа или осталась без потребителей. Упавшие задачи алертят только всплеском: от сотни за час и в пять раз больше обычного. Очереди мертвых писем (dlq) не судятся.')}
                </div>
                {(s.queue_skipped ?? []).length > 0 && (
                  <div className="muted small">
                    {t('Очереди в этих Redis не видны: helper не нашел пароль ({list}). Он берет REDIS_PASSWORD или --requirepass из настроек контейнера либо requirepass из redis.conf.', { list: (s.queue_skipped ?? []).join(', ') })}
                  </div>
                )}
                {dbNoAuth.length > 0 && (
                  <div className="muted small">
                    {t('Таблицы очередей в этих базах не проверены: helper не смог войти ({list}). PostgreSQL он открывает через сокет под пользователем сервера или по POSTGRES_PASSWORD из настроек контейнера, MySQL - через debian.cnf или по MYSQL_ROOT_PASSWORD.', { list: dbNoAuth.map((x) => x.where).join('; ') })}
                  </div>
                )}
                {dbBig.length > 0 && (
                  <div className="muted small">
                    {t('Не считаются каждую минуту: в таблице очередей больше миллиона строк, выполненные задачи из нее не удаляются ({list}).', { list: dbBig.map((x) => t('{w}: {n} строк', { w: x.where, n: rpmText(x.rows ?? 0) })).join('; ') })}
                  </div>
                )}
                {qProbN > 0 && (rabbit.length > 1 || qAll.length > 8) && (
                  <div className="win-switch q-filter">
                    <button className={`win-btn${!qOnly ? ' win-btn-active' : ''}`} onClick={() => setQOnly(false)}>
                      {t('Все очереди')}
                    </button>
                    <button className={`win-btn${qOnly ? ' win-btn-active' : ''}`} onClick={() => setQOnly(true)}>
                      {t('С проблемами: {n}', { n: qProbN })}
                    </button>
                  </div>
                )}
                {brokers.map((it) => (
                  <QueueList key={(it.svc?.source ?? '') + (it.svc?.kind ?? '')} server={s} svc={it.svc!} busy={busy} save={save}
                    onChanged={onChanged} onlyProblems={qOnly} />
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

// плитки сводки как фильтр: какие сервисы ноды попадают под плитку. Эти же условия считают
// числа плиток, поэтому чипы отобранных строк в сумме дают число плитки
type SFilter = 'db' | 'web' | 'queues'
const isDbItem = (it: SvcItem) => !it.web && !it.svc && !it.llm && !it.workers && !it.cron
const SVC_OF: Record<SFilter, (it: SvcItem) => boolean> = {
  db: isDbItem,
  web: (it) => !!it.web,
  queues: (it) => !!it.svc,
}

// что из нажатой плитки нашлось на ноде сверх чипов сервисов: очереди и сайты
function svcHit(f: SFilter, r: SrvServices, ctx: SitesCtx, t: TFn): string | null {
  const list = (xs: string[]) => ({ list: shortList(xs, t, 8) })
  if (f === 'queues') {
    const svc = r.items.filter(SVC_OF.queues)
    const hot = svc.flatMap((it) => it.svc?.queues ?? []).filter((x) => qDepth(x) >= QUEUE_HOT).map((x) => x.name)
    return subOf(
      t('очередей: {n}', { n: svc.reduce((n, it) => n + (it.svc?.total ?? 0), 0) }),
      hot.length > 0 && t('переполнены: {list}', list(hot)),
    )
  }
  if (f === 'web') {
    const s = r.server
    const miss = missingSites([...new Set(r.items.flatMap((it) => it.sites ?? []))], ctx)
    const cfg = (s.web_config ?? []).filter((c) => !c.ok).map((c) => c.where || c.server)
    const certs = (s.web_certs ?? []).filter((c) => c.days <= EXPIRY_SHOW_DAYS).map((c) => c.label)
    return subOf(
      t('сайтов: {n}', { n: r.items.filter(SVC_OF.web).reduce((n, it) => n + (it.sites?.length ?? 0), 0) }),
      ctx.hosts != null && miss > 0 && t('без монитора: {n}', { n: miss }),
      cfg.length > 0 && s.online && t('конфиг с ошибкой: {list}', list(cfg)),
      certs.length > 0 && t('сертификаты истекают: {list}', list(certs)),
    )
  }
  return null
}

// Строка ноды: иконки ее сервисов (сразу видно, что где крутится) и чипы бед - переполненные
// очереди, конфиг веб-сервера с ошибкой, проблемы баз. Справа число сервисов, цвет по худшему
function HostRow({ srv, showGroup, only, hint, onOpen }: {
  srv: SrvServices
  showGroup?: boolean
  only?: (it: SvcItem) => boolean // нажата плитка: чипы и счетчик - только ее сервисы
  hint?: string | null
  onOpen: () => void
}) {
  const { t } = useI18n()
  const s = srv.server
  const items = only ? srv.items.filter(only) : srv.items
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
            <span className="type-chip off" title={t('переполненные очереди')}>📨 {hot}</span>
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
          {/* одинаковые сервисы на ноде (два LiteLLM, три TEI) - один чип со счетчиком, где - в подсказке */}
          {[...new Set(items.map((it) => it.kind))].map((kind) => {
            const xs = items.filter((it) => it.kind === kind)
            const where = xs.map((x) => x.where).filter(Boolean).join('\n')
            return (
              <span key={kind} className="svc-chip" title={where ? `${kind}\n${where}` : kind}>
                <span className="svc-ico"><EngineIcon name={kind} /></span> {kind}
                {xs.length > 1 && <span className="muted"> ({xs.length})</span>}
              </span>
            )
          })}
        </div>
        {hint && <div className="hs-host-hit">{hint}</div>}
      </div>
      <div className={`hs-host-count mono ${tone}`}>
        {items.length}<span className="muted"> {t('серв.')}</span>
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
  const { tile, setTile, tileProps } = useTileFilter<SFilter>()
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
  // ноды только с кроном - для окна по ссылке, в списке нод их нет
  const withCron = buildByServer(servers, t)
  const all = withCron.filter((r) => r.items.length > 0)
  // Ищем и по ноде, и по СЕРВИСУ: на этой странице чаще спрашивают «где у меня redis»,
  // а не «что на ноде X» — поиск только по именам нод отвечал бы на другой вопрос.
  const q = query.trim().toLowerCase()
  // нажатая плитка оставляет ноды с ее сервисами (очереди - как в плитке, только на связи)
  const rows = (q
    ? all.filter(({ server, items }) =>
        [server.name, server.group_name, ...items.map((i) => i.kind)]
          .join(' ')
          .toLowerCase()
          .includes(q),
      )
    : all
  ).filter((r) => !tile || ((tile !== 'queues' || r.server.online) && r.items.some(SVC_OF[tile])))
  const open = withCron.find((r) => r.server.id === openId)
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
  const dbItems = svcAll.filter(({ it }) => isDbItem(it))
  const dbBad = live.filter((r) => (r.server.db_health ?? []).some((h) => h.problems.some((p) => p.level >= 2))).map((r) => r.server.name)
  const dbWarn = live.filter((r) => (r.server.db_health ?? []).some((h) => h.problems.some((p) => p.level < 2))).map((r) => r.server.name)
  const webItems = svcAll.filter(({ it }) => it.web)
  const siteN = webItems.reduce((n, { it }) => n + (it.sites?.length ?? 0), 0)
  const missN = all.reduce((n, r) => n + missingSites([...new Set(r.items.flatMap((it) => it.sites ?? []))], sitesCtx), 0)
  const cfgBad = live.filter((r) => (r.server.web_config ?? []).some((c) => !c.ok)).map((r) => r.server.name)
  // сертификаты ближе месяца - видно; алертный порог (неделя, день) - цветом плитки. Срок не
  // устаревает, поэтому по всем нодам
  const certs = all.flatMap((r) => (r.server.web_certs ?? []).filter((c) => c.days <= EXPIRY_SHOW_DAYS))
  const rabbit = svcAll.filter(({ it, s }) => !!it.svc && s.online)
  const qAll = rabbit.flatMap(({ it }) => it.svc?.queues ?? [])
  const qHot = qAll.filter((x) => qDepth(x) >= QUEUE_HOT)
  // нажатая плитка: подпись чипа над списком и что сказать, если под нее ничего не попало
  const filterLabel: Record<SFilter, string> = { db: t('базы данных'), web: t('веб-серверы'), queues: t('очереди') }
  const filterEmpty: Record<SFilter, string> = {
    db: t('Баз данных не найдено.'), web: t('Веб-серверов не найдено.'), queues: t('Очередей нет.'),
  }

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
            {...tileProps(null)}
          />
          <NetTile
            label={t('Базы данных')}
            value={String(dbItems.length)}
            sub={live.length ? subOf(
              dbBad.length > 0 && t('проблемы: {list}', { list: names(dbBad) }),
              dbWarn.length > 0 && t('предупреждения: {list}', { list: names(dbWarn) }),
            ) || t('все в порядке') : t('свежих данных нет')}
            tone={!live.length ? undefined : dbBad.length ? 'bad' : dbWarn.length ? 'warn' : 'ok'}
            {...tileProps('db')}
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
            {...tileProps('web')}
          />
          {rabbit.length > 0 && (
            <NetTile
              label={t('Очереди')}
              value={String(rabbit.reduce((n, { it }) => n + (it.svc?.total ?? 0), 0))}
              sub={subOf(
                t('сообщений: {m}', { m: rpmText(qAll.reduce((a, x) => a + qDepth(x), 0)) }),
                qHot.length > 0 && t('переполнены: {list}', { list: names(qHot.map((x) => x.name)) }),
              )}
              tone={qHot.length ? 'warn' : 'ok'}
              {...tileProps('queues')}
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
      {tile && <TileFilterChip label={filterLabel[tile]} onClear={() => setTile(null)} />}
      {all.length === 0 ? (
        <div className="card muted">
          {t('Сервисы не обнаружены. Панель находит их по контейнерам, подам и процессам — обновите агенты.')}
        </div>
      ) : rows.length === 0 ? (
        <div className="card muted">
          {tile && !q ? filterEmpty[tile] : <>
            {t('По запросу «{q}» ничего нет.', { q: query.trim() })}
            <button className="ghost search-clear" onClick={() => setQuery('')}>{t('Сбросить')}</button>
          </>}
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
                  <HostRow key={r.server.id} srv={r} showGroup={groupBy !== 'group'} onOpen={() => setOpenId(r.server.id)}
                    only={tile ? SVC_OF[tile] : undefined} hint={tile ? svcHit(tile, r, sitesCtx, t) : null} />
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
