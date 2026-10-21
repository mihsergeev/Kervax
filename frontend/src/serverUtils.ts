import type { BackupAudit, BackupInfo, KnownIssue, DiskHealth, DiskUsage, DockerRisk, Server, ServerReport, UnitsBlock, WebRate } from './api'
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

// sec — раздел детали сервера (msec-<sec>), куда ведёт клик по 🔥. Те же имена,
// что в диплинках алертов (_SRV_SECTION в коллекторе), чтобы поведение совпадало.
// mute — ключ для быстрого приглушения ИМЕННО этого сигнала (disk@1 = только предупр.
// диска, крит останется). Панель шлёт его в alert_mutes.
// since - с какого момента проблема видна (ISO): для "Что сломано" - "2 дн"
// section и srv - куда вести с главной, если проблема не про карточку сервера (см. ServerProblem)
export type SrvIssue = {
  tone: 't-down' | 't-degraded'; text: string; sec?: string; mute?: string; kind?: string; since?: string | null
  section?: 'kuber' | 'services' | 'backups' | 'docker'; srv?: boolean
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
  if (mutes.has(i.mute)) return true
  // уровень только у вида без хвоста (disk@1): в unit:openvpn@srv.service "@" - часть имени
  const lv = /^([a-z_]+)@(\d)$/.exec(i.mute)
  const base = lv ? lv[1] : i.mute
  const level = lv ? parseInt(lv[2], 10) : 3
  if (mutes.has(base)) return true
  for (const m of mutes) {
    if (!m.startsWith(base + '@')) continue
    const n = parseInt(m.slice(base.length + 1), 10)
    if (!Number.isNaN(n) && level <= n) return true
  }
  return false
}

function srvIssuesRaw(
  s: Server,
  t: (k: string, p?: Record<string, string | number>) => string,
): SrvIssue[] {
  // агента еще не ставили: задача, а не поломка (алерта "недоступен" по такому серверу и нет)
  if (!s.online && !s.last_seen)
    return [{ tone: 't-degraded', text: t('агент ещё не выходил на связь'), mute: 'offline', kind: 'offline' }]
  if (!s.online) {
    // что ответил сервер панели в момент алерта: молчит агент или сама машина (backend app/liveness.py)
    const p = s.offline_probe
    const why = p?.state === 'alive' ? t('сервер отвечает ({where}), молчит агент', { where: p.where ?? '' })
      : p?.state === 'refused' ? t('адрес отвечает отказом ({where}), похоже на перезагрузку', { where: p.where ?? '' })
        : p?.state === 'silent' && p.was ? t('с панели сервер тоже не отвечает, хотя на связи отвечал ({where})', { where: p.was })
          : p?.state === 'silent' ? t('с панели сервер тоже не отвечает')
            // эталон: и на связи панель до сервера не достукивалась, молчание ничего не значит
            : p?.state === 'blind' ? t('с панели не проверить: порты закрыты для нее') : ''
    return [{ tone: 't-down', text: why ? `${t('оффлайн')} - ${why}` : t('оффлайн'), mute: 'offline', kind: 'offline', since: s.last_seen }]
  }
  const out: SrvIssue[] = []
  // все, о чем шлют алерты (место и inode, CPU, RAM, температуры, conntrack и троттлинг - по уровню
  // алерта, с его выдержкой, а не по последнему отчету; поломки дисков, прогноз, юниты, часы, DNS, коннекты СУБД,
  // 5xx, очереди, сроки и Flux, дампы и проверки бэкапов), приходит с бэкенда готовым: уровни те
  // же, что у алертов, считать их здесь второй раз незачем
  for (const p of s.problems ?? [])
    out.push({ tone: p.level >= 2 ? 't-down' : 't-degraded', text: p.text, sec: p.sec, mute: p.mute, kind: p.kind,
      since: p.since, section: p.section, srv: p.srv })
  // с какого момента: панель запоминает начало проблемы каждого вида (alert_since)
  for (const i of out) if (i.since === undefined && i.kind) i.since = s.alert_since?.[i.kind] ?? null
  return out
}

