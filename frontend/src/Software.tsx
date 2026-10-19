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
// пункт уходит с главной и из счетчика раздела, а здесь приглушен с кнопкой "вернуть"

type TFn = (k: string, p?: Record<string, string | number>) => string
const WHERE_MAX = 6

// Шаг исправления словами; a - подстановки способа (продукт, from, to, контейнер, образ, пакеты)
function swFixText(id: string, a: Record<string, string>, t: TFn): string {
  const p = { ...a, env: a.env || a.image || '' }
  switch (id) {
    case 'c_prep': return t('Все шаги - в одном окне терминала: эта команда находит compose-проект контейнера {c} (проект, сервис, каталог, файлы) и тома на запись с их объемом, остальные берут это отсюда. Пустой project - контейнер запущен не из compose, тогда тег меняется там, откуда его запускают. Для отката в новом окне - сначала снова она.', p)
    case 'img_pull': return t('Скачать {new} заранее, пока работает старая версия: тег ветки {to} - ее последний выпуск. Нет такого тега - pull скажет not found и ничего не поменяется, тогда точную версию взять на странице образа.', p)
    case 'c_backup': return t('Копия для отката: compose-файлы с .env и тома на запись одним архивом в /var/backups/kervax-upgrade/{c}, контейнер на это время остановлен (места - как объем томов из первой команды), прежний образ - под тегом kervax-old. Большие данные, которые новая версия не трогает (загрузки, статика), можно не копировать: уберите их путь из M.', p)
    case 'img_apply': return t('Поменять тег в compose-файле ({image} на {new}) и пересоздать только этот сервис. Строки image с этим же образом у других сервисов файла тоже поменяются. Если образ задан переменной (.env), grep ничего не найдет и up не запустится: тег меняется там.', p)
    case 'c_check': return t('Проверить: контейнер работает, образ новый, в логе нет ошибок. Пункт уйдет сам со следующим отчетом агента, около минуты.', p)
    case 'c_rollback': return t('Откат: тома и compose-файлы из последней копии (у тега без версии - еще и прежний образ под своим именем). То, что успела записать новая версия, остается рядом с суффиксом .kervax-new: удалить руками, когда откат проверен.', p)
    case 'pull_apply': return t('Тег {image} без версии: образ просто давно не скачивали. Скачать свежий и пересоздать только этот сервис. Версия не поменялась - автор образа еще не пересобрал его на новой базе.', p)
    case 'rb_from': return t('Версия {name} пришла из базового образа ({env}): найти FROM в Dockerfile проекта и поменять на {name} {to}. Если там тег без версии (nginx:alpine), хватит пересборки.', p)
    case 'rb_build': return t('Пересобрать образ со свежей базой (--pull) и пересоздать только этот сервис. У сервиса нет build: в compose - его образ скачается заново; версия не поменялась - образ собирают не здесь, обновить его там, где собирают.', p)
    case 'c_ci': return t('Образ {image} собирают не на этой ноде, версия {name} пришла из его базового образа ({env}). Поменять FROM в Dockerfile проекта на {name} {to} (тег там без версии - просто пересобрать) и выкатить как обычно, чужой образ - обновить до версии на поддерживаемой базе. Пункт уйдет, когда на ноде заработает новая сборка.', p)
    case 'c_dangling': return t('Контейнер работает на образе без имени: имя уже перевешено на другую сборку. Пересоздать контейнер (docker compose up -d с именем сервиса) - он пойдет на ней; версия {name} не поменялась - пересобрать образ на поддерживаемой базе.', p)
    case 'major': return t('{name} {from} на {to} - смена основной версии: до обновления прочитать заметки о переходе (upgrade guide, breaking changes), что перестанет работать.', p)
    case 'hint_grafana': return t('Grafana при первом запуске переводит свою базу (grafana.db в /var/lib/grafana) на новую схему, и старая версия с ней может не запуститься: без копии откат невозможен. Сторонние плагины проверить на совместимость с {to}.', p)
    case 'hint_traefik': return t('Traefik 3 читает часть настроек иначе, чем 2 (синтаксис правил, часть меток и middlewares): до переезда - руководство по миграции с v2 на v3, в нем совместимый режим core.defaultRuleSyntax: v2.', p)
    case 'hint_runtime': return t('Это среда выполнения приложения: проверить приложение и его зависимости на {name} {to}, лучше сначала на стенде.', p)
    case 'hint_redis': return t('Новый Redis сам читает старые dump.rdb и AOF, а старый новый формат уже не прочитает: откат только из копии. С 7.4 Redis не под BSD (RSAL и SSPL, с 8.0 еще AGPL), совместимая замена с теми же данными - Valkey.', p)
    case 'hint_valkey': return t('Новый Valkey сам читает старые RDB и AOF, а старый новый формат уже не прочитает: откат только из копии.', p)
    case 'hint_os': return t('Это базовый образ ОС: если контейнер ставит пакеты при запуске, на {name} {to} их имена и версии могут быть другими.', p)
    case 'no_target': return t('В таблице сроков нет поддерживаемой версии {name} новее {from}: выбрать по сайту вендора.', p)
    case 'db_dump_pg': return t('Дамп всех баз (pg_dumpall), пока база работает: по нему данные переезжают в новую версию. Пустой файл - дамп не получился (суперпользователь другой - поменять -U).', p)
    case 'db_copy': return t('Копия каталога данных для отката при остановленной базе: минуты, по объему из первой команды, места нужно столько же. База запускается сразу после копии.', p)
    case 'db_inplace': return t('Команды смены тега тут нет намеренно: после копии - тег {to} в compose руками, docker compose up -d с именем сервиса и лог до конца перевода данных.', p)
    case 'path_postgresql': return t('PostgreSQL {to} не читает файлы {from}: тег в compose просто так не менять, база не запустится. Путь: рядом новый сервис {name} {to} с новым пустым каталогом данных, в него дамп (gunzip -c дамп | docker exec -i новый psql -U пользователь -d postgres), проверка, переключение приложения, старый контейнер с данными - до конца проверки. На больших базах быстрее pg_upgrade (образ tianon/postgres-upgrade). Расширения (PostGIS, TimescaleDB, pgvector) нужны в новом образе той же версии.', p)
    case 'path_mysql': return t('MySQL обновляется на месте, но по одной LTS за раз (5.7, 8.0, 8.4): при первом запуске новая версия переводит данные, обратно - только из копии. Совместимость заранее проверяет MySQL Shell: util.checkForServerUpgrade().', p)
    case 'path_mariadb': return t('MariaDB обновляется на месте, после первого запуска новой версии нужен mariadb-upgrade (официальный образ делает его сам при MARIADB_AUTO_UPGRADE=1). Через несколько LTS лучше по одной. Обратно - только из копии.', p)
    case 'path_mongodb': return t('MongoDB - по одной основной версии за раз (5.0, 6.0, 7.0, 8.0), перед каждым шагом setFeatureCompatibilityVersion на текущую, через версию не запустится. Обратно - только из копии.', p)
    case 'path_elasticsearch': return t('Elasticsearch - через последний минорный выпуск своей основной версии (7.17, 8.19), что мешает переезду, покажет Upgrade Assistant в Kibana; индексы, созданные две основные версии назад, переиндексировать. Обратно - только из снапшота.', p)
    case 'path_clickhouse': return t('ClickHouse обновляется на месте, лучше на LTS (выпуски X.3 и X.8); перед этим прочитать Backward Incompatible Changes в changelog всех версий между. Обратно - только из копии, если новая успела переписать данные.', p)
    case 'path_rabbitmq': return t('RabbitMQ - через промежуточные версии по таблице upgrade path (с 3.8 до 3.13 по одной, дальше 4.x), перед каждым шагом включить все feature flags (rabbitmqctl enable_feature_flag all), очереди лучше разобрать. Обратно - только из копии.', p)
    case 'path_zookeeper': return t('ZooKeeper обновляется на месте по одному узлу; с 3.4 на 3.5 и новее без снапшота нужен snapshot.trust.empty=true. Обратно - из копии.', p)
    case 'h_php_install': return t('Поставить PHP {to} рядом с {from} со всеми расширениями, которые есть у {from} (каких нет для {to}, пропускает). Старая версия продолжает работать.', p)
    case 'h_php_switch': return t('Перенести на {to}: пулы из /etc/php/{from}/fpm/pool.d/ в /etc/php/{to}/fpm/pool.d/ (в listen сокет php{to}-fpm.sock, если он там), свои настройки из php.ini, у сайтов fastcgi_pass (nginx) или SetHandler (apache) - на новый сокет, CLI - update-alternatives --set php /usr/bin/php{to}. Откат до удаления старой - вернуть сайтам старый сокет.', p)
    case 'h_php_check': return t('Проверить версию и конфиг FPM, потом перезагрузить веб-сервер и открыть сайты.', p)
    case 'h_php_remove': return t('Когда сайты и кроны на {to}: удалить {from} (apt покажет список и спросит, конфиги в /etc/php/{from} останутся). Пункт уйдет в течение часа.', p)
    case 'h_node_repo': return t('Перевести репозиторий NodeSource на {to} и обновить nodejs (npm в том же пакете).', p)
    case 'h_node_check': return t('Проверить версию, перезапустить приложения на Node (systemd, pm2), модули с нативной частью пересобрать (npm rebuild в каталоге приложения). Пункт уйдет в течение часа.', p)
    case 'h_node_rollback': return t('Откат: репозиторий обратно на {from} и прежняя версия пакета.', p)
    case 'h_etc_backup': return t('Копия конфигов в /var/backups/kervax-upgrade.', p)
    case 'h_nginx_upgrade': return t('Репозиторий nginx.org отдает текущую версию своей ветки (stable или mainline). Версия осталась старой - в /etc/apt/sources.list.d/ старое кодовое имя дистрибутива или пакет зафиксирован (apt-mark showhold).', p)
    case 'h_nginx_check': return t('Проверить версию, конфиг и что служба работает. Пункт уйдет в течение часа.', p)
    case 'h_apt_rollback': return t('Откат: прежняя версия пакета (конфиги - из копии, если их меняли).', p)
    case 'h_haproxy': return t('Ветку HAProxy задает репозиторий (PPA vbernat/haproxy-X.Y или backports): перевести его на {to}, apt update и apt install haproxy.', p)
    case 'h_haproxy_check': return t('Проверить конфиг и что служба работает. Пункт уйдет в течение часа.', p)
    case 'h_pg_dump': return t('Дамп всех баз кластера {from}/main, пока он работает.', p)
    case 'h_pg_path': return t('Переезд штатными средствами Debian: sudo apt install postgresql-{to}, пустой кластер {to} main, который создала установка, убрать (pg_dropcluster {to} main --stop, только сверившись в pg_lsclusters, что он новый и пустой), потом pg_upgradecluster {from} main - старый кластер остается остановленным, к нему можно вернуться. Расширения нужны для {to}. Когда все проверено - pg_dropcluster {from} main и apt remove postgresql-{from}, пункт уйдет в течение часа.', p)
    case 'h_my_dump': return t('Дамп всех баз (root входит по сокету; если по паролю - добавить -p).', p)
    case 'h_rabbit_defs': return t('Выгрузить определения (очереди, обменники, пользователи), сообщения в них не попадают.', p)
    case 'h_db_copy': return t('Копия каталога данных при остановленной службе (systemctl stop, tar, systemctl start) или штатный дамп.', p)
    case 'h_repo': return t('Пакеты {name} - из репозитория вендора: перевести его на {to} по инструкции вендора, apt update и обновить пакеты, затем проверить службу и логи. Пункт уйдет в течение часа.', p)
    case 'h_rpm': return t('Пакеты не из apt: обновить {name} до {to} средствами дистрибутива (dnf, для PHP - модули remi) по инструкции вендора, перед этим копия данных и конфигов.', p)
    default: return id
  }
}

