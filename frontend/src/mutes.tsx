// Заглушения (мьюты/снузы) — общий код для страниц «Серверы» и «Бэкапы».
//
// Зачем показывать их в списках вообще: временный снуз сам истечёт, а мьют — нет.
// Поставленный полгода назад мьют молча выключает проверку навсегда, и узнать об
// этом можно было, только открыв карточку сервера. Поэтому заглушённое видно в
// списке, причём КАЖДОЕ на своей странице: серверные пороги — в «Серверах»,
// репозитории и покрытие бэкапа — в «Бэкапах» (иначе одна сводка мешала бы всё).
import {
  backupAuditMute,
  backupRepoMute,
  snoozeServer,
  snoozeServerAlert,
  updateServer,
  type Server,
} from './api'
import { shortList } from './serverUtils'

export type TFn = (s: string, params?: Record<string, string | number>) => string

export type MuteItem = {
  label: string
  until: string | null // null → бессрочно (само не истечёт — за такими и следим)
  group: MuteGroup
  // что снимать: ключ в alert_mutes или alert_snoozes, имя репо, ключ покрытия; '' - снуз всего сервера
  key: string
}

export type MuteGroup = 'alert' | 'repo' | 'audit'

export const SRV_ALERT_KINDS: { k: string; label: string }[] = [
  { k: 'offline', label: 'Недоступен' },
  { k: 'cpu', label: 'CPU' },
  { k: 'mem', label: 'RAM' },
  { k: 'disk', label: 'Диск' },
  { k: 'temp', label: 'Температура CPU' },
  { k: 'throttle', label: 'Троттлинг CPU' },
  { k: 'conntrack', label: 'Conntrack' },
  { k: 'disktemp', label: 'Температура диска' },
  { k: 'disk_health', label: 'Диск: поломка' },
  { k: 'inode', label: 'Диск: inode' },
  { k: 'disk_forecast', label: 'Диск: скоро заполнится' },
  { k: 'units', label: 'Systemd: упавшие юниты' },
  { k: 'unit_loop', label: 'Systemd: юниты в цикле перезапусков' },
  { k: 'docker_restarts', label: 'Docker: часто перезапускается' },
  { k: 'kube_restarts', label: 'Kubernetes: под часто перезапускается' },
  { k: 'mem_leak', label: 'Память: растет день за днем' },
  { k: 'ssh_risk', label: 'SSH: опасный вход' },
  { k: 'db_health', label: 'СУБД: реплика, транзакции, запись, VACUUM' },
  { k: 'web_config', label: 'Веб-сервер: конфиг не проходит проверку' },
  { k: 'web_cert', label: 'Веб-сервер: сертификат истекает' },
  { k: 'listen_gone', label: 'Сервер: пропал порт' },
  { k: 'disk_slow', label: 'Диск тормозит' },
  { k: 'gpu', label: 'GPU: драйвер, пропала карта, ошибки Xid' },
  { k: 'gpu_heat', label: 'GPU: перегрев' },
  { k: 'llm', label: 'LLM: не отвечает, очередь, задержки' },
  { k: 'queue_stuck', label: 'Очередь растет и не разбирается' },
  { k: 'queue_failed', label: 'Всплеск упавших задач' },
  { k: 'workers', label: 'Воркер упал или падает и поднимается' },
  { k: 'cron_stuck', label: 'Крон наслаивается или висит' },
  { k: 'log_errors', label: 'Всплеск ошибок в логах' },
  { k: 'fpm', label: 'PHP-FPM не справляется' },
  { k: 'cpu_spin', label: 'CPU: процессы в пустом цикле' },
  { k: 'backup_growth', label: 'Бэкап: резкий прирост' },
  { k: 'docker_sock', label: 'Docker: внешний прокси с docker-сокетом' },
  { k: 'docker_risk', label: 'Docker: контейнер с ключами от хоста' },
  { k: 'access_risk', label: 'Доступ: второй root или пустой пароль' },
  { k: 'exposed', label: 'Сеть: открыто в интернет то, что должно быть закрыто' },
  { k: 'port_new', label: 'Сеть: открылся новый порт' },
  { k: 'pressure', label: 'Сервер задыхается' },
  { k: 'net_health', label: 'Сеть: потери и ошибки' },
  { k: 'link', label: 'Сеть: связь между серверами' },
  { k: 'traffic', label: 'Сеть: лимит трафика тарифа' },
  { k: 'access_change', label: 'Сервер: изменился доступ' },
  { k: 'reboot', label: 'Перезагрузка' },
  { k: 'oom', label: 'OOM-killer' },
  { k: 'web_5xx', label: 'Веб: ошибки 5xx' },
  { k: 'kube_pod', label: 'Kubernetes: поды' },
  { k: 'bogus_requests', label: 'Левые запросы в панель' },
]

