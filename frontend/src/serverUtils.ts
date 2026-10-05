import type { DiskHealth, DiskUsage, Server, ServerReport, UnitsBlock, WebRate } from './api'
import { tr } from './i18n'

// Запросы в минуту по access-логам ноды: блок кладёт root-хелпер webserver-setup, агент
// отдаёт его как есть. Протухший блок (хелпер встал, логи не крутятся) не показываем:
// вчерашний поток выглядел бы как сегодняшний.
export function webRate(r?: ServerReport | null): WebRate | null {
  const w = r?.extras?.['web-rate']
  if (!w || !w.ts || typeof w.rpm !== 'number') return null
  // возраст меряем часами самой ноды (clock_unix): её часы бывают сдвинуты, и живые
  // данные из-за этого пропадали бы
  const nowTs = r?.clock_unix || Date.now() / 1000
  return nowTs - w.ts > 900 ? null : w
}

// Разбор места от helper'а diskusage-setup. Блок обновляется раз в 10 минут, пока раздел
// заполняется; старше двух часов - helper встал, старый разбор за свежий не выдаем.
// Возраст - по часам самой ноды, как у блока веба.
export function diskUsage(r?: ServerReport | null): DiskUsage | null {
  const d = r?.extras?.['disk-usage']
  if (!d || !d.ts || !Array.isArray(d.fs) || !d.fs.length) return null
  const nowTs = r?.clock_unix || Date.now() / 1000
  return nowTs - d.ts > 7200 ? null : d
}

// Здоровье дисков от helper'а diskhealth-setup: блок обновляется раз в 2 минуты, старше
// получаса - helper встал, и показывать вчерашний "все в порядке" нельзя.
export function diskHealth(r?: ServerReport | null): DiskHealth | null {
  const d = r?.extras?.['disk-health']
  if (!d || d.v !== 1 || !d.ts) return null
  const nowTs = r?.clock_unix || Date.now() / 1000
  return nowTs - d.ts > 1800 ? null : d
}

// Упавшие юниты от helper'а units-setup: блок обновляется раз в минуту, старше 10 минут -
// helper встал, старый список за свежий не выдаем.
export function unitsBlock(r?: ServerReport | null): UnitsBlock | null {
  const u = r?.extras?.units
  if (!u || u.v !== 1 || !u.ts || !Array.isArray(u.units)) return null
  const nowTs = r?.clock_unix || Date.now() / 1000
  return nowTs - u.ts > 600 ? null : u
}

// Подпись лога - как у панели (web_log_label): под kubernetes, "контейнер (домены +N)",
// домены, имя или файл. Несколько доменов в одной строке - это один nginx: все свои
// сайты он пишет в общий access-лог, и разделить их можно, только если в формате лога
// есть $host. По этой подписи строки под графиком находят цвет своей полосы.
export function webLogLabel(l: { log: string; name?: string; sites?: string[] }): string {
  let name = l.name ?? ''
  const sites = (l.sites ?? []).filter(Boolean)
  if (name.includes('/')) {
    // под kubernetes - по контроллеру, с доменами его nginx (у ingress-nginx - хосты Ingress)
    name = name.match(POD_RE)?.[1] ?? name
    if (!sites.length) return name
  }
  if (sites.length) {
    const doms = sites.slice(0, 2).join(', ') + (sites.length > 2 ? ` +${sites.length - 2}` : '')
    return (name ? `${name} (${doms})` : doms).slice(0, 120)
  }
  return (name || l.log.split('/').pop() || l.log).slice(0, 120)
}

// Под какой полосой графика идет лог: поды одного деплоймента - одна полоса (как
// web_breakdown на бэкенде). Хвосты пода kubernetes - из алфавита без гласных.
const K8S_RAND = '[bcdfghjklmnpqrstvwxz2456789]'
const POD_RE = new RegExp(`^([a-z0-9.-]+/.+?)(?:-${K8S_RAND}{6,10})?-${K8S_RAND}{5}$`)
export function webSeriesName(label: string): string {
  if (!label.includes('/') || label.includes('(')) return label
  return label.match(POD_RE)?.[1] ?? label
}

// Строк в минуту, где код ответа не распознан. Без этого «5xx: 0» значило бы и «ошибок
// нет», и «формат лога незнакомый» - а это противоположные вещи.
export function webUnparsed(r?: ServerReport | null): number {
  return (webRate(r)?.logs ?? []).reduce((a, l) => a + (l.un ?? 0), 0)
}

