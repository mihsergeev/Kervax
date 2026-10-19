// Ответы helper'ов с ноды — на язык интерфейса.
//
// Скрипты helper'ов общие для всех установок, и отвечают они по-английски. Панель выводила
// их как есть: после «включить дампы» по-русски вдруг шло «OK pg dumps enabled -> …». Здесь
// известные фразы переводятся, всё незнакомое (например, сырой вывод pg_dump в тексте
// ошибки) остаётся как пришло. Фразы сверены с agent/backup-setup.sh.

type T = (s: string, p?: Record<string, string | number>) => string

const ENGINES: Record<string, string> = {
  pg: 'PostgreSQL', mysql: 'MySQL/MariaDB', ch: 'ClickHouse', redis: 'Redis',
  rabbitmq: 'RabbitMQ', k8s: 'Kubernetes (etcd)', grafana: 'Grafana', neo4j: 'Neo4j',
}

// «pg» → PostgreSQL, «pg@traccar-postgres» → PostgreSQL (traccar-postgres),
// «ch@k8s.default.sts.clickhouse» → ClickHouse (default/clickhouse)
function engineName(slot: string): string {
  const [code, cont] = slot.split('@')
  const name = ENGINES[code] ?? code
  if (cont && cont.startsWith('k8s.')) {
    const [, ns, , ...wl] = cont.split('.')
    return `${name} (${ns}/${wl.join('.')})`
  }
  return cont ? `${name} (${cont})` : name
}

const KIND: Record<string, string> = { statefulset: 'StatefulSet', deployment: 'Deployment', daemonset: 'DaemonSet' }

// Готовые команды к ответам про дампы. Адрес панели знает только она сама, helper на ноде его
// не знает, поэтому команды дописываются здесь.
const panelOrigin = () => (typeof window !== 'undefined' ? window.location.origin : '')
const updateHelperCmd = () => `curl -fsSL ${panelOrigin()}/api/agent/setup/backup-setup.sh | sudo bash`
// та же проверка руками: причина будет в выводе (при успехе дамп включится с настройками по умолчанию)
const manualDumpCmd = (slot: string) => {
  const [eng, cont] = slot.split('@')
  return `sudo /lib65/kervax/kervax-backup-helper dump-setup ${eng}${cont ? ' ' + cont : ''}`
}
// set -C: готовый /root/.my.cnf команда не перезапишет, а скажет, что файл уже есть
const myCnfCmd = "sudo sh -c 'umask 077; set -C; printf \"[client]\\nuser=root\\npassword=<password>\\n\" > /root/.my.cnf'"