// Заглушенные 5xx одного лога (домена): ключ web_5xx:<ключ лога>. Подпись - с самим
// логом, иначе в списке заглушенного был бы голый технический ключ.
export const WEB_MUTE = 'web_5xx:'
// Один упавший юнит systemd: unit:<имя>, остальные юниты ноды алертят
export const UNIT_MUTE = 'unit:'
// Порт, открытый в интернет намеренно: exposed:<порт>
export const EXPOSED_MUTE = 'exposed:'
// Один контейнер или под, который часто перезапускается: restarts:docker:<имя> | restarts:pod:<ns>/<имя>
export const RESTARTS_MUTE = 'restarts:'
// Порог предупреждения "часто перезапускается": перезапусков за сутки (restarts.MIN на бэкенде)
export const RESTARTS_DAY_MIN = 5
// Один контейнер, под или процесс, чья память растет: leak:docker:<имя> | leak:pod:<ns>/<имя> | leak:proc:<имя>@<польз.>
export const LEAK_MUTE = 'leak:'
// Один пропавший порт: listen:<порт>
export const LISTEN_MUTE = 'listen:'
// Одна очередь, которая растет и не разбирается: qstuck:<источник>|<vhost>/<имя> (ключ порога очереди)
export const QSTUCK_MUTE = 'qstuck:'
// Всплеск упавших задач одного брокера очередей: qfail:<источник брокера>
export const QFAIL_MUTE = 'qfail:'
// Один воркер под supervisor или pm2: worker:<демон, где>|<программа>
export const WORKER_MUTE = 'worker:'
// Одна задача cron: cron:<где>|<команда>
export const CRON_MUTE = 'cron:'
// Всплеск ошибок в одном логе контейнера или пода: logerr:<где>|<контейнер пода>
export const LOGERR_MUTE = 'logerr:'
// Один пул PHP-FPM: fpm:<где>|<пул>
export const FPM_MUTE = 'fpm:'
// OOM из-за одного виновника (кто занимал память): oom:svc:<служба> | oom:ct:<контейнер> | oom:pod:<uid> |
// oom:cg:<cgroup> | oom:proc:<процесс>; OOM из-за других приходят. "oom" целиком - все OOM сервера
export const OOM_MUTE = 'oom:'
// gen - после "из-за": из-за контейнера web, из-за процесса java
export function oomCulpritLabel(key: string, t: TFn, gen = false): string {
  const i = key.indexOf(':')
  const kind = key.slice(0, i)
  const name = key.slice(i + 1)
  if (kind === 'ct') return t(gen ? 'контейнера {n}' : 'контейнер {n}', { n: name })
  // до 1.5.2 под глушили по uid, теперь по ворклоаду
  if (kind === 'pod') return t(gen ? 'пода {n}' : 'под {n}', { n: /^[0-9a-f]{8}-[0-9a-f]{4}-/.test(name) ? name.slice(0, 8) : name })
  if (kind === 'proc') return t(gen ? 'процесса {n}' : 'процесс {n}', { n: name })
  return name
}
// Один контейнер с ключами от хоста, которому они нужны намеренно: dockrisk:<имя>
export const DOCKRISK_MUTE = 'dockrisk:'
// Контейнер или юнит, которому не нужно подниматься после перезагрузки: noboot:<имя>
export const NOBOOT_MUTE = 'noboot:'
// Учетка, которая нужна, хотя в нее давно не входят: access:<пользователь>
export const ACCESS_MUTE = 'access:'
// ОС, которую сейчас не обновить или не перезагрузить: osupd:<вид совета> (eol, reboot, security, apt)
export const OSUPD_MUTE = 'osupd:'
// Версия ПО, которую сейчас не обновить: swver:<состояние>:<продукт>-<цикл>. У "скоро" - отложить до
// конца поддержки: когда она кончится, у пункта другое состояние, и он вернется сам
export const SWVER_MUTE = 'swver:'
// SSH пускает по паролю, и так нужно: ssh:<вид совета> (ssh_password)
export const SSH_MUTE = 'ssh:'
// Старый образ внешнего прокси, который решили не обновлять: proxyold:<контейнер>
export const PROXYOLD_MUTE = 'proxyold:'
// Решения "так и должно быть": учетка нужна, порт открыт намеренно, ключи от хоста по назначению,
// не нужен после перезагрузки, ОС или ПО не обновить, вход по паролю нужен, старый прокси оставить.
// Хранятся в alert_mutes, но это не заглушенный алерт: в "Заглушено" и чипе "бессрочно" их нет, видны
// и снимаются там, где поставлены (раздел карточки, "вернуть")
const DECISIONS = [ACCESS_MUTE, EXPOSED_MUTE, DOCKRISK_MUTE, NOBOOT_MUTE, OSUPD_MUTE, SWVER_MUTE, SSH_MUTE, PROXYOLD_MUTE]
export const isDecision = (k: string) => DECISIONS.some((p) => k.startsWith(p))
// Кнопка и подпись решения по совету об ОС: что именно нельзя
export function osDecisionLabel(kind: string, t: TFn): string {
  if (kind === 'eol') return t('обновить ОС нельзя')
  if (kind === 'reboot') return t('перезагружать нельзя')
  return t('обновлять нельзя')
}
// Кнопка (done=false) и подпись (done=true) решения по версии ПО
export function swDecisionLabel(state: string, done: boolean, t: TFn): string {
  if (state === 'soon') return done ? t('отложено до конца поддержки') : t('отложить до конца поддержки')
  return t('обновить нельзя')
}
// Бессрочный мьют, который снимет сама панель: поломка диска - после замены диска и сборки массива
// (app/disk_mute.py). Подпись вместо "бессрочно", иначе он выглядит забытым навсегда
export function muteAutoNote(k: string, t: TFn): string | null {
  return k === 'disk_health' ? t('снимется сам после замены диска и сборки массива') : null
}
export function muteKindLabel(k: string, t: TFn): string | null {
  if (k === 'noboot') return t('Не переживет перезагрузку')
  if (k === 'access_audit') return t('Доступы: заброшенные учетки и слабые ключи')
  if (k.startsWith(NOBOOT_MUTE)) return `${t('не нужен после перезагрузки')}: ${k.slice(NOBOOT_MUTE.length)}`
  if (k.startsWith(ACCESS_MUTE)) return `${t('учетка нужна')}: ${k.slice(ACCESS_MUTE.length)}`
  if (k.startsWith(OSUPD_MUTE)) return `${t('Обновления ОС')}: ${osDecisionLabel(k.slice(OSUPD_MUTE.length), t)}`
  if (k.startsWith(SWVER_MUTE)) {
    const [st, what] = k.slice(SWVER_MUTE.length).split(':')
    return `${t('Версии ПО')}: ${what ?? ''} (${swDecisionLabel(st, true, t)})`
  }
  if (k.startsWith(SSH_MUTE)) return `SSH: ${t('вход по паролю нужен')}`
  if (k.startsWith(DOCKRISK_MUTE)) return `${t('ключи от хоста по назначению')}: ${k.slice(DOCKRISK_MUTE.length)}`
  if (k.startsWith(PROXYOLD_MUTE)) return `${t('старый образ прокси оставлен')}: ${k.slice(PROXYOLD_MUTE.length)}`
  if (k.startsWith(LISTEN_MUTE)) return `${t('пропавший порт')}: ${k.slice(LISTEN_MUTE.length)}`
  if (k.startsWith(QSTUCK_MUTE)) {
    const [src, q] = k.slice(QSTUCK_MUTE.length).split('|')
    return `${t('очередь')}: ${(q ?? '').replace(/^\/\//, '').replace(/^[^/]*\//, '')}${src ? ` (${src})` : ''}`
  }
  if (k.startsWith(QFAIL_MUTE)) return `${t('упавшие задачи')}: ${k.slice(QFAIL_MUTE.length)}`
  if (k.startsWith(WORKER_MUTE)) {
    const [src, name] = k.slice(WORKER_MUTE.length).split('|')
    return `${t('воркер')}: ${name ?? ''}${src ? ` (${src})` : ''}`
  }
  if (k.startsWith(CRON_MUTE)) {
    const key = k.slice(CRON_MUTE.length)
    const i = key.indexOf('|')
    return `${t('крон')}: ${i < 0 ? key : `${key.slice(i + 1)} (${key.slice(0, i)})`}`
  }
  if (k.startsWith(LOGERR_MUTE)) {
    const [w, c] = k.slice(LOGERR_MUTE.length).split('|')
    return `${t('ошибки в логе')}: ${w}${c ? ` (${c})` : ''}`
  }
  if (k.startsWith(OOM_MUTE)) return `${t('OOM из-за')}: ${oomCulpritLabel(k.slice(OOM_MUTE.length), t)}`
  if (k.startsWith(FPM_MUTE)) {
    const [w, p] = k.slice(FPM_MUTE.length).split('|')
    return `${t('пул PHP-FPM')}: ${p ?? ''} (${w})`
  }
  if (k.startsWith(LEAK_MUTE)) return `${t('рост памяти')}: ${k.slice(LEAK_MUTE.length).replace(/^(docker|pod|proc):/, '')}`
  if (k.startsWith(RESTARTS_MUTE)) {
    const key = k.slice(RESTARTS_MUTE.length)
    if (key === 'bursts') return t('поды перезапускаются пачками')
    return key.startsWith('pod:')
      ? `${t('перезапуски пода')}: ${key.slice(4)}`
      : `${t('перезапуски контейнера')}: ${key.replace(/^docker:/, '')}`
  }
  if (k.startsWith(UNIT_MUTE)) return `${t('юнит')}: ${k.slice(UNIT_MUTE.length)}`
  if (k.startsWith(EXPOSED_MUTE)) return `${t('открытый порт')}: ${k.slice(EXPOSED_MUTE.length)}`
  return k.startsWith(WEB_MUTE) ? `${t('5xx по логу')}: ${k.slice(WEB_MUTE.length)}` : null
}