// --- метрики сервера из последнего снимка (для сортировки/группировки/сводки) ---

export function srvCpuPct(s: Server): number | null {
  const c = s.last_report?.cpu_percent
  return c != null ? Math.round(c) : null
}
export function srvRamPct(s: Server): number | null {
  const r = s.last_report
  return r?.mem_total ? Math.round(((r.mem_used ?? 0) / r.mem_total) * 100) : null
}
export function srvDiskPct(s: Server): number | null {
  const d = s.last_report?.disks
  return d?.length
    ? Math.max(...d.filter((x) => x.total).map((x) => Math.round((x.used / x.total) * 100)))
    : null
}

// sec — раздел детали сервера (msec-<sec>), куда ведёт клик по 🔥. Те же имена,
// что в диплинках алертов (_SRV_SECTION в коллекторе), чтобы поведение совпадало.
// mute — ключ для быстрого приглушения ИМЕННО этого сигнала (disk@1 = только предупр.
// диска, крит останется). Панель шлёт его в alert_mutes.
// since - с какого момента проблема видна (ISO): для "Что сломано" - "2 дн"
export type SrvIssue = {
  tone: 't-down' | 't-degraded'; text: string; sec?: string; mute?: string; kind?: string; since?: string | null
}

// Сколько длится проблема, коротко: "40 мин", "5 ч", "3 дн". Меньше минуты - пусто: только что
// появившееся длительностью не подписываем.
export function sinceShort(iso: string | null | undefined, t: (k: string, p?: Record<string, string | number>) => string): string {
  if (!iso) return ''
  const sec = (Date.now() - Date.parse(iso)) / 1000
  if (!Number.isFinite(sec) || sec < 60) return ''
  if (sec < 3600) return t('{n} мин', { n: Math.floor(sec / 60) })
  if (sec < 48 * 3600) return t('{n} ч', { n: Math.floor(sec / 3600) })
  return t('{n} дн', { n: Math.floor(sec / 86400) })
}

// Проблемы сервера: оффлайн / CPU / RAM / диск (warn=degraded, ≥alert/crit=down).
// Заглушён ли сигнал (повторяет бэкендовый _muted): базовый ключ `disk` глушит все
// уровни; `disk@N` — уровни ≤ N. Без mute-ключа сигнал не приглушается. Нужно, чтобы
// приглушённое колокольчиком РЕАЛЬНО уходило с главной (иначе «жму — ничего не происходит»).
function srvIssueMuted(i: SrvIssue, mutes: Set<string>): boolean {
  if (!i.mute) return false
  const at = i.mute.indexOf('@')
  const base = at >= 0 ? i.mute.slice(0, at) : i.mute
  const level = at >= 0 ? parseInt(i.mute.slice(at + 1), 10) : 3
  if (mutes.has(base)) return true
  for (const m of mutes) {
    if (!m.startsWith(base + '@')) continue
    const n = parseInt(m.slice(base.length + 1), 10)
    if (!Number.isNaN(n) && level <= n) return true
  }
  return false
}

