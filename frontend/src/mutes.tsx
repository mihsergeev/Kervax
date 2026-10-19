// Заглушения (мьюты/снузы) — общий код для страниц «Серверы» и «Бэкапы».
//
// Зачем показывать их в списках вообще: временный снуз сам истечёт, а мьют — нет.
// Поставленный полгода назад мьют молча выключает проверку навсегда, и узнать об
// этом можно было, только открыв карточку сервера. Поэтому заглушённое видно в
// списке, причём КАЖДОЕ на своей странице: серверные пороги — в «Серверах»,
// репозитории и покрытие бэкапа — в «Бэкапах» (иначе одна сводка мешала бы всё).
import type { Server } from './api'

export type TFn = (s: string, params?: Record<string, string | number>) => string

export type MuteItem = {
  label: string
  until: string | null // null → бессрочно (само не истечёт — за такими и следим)
  group: MuteGroup
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
// Один контейнер с ключами от хоста, которому они нужны намеренно: dockrisk:<имя>
export const DOCKRISK_MUTE = 'dockrisk:'
// Контейнер или юнит, которому не нужно подниматься после перезагрузки: noboot:<имя>
export const NOBOOT_MUTE = 'noboot:'
// Учетка, которая нужна, хотя в нее давно не входят: access:<пользователь>
export const ACCESS_MUTE = 'access:'
// ОС, которую сейчас не обновить или не перезагрузить: osupd:<вид совета> (eol, reboot, security, apt)
export const OSUPD_MUTE = 'osupd:'
// Решения "так и должно быть": учетка нужна, порт открыт намеренно, ключи от хоста по назначению,
// не нужен после перезагрузки, ОС не обновить. Хранятся в alert_mutes, но это не заглушенный алерт: в
// "Заглушено" и чипе "бессрочно" их нет, видны и снимаются там, где поставлены (раздел карточки, "вернуть")
const DECISIONS = [ACCESS_MUTE, EXPOSED_MUTE, DOCKRISK_MUTE, NOBOOT_MUTE, OSUPD_MUTE]
export const isDecision = (k: string) => DECISIONS.some((p) => k.startsWith(p))
// Кнопка и подпись решения по совету об ОС: что именно нельзя
export function osDecisionLabel(kind: string, t: TFn): string {
  if (kind === 'eol') return t('обновить ОС нельзя')
  if (kind === 'reboot') return t('перезагружать нельзя')
  return t('обновлять нельзя')
}
export function muteKindLabel(k: string, t: TFn): string | null {
  if (k === 'noboot') return t('Не переживет перезагрузку')
  if (k === 'access_audit') return t('Доступы: заброшенные учетки и слабые ключи')
  if (k.startsWith(NOBOOT_MUTE)) return `${t('не нужен после перезагрузки')}: ${k.slice(NOBOOT_MUTE.length)}`
  if (k.startsWith(ACCESS_MUTE)) return `${t('учетка нужна')}: ${k.slice(ACCESS_MUTE.length)}`
  if (k.startsWith(OSUPD_MUTE)) return `${t('Обновления ОС')}: ${osDecisionLabel(k.slice(OSUPD_MUTE.length), t)}`
  if (k.startsWith(DOCKRISK_MUTE)) return `${t('ключи от хоста по назначению')}: ${k.slice(DOCKRISK_MUTE.length)}`
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
  if (k.startsWith(FPM_MUTE)) {
    const [w, p] = k.slice(FPM_MUTE.length).split('|')
    return `${t('пул PHP-FPM')}: ${p ?? ''} (${w})`
  }
  if (k.startsWith(LEAK_MUTE)) return `${t('рост памяти')}: ${k.slice(LEAK_MUTE.length).replace(/^(docker|pod|proc):/, '')}`
  if (k.startsWith(RESTARTS_MUTE)) {
    const key = k.slice(RESTARTS_MUTE.length)
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
      out.push({ label: t('Весь сервер'), until: s.snooze_until, group: 'alert' })
    for (const [k, u] of Object.entries(s.alert_snoozes ?? {}))
      if (new Date(u).getTime() > now) out.push({ label: label(k), until: u, group: 'alert' })
    for (const k of s.alert_mutes ?? []) if (!isDecision(k)) out.push({ label: label(k), until: null, group: 'alert' })
  }
  if (groups.includes('repo'))
    for (const r of s.backup_repo_mutes ?? [])
      out.push({ label: t('репо {n}', { n: r }), until: null, group: 'repo' })
  if (groups.includes('audit'))
    for (const a of s.backup_audit_mutes ?? [])
      out.push({ label: t('покрытие {n}', { n: a }), until: null, group: 'audit' })
  return out
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
  return `${i.label} — ${i.until ? t('ещё {d}', { d: muteLeft(i.until, t) }) : t('бессрочно')}`
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

// Чип в строке списка
export function MuteChip({ items, t }: { items: MuteItem[]; t: TFn }) {
  const perm = items.some((i) => !i.until)
  return (
    <span
      className={`type-chip mute-chip${perm ? ' mute-chip-perm' : ''}`}
      title={items.map((i) => muteText(i, t)).join('\n')}
    >
      🔕 {muteWhen(items, t)}
      {items.length > 1 && ` · ${items.length}`}
    </span>
  )
}

// Сводка над списком: клик открывает карточку, снимают мьют уже там
export function MutesBanner({
  entries,
  t,
  onOpen,
}: {
  entries: { id: number; name: string; items: MuteItem[] }[]
  t: TFn
  onOpen: (id: number) => void
}) {
  if (entries.length === 0) return null
  const total = entries.reduce((a, x) => a + x.items.length, 0)
  return (
    <div className="mutes-banner">
      <span className="mutes-banner-head">🔕 {t('Заглушено ({n})', { n: total })}</span>
      {entries.map(({ id, name, items }) => (
        <button
          key={id}
          className="mute-sum-chip"
          title={items.map((i) => muteText(i, t)).join('\n')}
          onClick={() => onOpen(id)}
        >
          <b>{name}</b>
          <span className="mute-sum-what">{muteBrief(items, t)}</span>
          <span
            className={`mute-sum-when${items.some((i) => !i.until) ? ' mute-sum-perm' : ''}`}
          >
            {muteWhen(items, t)}
          </span>
        </button>
      ))}
    </div>
  )
}
