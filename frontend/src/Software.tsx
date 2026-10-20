import { Fragment, useCallback, useEffect, useState } from 'react'
import { getSoftware, updateServer, type Server, type SoftwareItem, type SoftwareOut, type SwFixBlock } from './api'
import { useAuth } from './auth'
import { FixSteps, useFixDone } from './FixSteps'
import { useI18n } from './i18n'
import { swDecisionLabel } from './mutes'

// Версии ПО (app/software.py): что стоит в контейнерах и на хосте и когда у этого кончается
// поддержка по снимку endoflife.date: все найденное по продукту и циклу (без поддержки сверху),
// образы без версии в теге и образы, которые больше года не пересобирались. Строки sw_advice
// здесь не повторяются: они на сводке и на главной, а здесь то же самое видно по списку.
// У кончившихся и скорых - как исправить (команды под ноду собирает бэкенд, тексты шагов здесь по
// id) и решение: "обновить нельзя" или "отложить до конца поддержки" (swver: в alert_mutes), такой
// пункт уходит с главной и из счетчика раздела, а здесь приглушен с кнопкой "вернуть". У способа
// шапка - что это за ПО (контейнер, образ, папка compose и сервис), шаги нумерованы, после них откат
// и другой путь "контейнер не нужен"

type TFn = (k: string, p?: Record<string, string | number>) => string
const WHERE_MAX = 6

