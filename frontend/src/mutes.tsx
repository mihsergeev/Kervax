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
  { k: 'reboot', label: 'Перезагрузка' },
  { k: 'oom', label: 'OOM-killer' },
]

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
  backup_dump: 'Бэкап: дамп СУБД',
  backup_dump_space: 'Бэкап: место под дампы',
  backup_cron: 'Бэкап: дамп-CronJob',
  clock: 'Время сервера',
}

// Всё заглушённое по серверу. groups сужает до «своей» страницы.
export function collectMutes(
  s: Server,
  t: TFn,
  groups: MuteGroup[] = ['alert', 'repo', 'audit'],
  now = Date.now(),
): MuteItem[] {
  const label = (k: string) =>
    t(SNOOZE_KINDS.find((x) => x.k === k)?.label ?? RULE_KIND_LABELS[k] ?? k)
  const out: MuteItem[] = []
  if (groups.includes('alert')) {
    if (s.snooze_until && new Date(s.snooze_until).getTime() > now)
      out.push({ label: t('Весь сервер'), until: s.snooze_until, group: 'alert' })
    for (const [k, u] of Object.entries(s.alert_snoozes ?? {}))
      if (new Date(u).getTime() > now) out.push({ label: label(k), until: u, group: 'alert' })
    for (const k of s.alert_mutes ?? []) out.push({ label: label(k), until: null, group: 'alert' })
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
