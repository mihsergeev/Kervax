import type { Server } from './api'
import { tr } from './i18n'

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
export type SrvIssue = { tone: 't-down' | 't-degraded'; text: string; sec?: string; mute?: string }

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
  if (!s.online) return keep([{ tone: 't-down', text: t('оффлайн'), mute: 'offline' }])
  const out: SrvIssue[] = []
  const cpu = srvCpuPct(s)
  if (cpu != null && s.cpu_alert_percent && cpu >= s.cpu_alert_percent)
    out.push({ tone: 't-down', text: `CPU ${cpu}%`, sec: 'cpu', mute: 'cpu' })
  const ram = srvRamPct(s)
  if (ram != null && s.mem_alert_percent && ram >= s.mem_alert_percent)
    out.push({ tone: 't-down', text: `RAM ${ram}%`, sec: 'mem', mute: 'mem' })
  const disk = srvDiskPct(s)
  if (disk != null) {
    if (s.disk_crit_percent && disk >= s.disk_crit_percent)
      out.push({ tone: 't-down', text: `${t('Диск')} ${disk}% 🚨`, sec: 'diskfill', mute: 'disk' })
    else if (s.disk_alert_percent && disk >= s.disk_alert_percent)
      out.push({ tone: 't-down', text: `${t('Диск')} ${disk}%`, sec: 'diskfill', mute: 'disk@2' })
    else if (s.disk_warn_percent && disk >= s.disk_warn_percent)
      out.push({ tone: 't-degraded', text: `${t('Диск')} ${disk}%`, sec: 'diskfill', mute: 'disk@1' })
  }
  const r = s.last_report
  // температура CPU (на VM датчика нет → cpu_temp = null)
  if (r?.cpu_temp != null && s.temp_alert_c && r.cpu_temp >= s.temp_alert_c)
    out.push({ tone: 't-down', text: `CPU ${Math.round(r.cpu_temp)}°C`, sec: 'temp' })
  // троттлинг CPU
  if (r?.cpu_throttle != null && r.cpu_throttle > 0)
    out.push({ tone: 't-degraded', text: t('троттлинг'), sec: 'throttle' })
  // conntrack близок к пределу
  const ctmax = r?.conntrack_max ?? 0
  if (ctmax > 0 && s.conntrack_alert_percent) {
    const fill = Math.round(((r?.conntrack_count ?? 0) / ctmax) * 100)
    if (fill >= s.conntrack_alert_percent)
      out.push({ tone: 't-down', text: `conntrack ${fill}%` })
  }
  // температура диска (макс по устройствам с датчиком)
  if (s.disk_temp_alert_c && r?.disk_devs?.length) {
    const temps = r.disk_devs.map((d) => d.temp).filter((x): x is number => x != null)
    if (temps.length && Math.max(...temps) >= s.disk_temp_alert_c)
      out.push({ tone: 't-down', text: `${t('Диск')} ${Math.round(Math.max(...temps))}°C` })
  }
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