// Шаг исправления одной фразой: что он делает; команды под ноду - с бэкенда.
// a - подстановки способа (продукт, from, to, контейнер, образ, папка compose, сервис, пакеты)
function swFixText(id: string, a: Record<string, string>, t: TFn): string {
  const p = { ...a, env: a.env || a.image || '' }
  switch (id) {
    case 'runtime_major': return t('{name} {from} и {to} - разные основные версии: код под {from} скорее всего сразу не заведется, сначала проверить на копии.', p)
    case 'hint_runtime': return t('Код приложения сначала проверить на {name} {to}, лучше на копии.', p)
    case 'major': return t('{name} {to} - новая основная версия: сначала прочитать, что в ней поменялось (upgrade guide).', p)
    case 'hint_grafana': return t('Grafana при запуске переводит свою базу на новую схему: обратно только из копии данных.', p)
    case 'hint_traefik': return t('В Traefik 3 часть правил и меток пишется иначе: сначала руководство по миграции с v2.', p)
    case 'hint_redis': return t('Новый Redis читает старые данные, обратно только из копии. С 7.4 лицензия не BSD, замена на тех же данных - Valkey.', p)
    case 'hint_valkey': return t('Новый Valkey читает старые данные, обратно только из копии.', p)
    case 'hint_os': return t('Это базовый образ ОС: пакеты, которые ставятся при запуске, в {name} {to} могут называться иначе.', p)
    case 'no_target': return t('Поддерживаемой версии {name} новее {from} в таблице сроков нет: выбрать по сайту вендора.', p)
    case 'c_find': return t('Узнать папку, compose-файл и сервис контейнера {c}, дальше все в этом же окне. Пусто - контейнер запущен не из compose.', p)
    case 'c_bak': return t('Копия compose-файла для отката.', p)
    case 'c_data': return t('Копия данных контейнера для отката, на это время он остановлен.', p)
    case 'img_set': return t('Поменять образ на {new}. Последняя строка скажет, вышло ли: НЕ ЗАМЕНЕНО - строка image в compose другая (образ из переменной или .env), ее поправить руками, grep выше показывает все строки image.', p)
    case 'c_up': return t('Перезапустить и проверить: в логе нет ошибок, а последняя строка скажет, на новом ли образе контейнер (Up в ps этого не говорит). НЕ ОБНОВЛЕН - образ в compose не заменен или такого тега нет (up скажет not found), прежний контейнер продолжит работать.', p)
    case 'c_undo': return t('Вернуть прежний compose-файл и перезапустить.', p)
    case 'c_undo_data': return t('Вернуть прежний compose-файл и данные из копии, перезапустить.', p)
    case 'c_down': return t('Контейнер не нужен - остановить и убрать проект. Данные в папках и томах остаются на диске.', p)
    case 'c_rm': return t('Контейнер не нужен - убрать только его, остальные сервисы проекта работают. Данные остаются на диске. Сервис убрать и из compose-файла, иначе он вернется при следующем up.', p)
    case 'run_down': return t('Контейнер не нужен - остановить и удалить. Данные в папках и томах остаются на диске.', p)
    case 'run_pull': return t('Скачать новый образ.', p)
    case 'run_recreate': return t('Контейнер {c} запущен не из compose: пересоздать его тем же docker run, что и раньше, но с новым образом.', p)
    case 'run_rebuild': return t('Контейнер {c} запущен не из compose, образ собран на этой ноде ({env}): поменять FROM в Dockerfile на {name} {to}, пересобрать и пересоздать контейнер тем же docker run.', p)
    case 'pull_keep': return t('Сохранить текущий образ под тегом kervax-old, для отката.', p)
    case 'pull_up': return t('Скачать свежий образ, перезапустить и проверить ps и лог. Версия не поменялась - автор образа его еще не пересобрал.', p)
    case 'pull_undo': return t('Вернуть прежний образ и перезапустить.', p)
    case 'pull_undo_data': return t('Вернуть прежний образ и данные из копии, перезапустить.', p)
    case 'rb_from': return t('Версия пришла из базового образа ({env}): найти FROM в Dockerfile и поменять на {name} {to}.', p)
    case 'rb_build': return t('Пересобрать со свежей базой, перезапустить и проверить ps и лог.', p)
    case 'rb_undo': return t('Вернуть прежний FROM и пересобрать так же.', p)
    case 'c_ci': return t('Образ {image} собирают не здесь ({env}): поменять FROM на {name} {to} там, где его собирают, и выкатить как обычно.', p)
    case 'c_dangling': return t('Имя образа уже перевешено на другую сборку: пересоздать контейнер (docker compose up -d). Версия не поменялась - пересобрать образ на {name} {to}.', p)
    case 'db_dump_pg': return t('Дамп всех баз, база при этом работает. Пустой файл - дамп не вышел, проверить пользователя в -U.', p)
    case 'db_copy': return t('Копия данных для отката, база на это время остановлена.', p)
    case 'db_inplace': return t('Команды смены тега нет намеренно: после копии поменять тег на {to} в compose руками, up -d и смотреть лог до конца перевода данных.', p)
    case 'path_postgresql': return t('PostgreSQL {to} не прочитает данные {from}: тег просто так не менять. Путь: рядом новый контейнер {to} с пустой папкой данных, в него дамп, проверка, переключение приложения. Расширения нужны и в новом образе.', p)
    case 'path_mysql': return t('MySQL обновляется на месте, но по одной LTS за раз (5.7, 8.0, 8.4), совместимость проверит MySQL Shell: util.checkForServerUpgrade(). Обратно только из копии.', p)
    case 'path_mariadb': return t('MariaDB обновляется на месте, после запуска нужен mariadb-upgrade (образ делает его сам при MARIADB_AUTO_UPGRADE=1). Обратно только из копии.', p)
    case 'path_mongodb': return t('MongoDB - по одной основной версии за раз (5.0, 6.0, 7.0, 8.0), перед каждой setFeatureCompatibilityVersion. Обратно только из копии.', p)
    case 'path_elasticsearch': return t('Elasticsearch - через последний выпуск своей основной версии (7.17, 8.19), что мешает, покажет Upgrade Assistant. Обратно только из снапшота.', p)
    case 'path_clickhouse': return t('ClickHouse обновляется на месте, лучше на LTS (X.3, X.8), сначала прочитать Backward Incompatible Changes в changelog. Обратно только из копии.', p)
    case 'path_rabbitmq': return t('RabbitMQ - через промежуточные версии по upgrade path, перед каждой rabbitmqctl enable_feature_flag all. Обратно только из копии.', p)
    case 'path_zookeeper': return t('ZooKeeper обновляется на месте по одному узлу, с 3.4 без снапшота нужен snapshot.trust.empty=true.', p)
    case 'h_php_install': return t('Поставить PHP {to} рядом с {from} с теми же расширениями, старый продолжает работать. Какого-то пакета нет - убрать его из списка в /tmp и повторить последнюю строку.', p)
    case 'h_php_switch': return t('Перевести сайты на {to}: пулы из /etc/php/{from}/fpm/pool.d/ в /etc/php/{to}/fpm/pool.d/, у сайтов сокет php{to}-fpm.sock, CLI - update-alternatives --set php /usr/bin/php{to}.', p)
    case 'h_php_check': return t('Проверить версию и конфиг FPM, перезагрузить веб-сервер и открыть сайты.', p)
    case 'h_php_remove': return t('Когда сайты и кроны на {to} - удалить {from} (apt спросит, конфиги останутся).', p)
    case 'h_node_repo': return t('Перевести репозиторий NodeSource на {to} и обновить nodejs. Строка после правки скажет, нашелся ли репозиторий.', p)
    case 'h_node_check': return t('Проверить версию (последняя строка скажет, новая ли), перезапустить приложения, нативные модули пересобрать (npm rebuild).', p)
    case 'h_node_rollback': return t('Вернуть репозиторий на {from} и прежнюю версию.', p)
    case 'h_etc_backup': return t('Копия конфигов.', p)
    case 'h_nginx_upgrade': return t('Обновить nginx из репозитория nginx.org. Версия не поменялась - в sources.list.d старое имя дистрибутива или пакет зафиксирован (apt-mark showhold).', p)
    case 'h_nginx_check': return t('Проверить версию, конфиг и что служба работает.', p)
    case 'h_apt_rollback': return t('Вернуть прежнюю версию пакета.', p)
    case 'h_haproxy': return t('Ветку HAProxy задает репозиторий (PPA vbernat/haproxy-X.Y или backports): перевести его на {to}, apt update и apt install haproxy.', p)
    case 'h_haproxy_check': return t('Проверить конфиг и что служба работает.', p)
    case 'h_pg_dump': return t('Дамп всех баз кластера {from}, он при этом работает.', p)
    case 'h_pg_path': return t('Переезд штатно: apt install postgresql-{to}, пустой кластер {to} main убрать (pg_dropcluster {to} main --stop), затем pg_upgradecluster {from} main. Старый остается остановленным, к нему можно вернуться.', p)
    case 'h_my_dump': return t('Дамп всех баз (если root входит по паролю - добавить -p).', p)
    case 'h_rabbit_defs': return t('Выгрузить очереди, обменники и пользователей, без сообщений.', p)
    case 'h_db_copy': return t('Копия папки данных при остановленной службе или штатный дамп.', p)
    case 'h_repo': return t('Перевести репозиторий вендора {name} на {to} по его инструкции, apt update, обновить пакеты и проверить службу.', p)
    case 'h_rpm': return t('Пакеты не из apt: обновить {name} до {to} через dnf по инструкции вендора, перед этим копия данных и конфигов.', p)
    default: return id
  }
}

