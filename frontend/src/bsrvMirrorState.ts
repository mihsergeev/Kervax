// Резервный сервер бэкапов: что панель понимает из отчетов (helper mirror-setup резервного и
// backupserver-setup основного). Компоненты - в BsrvMirror.tsx.
import type { MirrorCopy, MirrorReport, Server } from './api'
import type { RepoTone } from './bsrvRepos'
import { currentLang } from './i18n'
import { fmtBytes } from './units'

export const HELPER = '/lib65/kervax/kervax-mirror-helper'
// как у бэкенда (app/bsrv_mirror.py): копия забирается раз в сутки, отстала - больше двух суток без
// удачной перекачки; первая должна закончиться за сутки
const STALE_S = 50 * 3600
const FIRST_S = 26 * 3600
const AGENT_MIN = '2.57'

export type T = (k: string, p?: Record<string, string | number>) => string

export function verLt(a: string, b: string): boolean {
  const pa = a.split('.').map(Number)
  const pb = b.split('.').map(Number)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] ?? 0) !== (pb[i] ?? 0)) return (pa[i] ?? 0) < (pb[i] ?? 0)
  }
  return false
}

export function ago(ts: number | undefined, t: T): string {
  if (!ts) return t('никогда')
  const sec = Math.max(0, Date.now() / 1000 - ts)
  const en = currentLang() === 'en'
  const f = (n: number, ru: string, e: string) => (en ? `${n} ${e} ago` : `${n} ${ru} назад`)
  if (sec < 3600) return f(Math.max(1, Math.round(sec / 60)), 'мин', 'min')
  if (sec < 86400) return f(Math.round(sec / 3600), 'ч', 'h')
  return f(Math.round(sec / 86400), 'д', 'd')
}

export function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

/** Блок helper'а mirror-setup, если он свежий (старше 15 минут - helper не работает). */
export function mirrorReport(s: Server): MirrorReport | null {
  const b = s.last_report?.extras?.['backup-mirror']
  if (!b || b.v !== 1 || !b.ts) return null
  const ref = s.last_report?.clock_unix || Date.now() / 1000
  return ref - b.ts > 15 * 60 ? null : b
}

/** Резервный: так отмечен в панели или на нем уже стоит helper. */
export function isMirror(s: Server): boolean {
  return !!s.backup_mirror_role || !!s.last_report?.extras?.['backup-mirror']
}

export function copyKey(c: MirrorCopy): string {
  return `${c.src}/${c.repo}`
}

export function running(c: MirrorCopy, rep: MirrorReport): boolean {
  return rep.running && rep.current === copyKey(c)
}

/** Тон копии: заморожена или отстала - красная, испорченные файлы или неудачная попытка - желтая,
 * снятая с копирования - серая (хранится как есть). */
export function copyTone(c: MirrorCopy, rep: MirrorReport): RepoTone {
  const now = Date.now() / 1000
  if (c.frozen) return 'bad'
  if (!c.enabled) return 'mute'
  if (!running(c, rep)) {
    if (c.last_ok && now - c.last_ok > STALE_S) return 'bad'
    if (!c.last_ok && c.added && now - c.added > FIRST_S) return 'bad'
  }
  if (c.check_bad || c.bad_new || (c.err_code && c.err_code !== 'frozen')) return 'warn'
  return 'ok'
}

export function errText(c: MirrorCopy, t: T): string {
  const m: Record<string, string> = {
    ssh: t('основной не пустил по SSH'),
    list: t('список файлов с основного пришел неполным'),
    get: t('перекачка оборвалась'),
    space: t('на резервном не хватает места'),
    config: t('нет адреса или ключа основного'),
    run: t('перекачка упала'),
    frozen: t('копия заморожена'),
  }
  if (!c.err_code) return ''
  return (m[c.err_code] ?? c.err_code) + (c.err && c.err_code !== 'frozen' ? ` (${c.err.replace(/[.\s]+$/, '')})` : '')
}