export function srvIssues(
  s: Server,
  t: (k: string, p?: Record<string, string | number>) => string,
): SrvIssue[] {
  const mutes = new Set(s.alert_mutes ?? [])
  const keep = (arr: SrvIssue[]) => arr.filter((i) => !srvIssueMuted(i, mutes))
  if (!s.online)
    return keep([{ tone: 't-down', text: t('оффлайн'), mute: 'offline', kind: 'offline', since: s.last_seen }])
  const out: SrvIssue[] = []
  const cpu = srvCpuPct(s)
  if (cpu != null && s.cpu_alert_percent && cpu >= s.cpu_alert_percent)
    out.push({ tone: 't-down', text: `CPU ${cpu}%`, sec: 'cpu', mute: 'cpu', kind: 'cpu' })
  const ram = srvRamPct(s)
  if (ram != null && s.mem_alert_percent && ram >= s.mem_alert_percent)
    out.push({ tone: 't-down', text: `RAM ${ram}%`, sec: 'mem', mute: 'mem', kind: 'mem' })
  const disk = srvDiskPct(s)
  if (disk != null) {
    if (s.disk_crit_percent && disk >= s.disk_crit_percent)
      out.push({ tone: 't-down', text: `${t('Диск')} ${disk}% 🚨`, sec: 'diskfill', mute: 'disk', kind: 'disk' })
    else if (s.disk_alert_percent && disk >= s.disk_alert_percent)
      out.push({ tone: 't-down', text: `${t('Диск')} ${disk}%`, sec: 'diskfill', mute: 'disk@2', kind: 'disk' })
    else if (s.disk_warn_percent && disk >= s.disk_warn_percent)
      out.push({ tone: 't-degraded', text: `${t('Диск')} ${disk}%`, sec: 'diskfill', mute: 'disk@1', kind: 'disk' })
  }
  const r = s.last_report
  // температура CPU (на VM датчика нет → cpu_temp = null)
  if (r?.cpu_temp != null && s.temp_alert_c && r.cpu_temp >= s.temp_alert_c)
    out.push({ tone: 't-down', text: `CPU ${Math.round(r.cpu_temp)}°C`, sec: 'temp', kind: 'temp' })
  // троттлинг CPU
  if (r?.cpu_throttle != null && r.cpu_throttle > 0)
    out.push({ tone: 't-degraded', text: t('троттлинг'), sec: 'throttle', kind: 'throttle' })
  // conntrack близок к пределу
  const ctmax = r?.conntrack_max ?? 0
  if (ctmax > 0 && s.conntrack_alert_percent) {
    const fill = Math.round(((r?.conntrack_count ?? 0) / ctmax) * 100)
    if (fill >= s.conntrack_alert_percent)
      out.push({ tone: 't-down', text: `conntrack ${fill}%`, sec: 'conntrack', kind: 'conntrack' })
  }
  // температура диска (макс по устройствам с датчиком)
  if (s.disk_temp_alert_c && r?.disk_devs?.length) {
    const temps = r.disk_devs.map((d) => d.temp).filter((x): x is number => x != null)
    if (temps.length && Math.max(...temps) >= s.disk_temp_alert_c)
      out.push({ tone: 't-down', text: `${t('Диск')} ${Math.round(Math.max(...temps))}°C`, sec: 'disktemp', kind: 'disktemp' })
  }
  // новые проверки (поломки дисков, прогноз заполнения, inode, упавшие юниты) приходят с
  // бэкенда готовыми: уровни те же, что у алертов, считать их здесь второй раз незачем
  for (const p of s.problems ?? [])
    out.push({ tone: p.level >= 2 ? 't-down' : 't-degraded', text: p.text, sec: p.sec, mute: p.mute, kind: p.kind, since: p.since })
  // с какого момента: панель запоминает начало проблемы каждого вида (alert_since)
  for (const i of out) if (i.since === undefined && i.kind) i.since = s.alert_since?.[i.kind] ?? null
  return keep(out)
}

// Версия setup-скрипта — строка «мажор.минор» (0.12). Показываем как есть; про
// сравнение знает только бэкенд (_ver_key), фронту сравнивать нечего.
export function fmtSetupVersion(v?: string | null): string {
  return v ? `v${v}` : '?'
}

// Вышел ли бэкап за «ночное окно». Мягкое уведомление (не алерт): бэкап либо ещё идёт
// после дедлайна, либо завершился позже него. backup_anytime отключает проверку — для
// нод, где дневной бэкап это норма. Возвращает текст уведомления или null.
export function backupWindowNote(s: Server): string | null {
  if (s.backup_anytime) return null
  const b = s.last_report?.backup
  if (!b) return null
  const deadline = s.backup_deadline_hour ?? 8
  const started = b.started_ts ?? 0
  const ended = b.last_backup_ts ?? 0
  if (!started && !ended) return null
  const hourOf = (ts: number) => new Date(ts * 1000).getHours()
  const hhmm = (ts: number) =>
    new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  // бэкап ещё идёт (последний старт новее последнего завершения), и уже позже дедлайна
  const running = started > ended
  if (running) {
    if (new Date().getHours() >= deadline) {
      return tr('бэкап идёт с {from} и не уложился в окно (до {to}:00)',
                { from: hhmm(started), to: deadline })
    }
    return null
  }
  // завершённый бэкап закончился позже дедлайна (и в тот же «день», а не глубокой ночью)
  if (ended && hourOf(ended) >= deadline && hourOf(ended) < 22) {
    return tr('бэкап закончился в {at} — позже окна (до {to}:00)',
              { at: hhmm(ended), to: deadline })
  }
  return null
}