// что это за ПО: контейнер, образ, откуда запущен - чтобы сразу было понятно, о чем шаги
function wayHead(b: SwFixBlock, t: TFn): string {
  if (b.kind === 'host') return t('на хосте: {list}', { list: b.name })
  const a = b.args
  const parts = [t('контейнер {c}', { c: b.name }), t('образ {image}', { image: a.image ?? '' })]
  if (a.dir) parts.push(`${a.dir}/${a.files || ''}`, t('сервис {svc}', { svc: a.svc ?? '' }))
  else if (a.compose === 'no') parts.push(t('не из compose'))
  return parts.join(' · ')
}

// шаг, после которого на ноде должна быть новая версия: скопировали его - ждем отчет агента
const RESTART = new Set(['c_up', 'pull_up', 'rb_build'])
// столько после копирования дать на то, чтобы команду вставили и она отработала
const RUN_GRACE = 30_000

function hms(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

// Что на ноде сейчас и когда панель это узнала: после шагов пункт уходит сам с отчетом агента, а
// без этой строки было непонятно, ждать или что-то не так. Отчеты идут раз в every секунд, пока
// шаги открыты, карточка перечитывает их так же часто. Скопировали перезапуск, а свежий отчет
// показывает прежнюю версию - прямо сказать, что обновление не прошло и где смотреть
function SwNow({ g, b, data, ran, checking, onCheck, t }: {
  g: SoftwareItem
  b: SwFixBlock
  data: SoftwareOut
  ran: number
  checking: boolean
  onCheck: () => void
  t: TFn
}) {
  const a = b.args
  const at = data.report_at
  const p = { c: b.name, image: a.image ?? '', status: a.status ?? '', name: g.name, from: g.cycle, n: data.every ?? 15 }
  const fresh = !!at && ran > 0 && new Date(at).getTime() > ran + RUN_GRACE
  let after = ''
  if (ran > 0 && !fresh) after = t('Ждем отчет агента после перезапуска, панель проверяет каждые {n} с.', p)
  else if (fresh && b.mode === 'image')
    after = t('Отчет агента в {time}: {c} все еще на {image}. Если перезапуск уже выполнен, образ не заменен: смотрите, что сказали последние строки замены образа и перезапуска.', { ...p, time: hms(at) })
  else if (fresh && b.mode === 'pull')
    after = t('Отчет агента в {time}: в {c} все еще {name} {from}. Если перезапуск уже выполнен, автор образа его еще не пересобрал.', { ...p, time: hms(at) })
  else if (fresh)
    after = t('Отчет агента в {time}: в {c} все еще {name} {from}. Если шаги уже выполнены, смотрите вывод последней команды.', { ...p, time: hms(at) })
  return (
    <div className="sw-now small">
      <div>
        <span className="muted">
          {p.status ? t('Сейчас на ноде: {c} на {image}, {status}.', p) : t('Сейчас на ноде: {c} на {image}.', p)}{' '}
          {at
            ? t('Отчет агента в {time}, они идут раз в {n} с, пока шаги открыты, панель перечитывает их сама.', { ...p, time: hms(at) })
            : t('Отчетов агента еще не было.')}
        </span>{' '}
        <button className="ghost small" disabled={checking} onClick={onCheck}>
          {checking ? t('проверяю...') : t('Проверить сейчас')}
        </button>
      </div>
      {after && <div className={fresh ? 't-degraded' : 'muted'}>{after}</div>}
    </div>
  )
}

function SwFix({ g, data, checking, onCheck, t }: {
  g: SoftwareItem
  data: SoftwareOut
  checking: boolean
  onCheck: () => void
  t: TFn
}) {
  const blocks = g.fix ?? []
  const a = blocks[0]?.args ?? {}
  // когда скопировали команду перезапуска, по способу
  const [ran, setRan] = useState<Record<string, number>>({})
  return (
    <div className="sw-fix">
      {a.to && (
        <div className="muted small">
          {t('Новая версия: {name} {to}', { name: g.name, to: a.to })}
          {a.more ? `, ${t('подойдет и {list}', { list: a.more })}` : ''}
        </div>
      )}
      {blocks.map((b) => (
        <div key={b.kind + b.name} className="sw-fix-way">
          <div className="small">
            <b>{wayHead(b, t)}</b>
            {b.others.length > 0 && (
              <span className="muted">
                {' '}
                {t('Тот же образ у {list}: шаги те же, в их папке compose.', {
                  list: b.others.slice(0, WHERE_MAX).join(', ') + (b.others.length > WHERE_MAX ? ` ${t('и еще {n}', { n: b.others.length - WHERE_MAX })}` : ''),
                })}
              </span>
            )}
          </div>
          <FixSteps numbered steps={b.steps} text={(st) => swFixText(st.id, b.args, t)}
            onCopy={(st) => RESTART.has(st.id) && setRan({ ...ran, [b.kind + b.name]: Date.now() })} />
          {b.kind === 'container' && (
            <SwNow g={g} b={b} data={data} ran={ran[b.kind + b.name] ?? 0} checking={checking} onCheck={onCheck} t={t} />
          )}
        </div>
      ))}
      <div className="muted small">
        {blocks.some((b) => b.kind === 'host')
          ? t('Пункт уходит сам, когда этой версии на ноде не останется: у контейнеров - со следующим отчетом агента, у пакетов хоста - в течение часа.')
          : t('Пункт уходит сам, когда этой версии на ноде не останется, со следующим отчетом агента.')}
      </div>
    </div>
  )
}

function ddmmyyyy(iso: string): string {
  const [y, m, d] = iso.split('-')
  return `${d}.${m}.${y}`
}

function term(g: SoftwareItem, t: TFn): string {
  if (g.state === 'eol') return typeof g.eol === 'string' ? t('кончилась {d}', { d: ddmmyyyy(g.eol) }) : t('кончилась')
  if (typeof g.eol !== 'string') return t('срок не объявлен')
  return g.state === 'soon'
    ? t('до {d}, через {n} дн.', { d: ddmmyyyy(g.eol), n: g.left ?? 0 })
    : t('до {d}', { d: ddmmyyyy(g.eol) })
}

function whereText(g: SoftwareItem, t: TFn): string {
  const cw = g.where.filter((w) => w.kind === 'container')
  const cont = [...new Set(cw.map((w) => w.name))].sort()
  const host = g.where.filter((w) => w.kind === 'host').map((w) => w.name)
  const parts: string[] = []
  // один контейнер - сразу и образ с папкой compose: видно, что это за PHP
  if (cont.length === 1) parts.push([cont[0], cw[0].image, cw[0].dir].filter(Boolean).join(' · '))
  else if (cont.length)
    parts.push(
      cont.slice(0, WHERE_MAX).join(', ') + (cont.length > WHERE_MAX ? ` ${t('и еще {n}', { n: cont.length - WHERE_MAX })}` : ''),
    )
  if (host.length) parts.push(t('хост: {list}', { list: host.join(', ') }))
  return parts.join(' · ')
}

function age(days: number, t: TFn): string {
  const y = Math.floor(days / 365)
  return y >= 2 ? t('{n} г. назад', { n: y }) : t('{n} дн. назад', { n: days })
}

export function SoftwareCard({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [data, setData] = useState<SoftwareOut | null>(null)
  const [oldOpen, setOldOpen] = useState(false)
  const [open, setOpen] = useState('')
  const [busy, setBusy] = useState('')
  const mutes = s.alert_mutes ?? []
  const decide = (key: string, on: boolean) => {
    setBusy(key)
    updateServer(s.id, { alert_mutes: on ? [...new Set([...mutes, key])] : mutes.filter((m) => m !== key) })
      .then(onChanged)
      .catch(() => {})
      .finally(() => setBusy(''))
  }
  const [checking, setChecking] = useState(false)
  const load = useCallback(() => getSoftware(s.id).then(setData).catch(() => {}), [s.id])
  // открыты шаги - перечитывать с каждым отчетом агента: после перезапуска видно сразу, ушла ли
  // версия; иначе хватает раза в 10 минут
  const every = open ? Math.max(data?.every ?? 15, 10) * 1000 : 10 * 60_000
  useEffect(() => {
    load()
  }, [load])
  useEffect(() => {
    const h = window.setInterval(load, every)
    return () => window.clearInterval(h)
  }, [load, every])
  const checkNow = () => {
    setChecking(true)
    load().finally(() => setChecking(false))
  }
  // версия без поддержки ушла, пока была на экране: на что обновили - по свежему списку
  const fixed = useFixDone(data ? data.items.filter((g) => g.state === 'eol' || g.state === 'soon') : null, (g) => g.key, (g) => {
    if (data?.items.some((x) => x.key === g.key)) return t('{name} {cycle}: поддержка продлена, замечание закрыто', { name: g.name, cycle: g.cycle })
    const now = data?.items.filter((x) => x.name === g.name).map((x) => x.cycle) ?? []
    return now.length
      ? t('{name} обновлен с {from} до {to}, замечание закрыто', { name: g.name, from: g.cycle, to: now.join(', ') })
      : t('{name} {cycle} на ноде больше нет, замечание закрыто', { name: g.name, cycle: g.cycle })
  }, { live: (g) => !(g.mkey && mutes.includes(g.mkey)) })
  if (!data || (!data.items.length && !data.unknown.length && !data.old.length && !fixed.done.length)) return null
  return (
    <div className="net-cards">
      <div className="chart-card net-card net-card-wide" id="mcard-software">
        <div className="net-head">
          <div className="chart-cap">{t('Версии ПО')}</div>
          {data.built && (
            <span className="muted small">{t('сроки поддержки по endoflife.date на {d}', { d: ddmmyyyy(data.built) })}</span>
          )}
        </div>
        {fixed.bars}
        {data.items.length > 0 && (
          <div className="sw-list">
            {data.items.map((g) => {
              const muted = !!g.mkey && mutes.includes(g.mkey)
              return (
                <Fragment key={g.key}>
                  <div className={`sw-row${muted ? ' sw-row-off' : ''}`}>
                    <span className={`lk-dot ${muted ? '' : g.state === 'eol' ? 'lk-dot-bad' : g.state === 'soon' ? 'lk-dot-warn' : 'lk-dot-ok'}`} />
                    <span className="sw-name">
                      {g.name} <span className="mono">{g.cycle}</span>
                    </span>
                    <span className="sw-where muted small" title={g.where.map((w) => [w.name, w.ver, w.image, w.dir].filter(Boolean).join(' ')).join('\n')}>
                      {whereText(g, t)}
                    </span>
                    <span className={`sw-term small ${muted ? 'muted' : g.state === 'eol' ? 't-down' : g.state === 'soon' ? 't-degraded' : 'muted'}`}>
                      {term(g, t)}
                    </span>
                    <span className="sw-act">
                      {muted && <span className="osupd-adv-why muted">{swDecisionLabel(g.state, true, t)}</span>}
                      {(g.fix?.length ?? 0) > 0 && !muted && (
                        <button className="ghost small row-act" onClick={() => setOpen(open === g.key ? '' : g.key)}>
                          {open === g.key ? t('скрыть') : t('как исправить')}
                        </button>
                      )}
                      {!isViewer && g.mkey && (
                        <button className="ghost small row-act" disabled={busy === g.mkey} onClick={() => decide(g.mkey ?? '', !muted)}>
                          {muted ? t('вернуть') : swDecisionLabel(g.state, false, t)}
                        </button>
                      )}
                    </span>
                  </div>
                  {open === g.key && !muted && <SwFix g={g} data={data} checking={checking} onCheck={checkNow} t={t} />}
                </Fragment>
              )
            })}
          </div>
        )}
        {data.unknown.length > 0 && (
          <div className="net-foot">
            <div>
              {t('Версия не видна, в теге образа ее нет: {list}.', {
                list: data.unknown.map((u) => `${u.name} (${u.pname})`).join(', '),
              })}{' '}
              {data.agent_ok
                ? t('В окружении образа версии тоже нет.')
                : t('Агент 2.35 и новее берет версию из окружения образа: обновите агент.')}
            </div>
          </div>
        )}
        {data.old.length > 0 && (
          <div className="sw-old">
            <button className="ghost small" onClick={() => setOldOpen(!oldOpen)}>
              {oldOpen ? '▾' : '▸'} {t('Образы старше года: {n}', { n: data.old.length })}
            </button>
            {oldOpen && (
              <div className="sw-list">
                {data.old.map((o) => (
                  <div key={o.name} className="sw-row">
                    <span className="lk-dot lk-dot-warn" />
                    <span className="sw-name mono">{o.name}</span>
                    <span className="sw-where muted small mono">{o.image}</span>
                    <span className="sw-term small muted">
                      {t('собран {d}', { d: new Date(o.built * 1000).toLocaleDateString() })}, {age(o.age_days, t)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        <div className="net-foot">
          <div>
            {t(
              'Версии берутся из инвентаря баз, окружения и тега образа, на хосте - из пакетов сторонних репозиториев (pgdg, sury, nodesource, nginx.org, MariaDB, MySQL, Redis). Пакеты дистрибутива дистрибутив чинит, пока жива ОС, их срок - поддержка ОС.',
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