// Варианты для ВРЕМЕННОГО приглушения. Отличаются от списка выше уровнями диска:
// «@N» = молчать про уровни до N включительно (см. _muted в коллекторе). Постоянно
// уровень выключается порогом (0), поэтому в форме мьютов этих вариантов нет — иначе
// одно и то же настраивалось бы двумя способами.
export const SNOOZE_KINDS: { k: string; label: string }[] = SRV_ALERT_KINDS.flatMap((x) =>
  x.k === 'disk'
    ? [
        x,
        { k: 'disk@1', label: 'Диск — только предупреждения' },
        { k: 'disk@2', label: 'Диск — предупр. и проблемы' },
      ]
    : [x],
)

// Подписи типов, которые заглушаются правилами алертов: в SNOOZE_KINDS их нет (там
// только пороговые метрики), а в alert_mutes они попасть могут — без этой таблицы
// светился бы голый ключ вроде «backup_dump».
const RULE_KIND_LABELS: Record<string, string> = {
  docker_down: 'Docker: контейнер упал',
  docker_loop: 'Docker: перезапуски',
  backup_missing: 'Бэкап: не настроен',
  backup_failed: 'Бэкап: ошибка',
  backup_stale: 'Бэкап: не свежий',
  backup_repo: 'Бэкап-сервер: репозитории',
  backup_lock: 'Бэкап-сервер: репозиторий залочен',
  backup_rotation: 'Бэкап-сервер: ротация встала',
  backup_prune: 'Бэкап-сервер: чистка не работает',
  backup_check: 'Бэкап-сервер: проверка репозитория не прошла',
  backup_unmonitored: 'Бэкап-сервер: клиент без агента',
  dns: 'DNS на ноде',
  backup_dump: 'Бэкап: дамп СУБД',
  backup_dump_space: 'Бэкап: место под дампы',
  backup_cron: 'Бэкап: дамп-CronJob',
  backup_custom: 'Бэкап: свои бэкапы',
  clock: 'Время сервера',
  // глушатся с главной кнопкой 🔕 у строки "Что сломано"
  'dns@1': 'DNS - только предупреждения',
  'clock@1': 'Время сервера - только предупреждения',
  'clock@2': 'Время сервера - предупр. и проблемы',
  db_conn: 'Коннекты СУБД',
  queue: 'Очереди: выше порога',
  kube_expiry: 'Kubernetes: сроки',
  kube_cron: 'Kubernetes: CronJob',
  kube_node: 'Kubernetes: ноды',
  kube_job: 'Kubernetes: упавшие Job',
  backup_restore: 'Бэкап: проверка восстановления',
  flux_down: 'Flux: доставка встала',
  flux_stale: 'Flux: репозиторий чартов недоступен',
}