export function helperText(raw: string | null | undefined, t: T): string {
  const s = (raw ?? '').trim()
  if (!s) return ''
  let m: RegExpMatchArray | null

  m = s.match(/^OK (\S+) dumps enabled -> (\S+) \((.+?), keeping (\d+), space threshold (\d+)%\)(.*)$/)
  if (m) {
    const [, eng, dir, when, keep, minfree, rest] = m
    const whenTxt = when === 'before every backup'
      ? t('перед каждым бэкапом')
      : when.startsWith('daily, on its own timer')
        ? t('ежедневно по своему таймеру — файлового бэкапа на ноде нет, копия только локальная')
        : when
    const parts = [
      t('Дампы {engine} включены → {dir}: {when}, храним {keep} последних, порог свободного места {minfree}%.', {
        engine: engineName(eng), dir, when: whenTxt, keep, minfree,
      }),
    ]
    const added = rest.match(/\((\S+) was added to the backup list\)/)
    if (added) parts.push(t('Каталог {dir} добавлен в список бэкапа.', { dir: added[1] }))
    const excluded = rest.match(/WARNING: (\S+) is on the exclusion list/)
    if (excluded) parts.push(t('ВНИМАНИЕ: {dir} в исключениях — дампы не попадут в бэкап!', { dir: excluded[1] }))
    if (rest.includes('with the next scheduled backup'))
      parts.push(t('Первый дамп снимется с ближайшим бэкапом по расписанию.'))
    else if (rest.includes('on the next timer run'))
      parts.push(t('Первый дамп снимется при ближайшем запуске таймера.'))
    return parts.join(' ')
  }
  m = s.match(/^the dump probe failed, dumps were NOT enabled: ?(.*)$/)
  if (m) return t('Пробный дамп не прошел - дампы НЕ включены: {err}', {
    // пустую причину от старого helper'а дописывает бэкенд (dump_reason.py), это запасной текст
    err: helperText(m[1], t) || t('helper на сервере не сообщил причину. Обновите helper backup-setup и нажмите еще раз: {cmd}', { cmd: updateHelperCmd() }),
  })
  // причина, которую дописала панель: helper на ноде ее не прислал (dump_reason.py)
  m = s.match(/^no reason came from backup-setup (\S+) for ([^\s:]+)(: helpers before (\S+) lose the error of the check, update it)?( \(MySQL on the host)?/)
  if (m) {
    const ver = m[1] === 'unknown' ? '' : ` ${m[1]}`
    if (!m[3]) return t('helper backup-setup{ver} не сообщил причину. Запустите ту же проверку на сервере, причина будет в выводе: {cmd}', { ver, cmd: manualDumpCmd(m[2]) })
    // команда - последней: текст после нее читался бы как ее продолжение
    const parts = [t('helper backup-setup{ver} на сервере не передает ошибку проверки, так было до {since}.', { ver, since: m[4] })]
    if (m[5]) parts.push(t('Частая причина у MySQL 8 на хосте: root без пароля не пускают, а новый helper входит через /etc/mysql/debian.cnf или /root/.my.cnf.'))
    parts.push(t('Обновите helper и нажмите еще раз: {cmd}', { cmd: updateHelperCmd() }))
    return parts.join(' ')
  }
  // вход в MySQL на хосте (backup-setup 0.38): что пробовали и чем отказали
  m = s.match(/^MySQL did not let the helper in: (.*)$/)
  if (m) return t('MySQL не пустил helper ({err}). Дампу нужен вход: положите в /root/.my.cnf пароль root или пользователя с правами SELECT, SHOW VIEW, TRIGGER, LOCK TABLES, EVENT, PROCESS и нажмите еще раз: {cmd}', {
    err: m[1].trim().replace(/root over the socket/g, t('root через сокет')), cmd: myCnfCmd,
  })
  if (/^no mysqldump or mariadb-dump on the host/.test(s))
    return t('На сервере нет mysqldump и mariadb-dump: не установлен клиент MySQL. Поставьте его (для MariaDB - mariadb-client) и нажмите еще раз: {cmd}', { cmd: 'sudo apt install mysql-client' })
  m = s.match(/^the check of (\S+) exited with code (\d+) and printed nothing$/)
  if (m) return t('Проверка {engine} завершилась с кодом {rc} и ничего не написала. Запустите ее на сервере, причина будет в выводе: {cmd}', { engine: engineName(m[1]), rc: m[2], cmd: manualDumpCmd(m[1]) })
  m = s.match(/^docker does not know the container (\S+)$/)
  if (m) return t('Docker на сервере не знает контейнер {name}: его удалили или переименовали.', { name: m[1] })
  if (/^no neo4j-admin on the host$/.test(s)) return t('На сервере нет neo4j-admin.')
  if (/^grafana\.db not found on the host or in volumes$/.test(s)) return t('grafana.db не найден ни на хосте, ни в томах.')
  if (/^neither sqlite3 nor python3 with sqlite3 on the host$/.test(s))
    return t('На сервере нет ни sqlite3, ни python3 с модулем sqlite3. Поставьте sqlite3 и нажмите еще раз: {cmd}', { cmd: 'sudo apt install sqlite3' })
  // базы в подах kubernetes
  if (/^no access to the Kubernetes cluster from this node/.test(s))
    return t('У helper на этой ноде нет доступа к кластеру Kubernetes: дамп из пода включается на управляющей ноде.')
  m = s.match(/^no (statefulset|deployment|daemonset) (\S+) in namespace (\S+)$/)
  if (m) return t('В пространстве {ns} нет {kind} {name}.', { ns: m[3], kind: KIND[m[1]], name: m[2] })
  m = s.match(/^no running pod of (statefulset|deployment|daemonset) (\S+) in namespace (\S+)$/)
  if (m) return t('У {kind} {name} в пространстве {ns} нет запущенного пода.', { ns: m[3], kind: KIND[m[1]], name: m[2] })
  // helper до 0.28 входил в ClickHouse пользователем default; с 0.28 схема читается из файлов
  if (/^clickhouse did not let the default user in/.test(s))
    return t('ClickHouse не пустил пользователя default: база требует пароль. Helper backup-setup 0.28 снимает схему из файлов, без входа в базу — обновите его на ноде.')
  m = s.match(/^no ClickHouse metadata directory at (\S+)$/)
  if (m) return t('Не найден каталог со схемой ClickHouse: {dir}.', { dir: m[1] })
  if (/^no databases found in the ClickHouse metadata$/.test(s))
    return t('В каталоге со схемой ClickHouse не нашлось ни одной базы.')
  if (/^redis did not answer PONG/.test(s))
    return t('Redis не ответил на ping: похоже, нужен пароль, которого нет в окружении базы.')
  m = s.match(/^dumps from pods are not supported for (\S+)$/)
  if (m) return t('Дамп из пода для {engine} не поддерживается.', { engine: engineName(m[1]) })
  m = s.match(/^OK (\S+) dumps disabled, local files removed/)
  if (m) return t('Дампы {engine} выключены, локальные файлы удалены (история осталась в restic).', { engine: engineName(m[1]) })
  m = s.match(/^(\S+) dumps were not enabled anyway$/)
  if (m) return t('Дампы {engine} и так не были включены.', { engine: engineName(m[1]) })
  m = s.match(/^unknown engine: (\S+)/)
  if (m) return t('Неизвестный движок дампа: {engine}', { engine: m[1] })
  m = s.match(/^the dump directory must be an absolute path: (.*)$/)
  if (m) return t('Каталог дампов должен быть абсолютным путём: {dir}', { dir: m[1] })
  m = s.match(/^invalid dump directory: (.*)$/)
  if (m) return t('Недопустимый каталог дампов: {dir}', { dir: m[1] })
  if (/^the root \/ cannot be used for dumps$/.test(s)) return t('Корень / для дампов использовать нельзя.')

  if (/^OK \(started\)$/.test(s)) return t('Бэкап запущен.')
  if (s === 'OK') return t('Готово.')
  m = s.match(/^restic is already (\S+) - no update needed$/)
  if (m) return t('restic уже {v} — обновлять не нужно.', { v: m[1] })
  m = s.match(/^restic updated to (\S+) \(was ([^)]*)\)/)
  if (m) return t('restic обновлён до {v} (было {old}).', { v: m[1], old: m[2] })
  m = s.match(/^could not download restic ?(\S*)$/)
  if (m) return t('Не удалось скачать restic {v}', { v: m[1] })
  if (/^the (sha256|checksum) did not match/.test(s)) return t('Контрольная сумма restic не совпала — обновление отменено.')
  if (/^the downloaded restic does not run$/.test(s)) return t('Скачанный restic не запускается — обновление отменено.')
  if (/^no restic found on the node/.test(s)) return t('На ноде не найден restic.')
  if (/^timer( file)? not found$/.test(s)) return t('Таймер бэкапа не найден.')
  if (/^script not found$/.test(s)) return t('Скрипт бэкапа не найден.')
  if (/^bad time$/.test(s)) return t('Неверное время.')
  if (/^bad mode$/.test(s)) return t('Неверный режим.')
  m = s.match(/^bad path: (.*)$/)
  if (m) return t('Недопустимый путь: {path}', { path: m[1] })
  if (/^no paths$/.test(s)) return t('Не указаны пути.')
  m = s.match(/^OK provisioned \(([^,]+), ([^)]+)\)$/)
  if (m) return t('Бэкап настроен ({mode}, {time}).', { mode: m[1], time: m[2] })
  m = s.match(/^OK adopted -> the panel layout \(([^,]+), ([^)]+)\)$/)
  if (m) return t('Бэкап переведён под управление панели ({mode}, {time}).', { mode: m[1], time: m[2] })
  if (/^already managed by the panel/.test(s)) return t('Бэкап уже под управлением панели — переносить нечего.')
  if (/^env not found \(the backup was not configured by this panel\)$/.test(s))
    return t('Бэкап настроен не панелью — доступов для восстановления у панели нет.')
  if (/^unknown action$/.test(s)) return t('Helper на ноде не знает эту команду — переустановите его.')

  // бэкап-сервер (backupserver-setup)
  m = s.match(/^OK rest-server updated -> (\S+) \(was ([^,]+), now [^)]*\), answering (\d+)$/)
  if (m) return t('rest-server обновлён до {v} (было {old}), отвечает {code}.', { v: m[1], old: m[2], code: m[3] })
  m = s.match(/^OK rest-server deployed: port (\d+), append-only plus private-repos, HTTP (\d+), ufw (\S+)$/)
  if (m) return t('rest-server развёрнут: порт {port}, append-only и private-repos, HTTP {code}, ufw {ufw}.', { port: m[1], code: m[2], ufw: m[3] })
  m = s.match(/^OK rest-server was already deployed and is running \(port (\d+), HTTP (\d+), ufw (\S+)\)$/)
  if (m) return t('rest-server уже развёрнут и работает (порт {port}, HTTP {code}, ufw {ufw}).', { port: m[1], code: m[2], ufw: m[3] })
  m = s.match(/^the image update failed: ?(.*)$/)
  if (m) return t('Обновление образа не удалось: {err}', { err: m[1] || '—' })
  m = s.match(/^OK native TLS rest-server on :(\d+)$/)
  if (m) return t('HTTPS rest-server запущен на порту {port}.', { port: m[1] })
  m = s.match(/^OK provisioned (\S+)$/)
  if (m) return t('Репозиторий {name} создан.', { name: m[1] })
  m = s.match(/^prune scripts regenerated: (\d+)$/)
  if (m) return t('Скрипты ротации пересобраны: {n}.', { n: m[1] })
  return s
}