// Отметка "известная проблема" еще в силе: срок не вышел (сборщик снимет ее и сам, но до
// его тика главная не должна прятать то, о чем снова пойдут алерты)
export function knownActive(k?: KnownIssue | null): k is KnownIssue {
  return !!k && (!k.until || Date.parse(k.until) > Date.now())
}

// Известная проблема этого вида, если не стало хуже отмеченного
export function srvKnownOf(s: Server, i: SrvIssue): KnownIssue | null {
  const k = i.kind ? s.known_issues?.[i.kind] : undefined
  return knownActive(k) && !k.worse ? k : null
}

// Проблемы сервера без приглушенных и без известных: известные показываются отдельно (srvKnown)
// Заглушенное у сервера сейчас: насовсем и на время (снуз 🔕 с главной на час, день, неделю)
function srvMutesNow(s: Server): Set<string> {
  const out = new Set(s.alert_mutes ?? [])
  const now = Date.now()
  for (const [k, u] of Object.entries(s.alert_snoozes ?? {})) if (Date.parse(u) > now) out.add(k)
  return out
}

export function srvIssues(
  s: Server,
  t: (k: string, p?: Record<string, string | number>) => string,
): SrvIssue[] {
  const mutes = srvMutesNow(s)
  return srvIssuesRaw(s, t).filter((i) => !srvIssueMuted(i, mutes) && !srvKnownOf(s, i))
}

// Приглушенные проблемы сервера: на главной их нет, а в карточке раздел с такой проблемой серый с
// пометкой, а не зеленый "все в порядке" рядом с "истёк" или "с проблемой 1"
export function srvMuted(
  s: Server,
  t: (k: string, p?: Record<string, string | number>) => string,
): SrvIssue[] {
  const mutes = srvMutesNow(s)
  return srvIssuesRaw(s, t).filter((i) => srvIssueMuted(i, mutes))
}

// Известные проблемы сервера: что сломано и что про это написали
export function srvKnown(
  s: Server,
  t: (k: string, p?: Record<string, string | number>) => string,
): (SrvIssue & { known: KnownIssue })[] {
  const mutes = srvMutesNow(s)
  return srvIssuesRaw(s, t).flatMap((i) => {
    const k = srvKnownOf(s, i)
    return k && !srvIssueMuted(i, mutes) ? [{ ...i, known: k }] : []
  })
}

// Версия setup-скрипта — строка «мажор.минор» (0.12). Показываем как есть; про
// сравнение знает только бэкенд (_ver_key), фронту сравнивать нечего.
export function fmtSetupVersion(v?: string | null): string {
  return v ? `v${v}` : '?'
}

// Версия и месяц сборки образа внешнего прокси: "v2.5.5, 12.2021". Версия есть не у всех
// образов (у caddy-docker-proxy метки нет), месяц сборки - всегда.
export function proxyBuilt(p: { version?: string; built: number }): string {
  const d = new Date(p.built * 1000)
  const when = `${String(d.getMonth() + 1).padStart(2, '0')}.${d.getFullYear()}`
  return p.version ? `${p.version}, ${when}` : when
}

// Список для одной строки: первые n и "и еще N"
export function shortList(xs: string[], t: (k: string, p?: Record<string, string | number>) => string, n = 5): string {
  return xs.length <= n ? xs.join(', ') : `${xs.slice(0, n).join(', ')} ${t('и еще {n}', { n: xs.length - n })}`
}

// Базы без дампа по именам. Одинаковые СУБД различаются местом: "PostgreSQL (default/postgres)",
// иначе выходило "PostgreSQL, PostgreSQL - нужен отдельный дамп", и непонятно, о каких базах речь
export function dbAuditNames(xs: BackupAudit[]): string[] {
  const where = (x: BackupAudit) => {
    const inst = x.instance || x.container || ''
    const m = /^k8s\.([^.]+)\.[a-z]+\.(.+)$/.exec(inst)
    return m ? `${m[1]}/${m[2]}` : inst
  }
  return xs.map((x) =>
    xs.filter((y) => y.subject === x.subject).length > 1 && where(x) ? `${x.subject} (${where(x)})` : x.subject)
}