// Алерты подов, Job и CronJob кластера: пока идет разработка, поды пересоздаются и Job падают,
// их глушат разом из карточки кластера. Ноды, сроки и Flux - инфраструктура, они остаются
export const KUBE_WORK_KINDS = ['kube_pod', 'kube_restarts', 'kube_job', 'kube_cron']

// Подпись вида алерта: свои у ключей с хвостом, дальше пороговые виды и виды правил
export function kindLabel(k: string, t: TFn): string {
  return muteKindLabel(k, t) ?? t(SNOOZE_KINDS.find((x) => x.k === k)?.label ?? RULE_KIND_LABELS[k] ?? k)
}

// Всё заглушённое по серверу. groups сужает до «своей» страницы.
export function collectMutes(
  s: Server,
  t: TFn,
  groups: MuteGroup[] = ['alert', 'repo', 'audit'],
  now = Date.now(),
): MuteItem[] {
  const label = (k: string) => kindLabel(k, t)
  const out: MuteItem[] = []
  if (groups.includes('alert')) {
    if (s.snooze_until && new Date(s.snooze_until).getTime() > now)
      out.push({ label: t('Весь сервер'), until: s.snooze_until, group: 'alert', key: '' })
    for (const [k, u] of Object.entries(s.alert_snoozes ?? {}))
      if (new Date(u).getTime() > now) out.push({ label: label(k), until: u, group: 'alert', key: k })
    for (const k of s.alert_mutes ?? [])
      if (!isDecision(k)) out.push({ label: label(k), until: null, group: 'alert', key: k })
  }
  if (groups.includes('repo'))
    for (const r of s.backup_repo_mutes ?? [])
      out.push({ label: t('репо {n}', { n: r }), until: null, group: 'repo', key: r })
  if (groups.includes('audit'))
    for (const a of s.backup_audit_mutes ?? [])
      out.push({ label: t('покрытие {n}', { n: a }), until: null, group: 'audit', key: a })
  return out
}