// Отказ команды дампа: кроме ответов helper'а это и тексты агента или панели, когда до helper'а
// дело не дошло (таймаут, нет спула). У каждого исхода свое "что сделать", пустым не бывает.
export function dumpFailText(raw: string | null | undefined, t: T): string {
  const s = (raw ?? '').trim()
  if (!s) return t('Сервер ответил отказом без текста. Обновите helper backup-setup и нажмите еще раз: {cmd}', { cmd: updateHelperCmd() })
  // панель: агент взял команду и не прислал ответ, или не взял вовсе
  if (/^агент не ответил/.test(s))
    return t('Агент на сервере не ответил вовремя: команда не дошла до сервера или helper бэкапа завис. Проверьте на сервере: {cmd}', { cmd: 'sudo systemctl status kervax-agent kervax-backup-req.path' })
  // агент: запрос лежит в спуле, а helper его не забрал
  if (/^процессор не ответил/.test(s))
    return t('Helper бэкапа на сервере не взял команду. Проверьте на сервере: {cmd}. Если helper не стоит, поставьте: {setup}', {
      cmd: 'sudo systemctl status kervax-backup-req.path', setup: updateHelperCmd(),
    })
  if (/^спул недоступен/.test(s))
    return t('На сервере нет helper backup-setup, через него панель включает дампы. Поставьте: {cmd}', { cmd: updateHelperCmd() })
  return helperText(s, t) || s
}
