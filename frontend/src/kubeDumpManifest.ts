// Генератор CronJob-манифестов для дампа СУБД в кластере. Отдельный модуль (без React):
// чистые функции, которые можно прогнать тестом, не поднимая компонент.
import type { KubePod, KubeCred } from './api'
import { tr } from './i18n'

// Один под ≠ одна база: у StatefulSet бывают реплики (mysql-0, mysql-1) — это ОДНА база,
// дампить её надо один раз через Service. А вот два разных StatefulSet'а (wordpress-mariadb
// и shop-mariadb) — две независимые базы, каждой нужен свой CronJob. Группируем поды по
// имени без суффикса реплики: это и есть имя сервиса.
// Имя пода → имя сервиса. Два вида суффиксов, и срезать надо оба:
//   StatefulSet: mysql-0, mysql-1                  → mysql
//   Deployment:  wordpress-mariadb-b65684cf4-p2z7l → wordpress-mariadb
// Хеш Deployment'а МЕНЯЕТСЯ при каждом передеплое: не срежь мы его, CronJob после
// обновления приложения начал бы писать в новый каталог, а старые дампы остались бы
// сиротами — и никто бы этого не заметил.
export function podService(pod: string): string {
  // deployment: <имя>-<хеш ReplicaSet>-<суффикс пода>
  const dep = pod.match(/^(.+)-[a-z0-9]{6,10}-[a-z0-9]{5}$/)
  if (dep) return dep[1]
  // statefulset: <имя>-<номер реплики>
  const sts = pod.match(/^(.+)-\d+$/)
  if (sts) return sts[1]
  // одиночный суффикс (DaemonSet/ReplicaSet). Срезаем ТОЛЬКО если он похож на случайный
  // (есть цифра): иначе «mysql-slave» превратился бы в «mysql» — и дамп ушёл бы не туда.
  const bare = pod.match(/^(.+)-([a-z0-9]{5})$/)
  if (bare && /\d/.test(bare[2])) return bare[1]
  return pod
}

function podServices(pods: string[]): { ns: string; svc: string }[] {
  const seen = new Map<string, { ns: string; svc: string }>()
  for (const ref of pods) {
    const [ns, pod] = ref.includes('/') ? ref.split('/') : ['default', ref]
    const svc = podService(pod)
    seen.set(`${ns}/${svc}`, { ns, svc })
  }
  return [...seen.values()]
}

export function kubeDumpManifest(
  engine: string, pods: string[], node: string, podObjs?: KubePod[],
): string {
  const svcs = podServices(pods)
  // креды берём у любого пода этого сервиса (реплики делят один Deployment → один секрет)
  const credOf = (ns: string, svc: string): KubeCred | undefined =>
    (podObjs ?? []).find((p) => p.ns === ns && podService(p.name) === svc && p.cred)?.cred
  // несколько баз → один YAML-документ с разделителями: применяется одним kubectl apply
  return svcs
    .map((x) => oneDumpManifest(engine, x.ns, x.svc, node, svcs.length > 1, credOf(x.ns, x.svc)))
    .join('\n---\n')
}

// имя выходного файла + расширение по движку
const OUT: Record<string, { base: string; ext: string }> = {
  pg: { base: 'dump', ext: 'sql' }, mysql: { base: 'dump', ext: 'sql' },
  ch: { base: 'schema', ext: 'sql' }, redis: { base: 'dump', ext: 'rdb' },
  rabbitmq: { base: 'defs', ext: 'json' },
}

type CredVars = { user?: string; pass?: string; root?: string }

// credVars — по именам кред-переменных пода подбираем, чем аутентифицироваться. null =
// уверенно не собрать (оставим плейсхолдер). {} = аутентификация не нужна (открытый redis/ch).
function credVars(engine: string, cred: KubeCred): CredVars | null {
  const names = (cred.env ?? []).map((e) => e.name)
  const find = (re: RegExp) => names.find((n) => re.test(n))
  if (engine === 'mysql') {
    const root = find(/ROOT.*PASS|PASS.*ROOT/i)
    if (root) return { root } // для --all-databases нужен root
    const pass = find(/PASS/i), user = find(/USER/i)
    return pass && user ? { user, pass } : null
  }
  if (engine === 'pg') {
    const pass = find(/PASS/i)
    return pass ? { pass, user: find(/USER/i) } : null
  }
  if (engine === 'rabbitmq') {
    const pass = find(/PASS/i), user = find(/USER/i)
    return pass && user ? { user, pass } : null
  }
  if (engine === 'redis') return { pass: find(/PASS/i) } // может быть без пароля
  if (engine === 'ch') return {} // обычно без auth
  return null
}

// rawDump — команда дампа В ФАЙЛ "$RAW" (БЕЗ pipe: при падении set -eu валит Job, а не
// пишет пустой gzip). v — имена env-переменных; отсутствие → плейсхолдер-дефолт движка.
function rawDump(engine: string, host: string, v: CredVars): string {
  switch (engine) {
    case 'mysql': {
      const auth = v.root
        ? `-uroot -p"$${v.root}"`
        : `-u"$${v.user ?? 'MYSQL_USER'}" -p"$${v.pass ?? 'MYSQL_PASSWORD'}"`
      // в образе mariadb:11 бинарь называется mariadb-dump (старый symlink mysqldump убран)
      return `mariadb-dump -h ${host} ${auth} --all-databases --single-transaction --quick --result-file="$RAW"`
    }
    case 'pg':
      return `PGPASSWORD="$${v.pass ?? 'POSTGRES_PASSWORD'}" pg_dumpall -h ${host} -U "$${v.user ?? 'POSTGRES_USER'}" -f "$RAW"`
    case 'ch':
      return `clickhouse-client -h ${host} --query "SELECT create_table_query FROM system.tables WHERE database NOT IN ('system','INFORMATION_SCHEMA','information_schema')" > "$RAW"`
    case 'redis':
      return `redis-cli -h ${host} ${v.pass ? `-a "$${v.pass}" ` : ''}--rdb "$RAW"`
    case 'rabbitmq':
      return `curl -fsS -u "$${v.user ?? 'RABBITMQ_USER'}:$${v.pass ?? 'RABBITMQ_PASS'}" http://${host}:15672/api/definitions -o "$RAW"`
    default:
      return `: > "$RAW"`
  }
}