function SwFix({ g, t }: { g: SoftwareItem; t: TFn }) {
  const blocks = g.fix ?? []
  const a = blocks[0]?.args ?? {}
  const many = blocks.length > 1
  const head = (b: SwFixBlock) => (b.kind === 'host' ? t('на хосте: {list}', { list: b.name }) : t('контейнер {c}', { c: b.name }))
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
          {(many || b.others.length > 0) && (
            <div className="small">
              <b>{head(b)}</b>
              {b.others.length > 0 && (
                <span className="muted">
                  {' '}
                  {t('То же у {list}: команды те же, в первой строке подставьте имя контейнера.', {
                    list: b.others.slice(0, WHERE_MAX).join(', ') + (b.others.length > WHERE_MAX ? ` ${t('и еще {n}', { n: b.others.length - WHERE_MAX })}` : ''),
                  })}
                </span>
              )}
            </div>
          )}
          <FixSteps steps={b.steps} text={(st) => swFixText(st.id, b.args, t)} />
        </div>
      ))}
      <div className="muted small">
        {t('Пункт уходит сам, когда этой версии на ноде не останется: у контейнеров - со следующим отчетом агента, у пакетов хоста - в течение часа.')}
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
  const cont = [...new Set(g.where.filter((w) => w.kind === 'container').map((w) => w.name))].sort()
  const host = g.where.filter((w) => w.kind === 'host').map((w) => w.name)
  const parts: string[] = []
  if (cont.length)
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
  const load = useCallback(() => {
    getSoftware(s.id).then(setData).catch(() => {})
  }, [s.id])
  useEffect(() => {
    load()
    const h = window.setInterval(load, 10 * 60_000)
    return () => window.clearInterval(h)
  }, [load])
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
                    <span className="sw-where muted small" title={g.where.map((w) => `${w.name} ${w.ver}`).join('\n')}>
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
                  {open === g.key && !muted && <SwFix g={g} t={t} />}
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