// Пункт общего списка заглушенного (панель "Заглушено"): мьют и его сервер
export type MuteRow = MuteItem & { sid: number; server: string }

export function muteRows(servers: Server[], t: TFn, groups: MuteGroup[]): MuteRow[] {
  return servers.flatMap((s) => collectMutes(s, t, groups).map((i) => ({ ...i, sid: s.id, server: s.name })))
}

// Порядок внутри сервера: бессрочные первыми (сами не истекут), дальше то, что истечет раньше
export function permFirst(a: MuteItem, b: MuteItem): number {
  if (!a.until !== !b.until) return a.until ? 1 : -1
  return a.until && b.until ? Date.parse(a.until) - Date.parse(b.until) : a.label.localeCompare(b.label)
}

// Истечет в ближайшие сутки
export const muteSoon = (i: MuteItem, now = Date.now()) => !!i.until && Date.parse(i.until) - now < 86400_000

// Снять заглушку из списка теми же ручками, что в карточке: бессрочную - из alert_mutes, со
// сроком - снузом на 0 часов, репо и покрытие бэкапа - своими. s - свежее состояние сервера:
// alert_mutes сохраняется целиком
export function liftMute(s: Server, i: MuteItem): Promise<Server> {
  if (i.group === 'repo') return backupRepoMute(s.id, i.key, false)
  if (i.group === 'audit') return backupAuditMute(s.id, i.key, false)
  if (i.key === '') return snoozeServer(s.id, 0)
  if (i.until) return snoozeServerAlert(s.id, i.key, 0)
  return updateServer(s.id, { alert_mutes: (s.alert_mutes ?? []).filter((k) => k !== i.key) })
}

// Вернуть только что снятое ("вернуть" в панели): снуз - на оставшееся до прежнего срока время,
// истекший к этому моменту не возвращается (он бы уже и так кончился)
export async function restoreMute(s: Server, i: MuteItem): Promise<Server> {
  if (i.group === 'repo') return backupRepoMute(s.id, i.key, true)
  if (i.group === 'audit') return backupAuditMute(s.id, i.key, true)
  if (!i.until) return updateServer(s.id, { alert_mutes: [...new Set([...(s.alert_mutes ?? []), i.key])] })
  const hours = Math.min(24 * 30, (Date.parse(i.until) - Date.now()) / 3600_000)
  if (hours <= 0) return s
  return i.key === '' ? snoozeServer(s.id, hours) : snoozeServerAlert(s.id, i.key, hours)
}