// credEnvYaml — env-блок контейнера дампа: те же ссылки на секрет, что у пода (secretKeyRef +
// envFrom + plain user/database). '' если ссылок нет (напр. открытый redis).
function credEnvYaml(cred: KubeCred): string {
  let env = ''
  const used = (cred.env ?? []).filter((e) => e.secret || e.value)
  if (used.length) {
    env += '              env:\n'
    for (const e of used) {
      env += `                - name: ${e.name}\n`
      env += e.secret
        ? `                  valueFrom:\n                    secretKeyRef:\n                      name: ${e.secret}\n                      key: ${e.key}\n`
        : `                  value: "${e.value}"\n`
    }
  }
  for (const s of cred.env_from ?? []) {
    if (!env.includes('envFrom:')) env += '              envFrom:\n'
    env += `                - secretRef:\n                    name: ${s}\n`
  }
  return env.replace(/\n$/, '')
}

function oneDumpManifest(
  engine: string, ns: string, host: string, node: string, many: boolean, cred?: KubeCred,
): string {
  // КАЖДАЯ база пишет в СВОЙ каталог. Иначе две задачи чистили бы общий /backup/<движок>
  // по «оставить 2 последних» и стирали дампы друг друга — тихо и без единой ошибки.
  const dir = `/backup/kube/${engine}/${ns}-${host}`
  const img: Record<string, string> = {
    pg: 'postgres:18', mysql: 'mariadb:11', ch: 'clickhouse/clickhouse-server:latest',
    redis: 'redis:8', rabbitmq: 'curlimages/curl:latest',
  }
  const { base, ext } = OUT[engine] ?? { base: 'dump', ext: 'sql' }
  // AUTO: креды из спеки пода определились уверенно → подставляем секрет и реальные имена
  // переменных. Иначе PLACEHOLDER: envFrom-плейсхолдер + дефолтные имена, пользователь вписывает.
  const cv = cred ? credVars(engine, cred) : null
  const auto = cred != null && cv != null
  const raw = rawDump(engine, host, auto ? cv! : {})
  const envYaml = auto ? credEnvYaml(cred!) : ''
  const envBlock = auto
    ? (envYaml || `              # ${tr('секрет не требуется (открытый доступ)')}`)
    : `              envFrom:
                - secretRef:
                    name: <${tr('secret-с-доступом-к-базе')}>   # ← ${tr('ПОДСТАВЬТЕ')}`
  return `# ${tr('Дамп {engine} ({ns}/{host}) → {dir} на ноде {node}.',
    { engine, ns, host, dir, node: node || `<${tr('нода с агентом')}>` })}
# ${tr('Оттуда его заберёт обычный restic-бэкап этой ноды (путь /backup уже в бэкапе).')}
# ${auto
    ? tr('Секрет с доступом к базе подставлен АВТОМАТИЧЕСКИ из спеки пода (те же ссылки, что у самой СУБД — панель значений не читает). Сверьте и примените.')
    : tr('ВАЖНО: подставьте имя Secret\'а с доступом к базе — панель секреты не читает и не хранит.')}${
    many ? `
# ${tr('Это ОДИН из нескольких манифестов: баз этого типа найдено больше одной, у каждой свой CronJob и свой каталог — иначе они затирали бы дампы друг друга.')}` : ''}${
    /-\d+$/.test(host) ? '' : `
# ${tr('Обращаемся к Service «{host}» (имя выведено из имени пода — СВЕРЬТЕ его с kubectl get svc), а не к конкретному поду: у реплик дамп надо снимать один раз. Если у вас primary/replica — укажите здесь Service именно primary.', { host })}`}
apiVersion: batch/v1
kind: CronJob
metadata:
  name: kervax-dump-${engine}-${host}
  namespace: ${ns}
spec:
  schedule: "0 2 * * *"          # ${tr('за час до бэкапа ноды')}
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 2
  failedJobsHistoryLimit: 2
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          nodeName: ${node || `<${tr('нода-с-агентом')}>`}   # ${tr('дамп должен лечь на ноду, где идёт restic')}
          containers:
            - name: dump
              image: ${img[engine]}
${envBlock}
              command: ["/bin/sh", "-c"]
              args:
                - |
                  set -eu
                  mkdir -p ${dir}
                  RAW="${dir}/.raw-$$"
                  # дамп в файл, БЕЗ pipe: при ошибке set -eu валит Job, а не пишет пустой gzip
                  ${raw}
                  gzip -c "$RAW" > "${dir}/${base}-$(date +%F-%H%M%S).${ext}.gz"
                  rm -f "$RAW"
                  # держим 2 последних: историю хранит restic. Чистим ТОЛЬКО свой каталог.
                  ls -1t ${dir}/*.gz | tail -n +3 | xargs -r rm -f
              volumeMounts:
                - name: backup-host
                  mountPath: /backup
          volumes:
            - name: backup-host
              hostPath:
                path: /backup
`
}