// Ключи от хоста у контейнера словами: "privileged, docker-сокет", "cap SYS_ADMIN"
export function dockRiskText(x: DockerRisk, t: (k: string, p?: Record<string, string | number>) => string): string {
  const word: Record<string, string> = { priv: 'privileged', sock: t('docker-сокет'), host: t('корень хоста на запись') }
  return x.flags.map((f) => (f === 'caps' ? `cap ${x.caps.join(', ')}` : word[f] ?? f)).join(', ')
}

// «живой» бэкап: есть systemd-таймер ИЛИ restic на месте и хоть раз бэкапил. Только
// осиротевший метрик (restic снесён, таймера нет) — мёртвый бэкап → нода в «Без бэкапа».
// Один helper backup-setup без настроенного restic - тоже не бэкап: по нему главная
// показывала "под бэкапом 8, все свежие" там, где в "Бэкапах" клиентов 0.
export function backupAlive(b?: BackupInfo | null): boolean {
  if (!b || !b.present) return false
  if (b.configured) return true
  if (b.restic_found && b.metric_present) return true
  return false
}

// свой файловый бэкап ноды (restic/borg/rsync без панели) — нода не «без бэкапа»
export function hasOwnFileBackup(s: Server): boolean {
  return (s.custom_backups ?? []).some((j) => j.files && !j.ignored)
}

// Вышел ли бэкап за «ночное окно». Мягкое уведомление (не алерт): бэкап либо ещё идёт
// после дедлайна, либо завершился позже него. backup_anytime отключает проверку — для
// нод, где дневной бэкап это норма. Возвращает текст уведомления или null.
export function backupWindowNote(s: Server): string | null {
  if (s.backup_anytime) return null
  const b = s.last_report?.backup
  // метрика без restic и таймера - следы старого бэкапа, ее время к окну отношения не имеет
  if (!b || !backupAlive(b)) return null
  const deadline = s.backup_deadline_hour ?? 8
  const started = b.started_ts ?? 0
  const ended = b.last_backup_ts ?? 0
  if (!started && !ended) return null
  const hourOf = (ts: number) => new Date(ts * 1000).getHours()
  const hhmm = (ts: number) =>
    new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  // Окно - ночь до дедлайна. Начатый днем или вечером (расписание на 18:00, запуск руками) в
  // окно и не собирался: "позже окна" про него вводило в заблуждение
  if (started && hourOf(started) >= deadline && hourOf(started) < 22) return null
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

// Кто отметил и до какого числа: "admin, до 20.10.2026"
// Текст отметки: что написал человек, а у поставленной при закрытии подключения - перевод
export function knownText(k: KnownIssue, t: (k: string, p?: Record<string, string | number>) => string): string {
  return k.onboard ? t('было при подключении') : k.text
}

export function knownMeta(k: KnownIssue, t: (k: string, p?: Record<string, string | number>) => string): string {
  const parts = [k.by || '']
  if (k.until) parts.push(t('до {d}', { d: new Date(k.until).toLocaleDateString() }))
  return parts.filter(Boolean).join(', ')
}

// GPU: строка раздела на карте сервера, когда замечаний нет
export function gpuSecLine(s: Server, t: (k: string, p?: Record<string, string | number>) => string): string {
  const sum = s.gpu_sum
  if (!sum) return ''
  if (!sum.helper) return t('карт на шине: {n}, нет helper gpu-setup', { n: sum.n })
  return [
    `${sum.n} x ${sum.models.join(', ')}`,
    sum.util != null ? t('загрузка {p}%', { p: sum.util }) : '',
    sum.mem != null ? t('память {a} из {b} ГБ', { a: sum.mem, b: sum.memt ?? 0 }) : '',
  ].filter(Boolean).join(', ')
}
