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
  if (m) return t('Пробный дамп не прошёл — дампы НЕ включены: {err}', { err: helperText(m[1], t) || '—' })
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