export function frozenText(c: MirrorCopy, t: T): string {
  const [code, ...rest] = c.frozen_why.split(' ')
  const m: Record<string, string> = {
    snapshots: t('на основном разом пропала большая часть снапшотов'),
    data: t('на основном разом пропала большая часть данных'),
    empty: t('репозиторий на основном пуст'),
    config_gone: t('на основном пропал config репозитория'),
    config_changed: t('на основном сменился config: это другой репозиторий'),
  }
  if (!m[code]) return c.frozen_why
  // helper пишет "snapshots 15 of 17 ..." или "data <байт> of <байт> ...": показываем сколько из скольких
  const [a, , b] = rest
  if ((code === 'snapshots' || code === 'data') && /^\d+$/.test(a ?? '') && /^\d+$/.test(b ?? '')) {
    const f = code === 'data' ? (x: string) => fmtBytes(Number(x)) : (x: string) => x
    return m[code] + ' ' + t('({gone} из {was})', { gone: f(a), was: f(b) })
  }
  return m[code]
}

/** Коротко для списка: "опоздала 3 дн.", "заморожена", "2 ч назад". */
export function copyShort(c: MirrorCopy, rep: MirrorReport, t: T): string {
  if (c.frozen) return t('заморожена')
  if (!c.enabled) return t('на паузе')
  if (running(c, rep)) return t('забирается')
  if (!c.last_ok) return t('ждет первой')
  return ago(c.last_ok, t)
}

/** Дошла ли настройка до серверов: пусто - да. */
export function entryDrift(p: Server, repo: string, m: Server, t: T): string {
  const e = p.backup_mirror?.[repo]
  if (!e) return ''
  const b = p.last_report?.extras?.['backup-server']
  if (b?.mirrors && !(b.mirrors[`mirror-${m.id}`]?.repos ?? []).includes(repo))
    return t('{p} еще не пустил резервный к этому репозиторию', { p: p.name })
  const r = mirrorReport(m)
  if (r) {
    const src = r.sources?.[e.src]
    if (!src?.repos.includes(repo)) return t('{m} еще не получил этот репозиторий в список', { m: m.name })
    if (src.pending)
      return t('{m} ждет подтверждения нового адреса или ключа {p}: на нем {cmd}', {
        m: m.name, p: p.name, cmd: `${HELPER} accept ${e.src}`,
      })
  }
  return ''
}

// заметки helper'а основного (sshd пускает только AllowUsers и т.п.) из итога рассылки
// Заметки helper'а основного (NOTE: ... после mirror-allow) по-человечески; незнакомые - как есть
const NOTES: [RegExp, string][] = [
  [/^ufw did not take the rule for (\S+) to port (\d+)$/, 'ufw не принял правило для {1} на порт {2}: откройте его сами'],
  [/^sshd is not found on this server$/, 'на основном не нашелся sshd'],
  [/^sshd has PubkeyAuthentication no for (\S+)$/, 'в sshd для {1} выключен вход по ключу (PubkeyAuthentication no)'],
  [/^sshd lets in only AllowUsers: add (\S+) there, otherwise the mirror is turned away$/,
    'sshd на основном пускает только AllowUsers: добавьте туда {1}, иначе резервного не пустят'],
  [/^sshd lets in only AllowGroups: add the group (\S+) there, otherwise the mirror is turned away$/,
    'sshd на основном пускает только AllowGroups: добавьте туда группу {1}, иначе резервного не пустят'],
  [/^sshd DenyUsers turns (\S+) away$/, 'DenyUsers в sshd не пускает {1}'],
  [/^not here, left out:(.*)$/, 'этих репозиториев на основном нет, их пропустили:{1}'],
]

function noteText(x: string, t: T): string {
  for (const [re, ru] of NOTES) {
    const m = re.exec(x)
    if (m) return t(ru).replace(/\{(\d)\}/g, (_, i: string) => m[Number(i)] ?? '')
  }
  return x
}

/** Что сказали серверы на последнюю раздачу настроек: заметки основного и ошибки. */
export function syncNotes(p: Server, mirrorId: number, t: T): string[] {
  const r = p.backup_mirror_sync?.[String(mirrorId)]
  const out: string[] = []
  if (r?.allow) {
    if (!r.allow.ok) out.push(r.allow.out)
    else out.push(...r.allow.out.split(/(?=NOTE: )/).filter((x) => x.startsWith('NOTE: ')).map((x) => noteText(x.slice(6).trim(), t)))
  }
  if (r?.set && !r.set.ok) out.push(r.set.out)
  return out.filter(Boolean)
}

/** Почему сервер пока не может быть резервным (пусто - может). */
export function mirrorNotReady(m: Server, t: T): string {
  if (!m.agent_version || verLt(m.agent_version, AGENT_MIN)) return t('нужен агент {v} или новее', { v: AGENT_MIN })
  const r = mirrorReport(m)
  if (!r) return t('не стоит helper mirror-setup')
  return ''
}