// Серверы со своими мьютами: больше всего заглушено - первым, дальше по имени
export function mutesByServer(rows: MuteRow[]): { sid: number; server: string; items: MuteRow[] }[] {
  const m = new Map<number, { sid: number; server: string; items: MuteRow[] }>()
  for (const r of rows) {
    if (!m.has(r.sid)) m.set(r.sid, { sid: r.sid, server: r.server, items: [] })
    m.get(r.sid)!.items.push(r)
  }
  const out = [...m.values()]
  for (const g of out) g.items.sort(permFirst)
  return out.sort((a, b) => b.items.length - a.items.length || a.server.localeCompare(b.server))
}

// Подпись плитки: на каких серверах (первые два по числу мьютов) и сколько само не истечет
export function mutesTileSub(rows: MuteRow[], t: TFn): { where: string; when: string; all: string } {
  if (!rows.length) return { where: t('ничего не заглушено'), when: '', all: '' }
  const names = mutesByServer(rows).map((g) => g.server)
  const perm = rows.filter((r) => !r.until).length
  const soon = rows.filter((r) => muteSoon(r)).length
  const when = [
    perm > 0 ? t('бессрочно {n}', { n: perm }) : '',
    soon > 0 ? t('истекут за сутки {n}', { n: soon }) : '',
  ]
    .filter(Boolean)
    .join(', ')
  return { where: shortList(names, t, 2), when, all: names.join(', ') }
}

// «на сколько ещё»: минуты → часы → дни, без ложной точности
export function muteLeft(until: string, t: TFn): string {
  const m = Math.max(1, Math.round((new Date(until).getTime() - Date.now()) / 60000))
  if (m < 60) return t('{n} мин', { n: m })
  const h = Math.round(m / 60)
  return h < 48 ? t('{n} ч', { n: h }) : t('{n} дн', { n: Math.round(h / 24) })
}

// Строка «что и насколько» — для подсказки
export function muteText(i: MuteItem, t: TFn): string {
  return `${i.label} — ${i.until ? t('ещё {d}', { d: muteLeft(i.until, t) }) : (i.group === 'alert' && muteAutoNote(i.key, t)) || t('бессрочно')}`
}

// Короткая подпись для сводки. Типы алертов перечисляем поимённо (их единицы), а репо
// и пункты покрытия сворачиваем в счётчик: на серверах их по 5-6, полным списком они
// затопили бы главное. Полный состав — в подсказке.
export function muteBrief(items: MuteItem[], t: TFn): string {
  const parts = items.filter((i) => i.group === 'alert').map((i) => i.label)
  const n = (g: MuteGroup) => items.filter((i) => i.group === g).length
  if (n('repo')) parts.push(t('репо ×{n}', { n: n('repo') }))
  if (n('audit')) parts.push(t('покрытие ×{n}', { n: n('audit') }))
  return parts.join(', ')
}

// самый поздний срок среди временных (null, если все бессрочные)
function latestUntil(items: MuteItem[]): string | null {
  const t = items
    .filter((i) => i.until)
    .sort((a, b) => new Date(b.until!).getTime() - new Date(a.until!).getTime())[0]
  return t?.until ?? null
}

// «бессрочно» / «ещё 3 ч» — бессрочное приоритетнее: срок снуза истечёт сам
function muteWhen(items: MuteItem[], t: TFn): string {
  if (items.some((i) => !i.until)) return t('бессрочно')
  const u = latestUntil(items)
  return u ? t('ещё {d}', { d: muteLeft(u, t) }) : ''
}

// Чип в строке списка: словом, без эмодзи, как остальные чипы строки
export function MuteChip({ items, t }: { items: MuteItem[]; t: TFn }) {
  const perm = items.some((i) => !i.until)
  return (
    <span
      className={`type-chip mute-chip${perm ? ' mute-chip-perm' : ''}`}
      title={items.map((i) => muteText(i, t)).join('\n')}
    >
      {t('заглушено')}
      <span className="mute-chip-when"> {muteWhen(items, t)}</span>
      {items.length > 1 && ` · ${items.length}`}
    </span>
  )
}
