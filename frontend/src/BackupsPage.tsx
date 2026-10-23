import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  backupCommand,
  backupCommandStatus,
  backupCredentials,
  backupRepoArchive,
  backupRepoMute,
  backupSetup,
  backupSetupStatus,
  CURRENT_BACKUP_HELPER,
  deployBackupServer,
  enableBackupTls,
  listServers,
  setCustomBackupIgnored,
  type BackupArchiveEntry,
  type BackupCommand,
  type BackupCreds,
  type BackupGroup,
  type BackupInfo,
  type BackupRun,
  type BackupServerExtra,
  type BackupServerInfo,
  type BackupServerLegacy,
  type BackupServerRepoExtra,
  type BackupSetupJob,
  type CustomBackup,
  type RepoStat,
  type Server,
} from './api'
import { BACKUP_HTTP_MUTE, backupServerToHttps, backupToHttps, updateServer } from './api'
import { trBackend } from './backendText'
import { backupAlive, backupDest, backupDestWhere, backupWindowNote, dbAuditNames, hasOwnFileBackup, shortList, srvIpHit, srvIssues, srvMuted, type BackupDest } from './serverUtils'
import { fmtRel } from './ServersPage'
import { CardNav, FreshChip, markMuted, OvAreas, OvRows, secInfoOf, type OvRow, type SecDef, type SecInfo, type SecItem } from './cardLayout'
import { OsIcon } from './osIcon'
import { CoverageAudit } from './CoverageAudit'
import { helperText } from './helperText'
import { bulkCleanupScript, safeRepoName } from './bulkCleanupScript'
import { useAuth } from './auth'
import { useUrlCard, useUrlParam } from './deeplink'
import { useI18n, currentLang, tr } from './i18n'
import { MuteChip, collectMutes, muteRows, mutesTileSub } from './mutes'
import { MutesPanel } from './MutesPanel'
import { VaultPanel } from './VaultPanel'
import { DeadBlocks, LegacyPlaque, OffRows, RowFix, bsrvFixText, offLines, rotationAction, rotationLines } from './BsrvCleanup'
import { FixSteps } from './FixSteps'
import { CountryFlag } from './CountryFlag'
import { byteUnits, fmtDur } from './units'
import { NetTile, TileFilterChip, useTileFilter, type Tone } from './NetLinks'

async function runAndWait(
  serverId: number,
  body: Parameters<typeof backupCommand>[1],
): Promise<BackupCommand> {
  const c = await backupCommand(serverId, body)
  let last = c
  for (let i = 0; i < 60 && last.status !== 'done' && last.status !== 'error'; i++) {
    await new Promise((r) => setTimeout(r, 400))
    last = await backupCommandStatus(serverId, c.id)
  }
  return last
}

// Вкладка «Бэкапы» (Фаза 1 — статус, read-only): компактный список нод с restic-бэкапом;
// клик по ноде → модалка с деталями (свежесть, успех, таймер, длительность, заметки).
// Агент снимает статус БЕЗ секретов (метрики node_exporter + systemctl show) — панель
// не видит пароли/URL/сервер назначения, обфускация клиента сохраняется.

const STALE_SECONDS = 2 * 86400

type BStatus = 'ok' | 'failed' | 'stale' | 'skipped' | 'unknown'

function backupStatus(b: BackupInfo): BStatus {
  // ноды со старым runner'ом метрик не пишут: агент (1.49+) берёт время из systemd,
  // а успех — из Result юнита. Такой бэкап полноценно оцениваем, а не прячем в «unknown»
  if (!b.metric_present && b.ts_source !== 'systemd') return 'unknown'
  if (b.ts_source === 'systemd') {
    const res = (b.service_result || '').trim()
    if (res && res !== 'success') return 'failed'
  } else if (b.success === 0) {
    return 'failed'
  }
  const ts = b.last_backup_ts || 0
  if (ts && Date.now() / 1000 - ts > STALE_SECONDS) return 'stale'
  if (b.skipped) return 'skipped'
  return 'ok'
}
function statusTone(st: BStatus): string {
  if (st === 'ok') return 't-up'
  if (st === 'failed' || st === 'stale') return 't-down'
  if (st === 'skipped') return 't-degraded'
  return 't-degraded'
}
// тон плитки по статусу бэкапа
const TILE: Record<BStatus, Tone | undefined> = { ok: 'ok', failed: 'bad', stale: 'bad', skipped: 'warn', unknown: undefined }
// точка строки по тону и подпись плитки из частей (пустые выкидываем)
const DOT: Record<string, string> = { 't-up': ' lk-dot-ok', 't-down': ' lk-dot-bad', 't-degraded': ' lk-dot-warn' }
const subOf = (...parts: (string | false | 0 | undefined)[]) => parts.filter(Boolean).join(' · ')
// заполнение тома с репозиториями, % (null: helper старше 0.18, данных нет)
function diskPct(info: BackupServerInfo): number | null {
  const total = info.disk_total || 0
  return total ? Math.min(100, Math.round(((info.disk_used || 0) / total) * 100)) : null
}

// Куда бэкапится: имя сервера бэкапов, за ним адрес, порт и канал (serverUtils.backupDest). Имя -
// ссылка на окно сервера бэкапов (или карточку, если rest-server на нем панель не видит). http через
// интернет подсвечен: это пункт "Требует действий", пока не решили оставить
function DestLabel({ dest, onOpen }: { dest: BackupDest; onOpen?: (s: Server) => void }) {
  const { t } = useI18n()
  const tg = dest.target
  const where = backupDestWhere(tg)
  const proto = tg && tg.kind !== 'local' ? (
    <span className={tg.insecure && !tg.muted ? 't-degraded' : undefined}>{tg.proto}</span>
  ) : null
  const addr = where || proto ? <>{where}{where && proto ? ', ' : ''}{proto}</> : null
  // сервер бэкапов в панели не нашелся: имя - это и есть хост, повторять его не нужно
  if (!dest.srv) return addr ?? <>{dest.name}</>
  const bsrv = !!dest.srv.last_report?.backup_server?.present
  const name = onOpen ? (
    <a href={bsrv ? `?backupsrv=${dest.srv.id}` : `?server=${dest.srv.id}`} title={t('открыть сервер бэкапов')}
      onClick={(e) => { e.preventDefault(); onOpen(dest.srv!) }}>{dest.name}</a>
  ) : dest.name
  return <>{name}{addr && <> ({addr})</>}</>
}

// Статистику репозитория (физический размер на бэкап-сервере, число снапшотов) панель
// уже знает СО СТОРОНЫ бэкап-сервера (его helper делает du + считает снапшоты). Клиент
// пароль репо не отдаёт, поэтому размер берём отсюда — сопоставляя repo_dest клиента с
// репозиторием на бэкап-сервере по хосту+имени (или по имени, если repo_dest старый).
function backupRepoStat(
  repoDest: string | undefined,
  servers: Server[],
  client?: Server,
): RepoStat | undefined {
  const host = repoDest?.match(/rest:[a-z]+:\/\/([^:/]+)/)?.[1] || ''
  const nameFromDest = repoDest?.match(/rest:[a-z]+:\/\/[^/]+\/([A-Za-z0-9._-]+)/)?.[1]
  const wanted = [nameFromDest, client?.hostname, client?.name].filter(Boolean) as string[]
  for (const s of servers) {
    const repos = s.last_report?.backup_server?.repos
    if (!repos) continue
    if (host && !(s.external_ip === host || s.agent_ip === host || s.hostname === host)) continue
    const r = repos.find((x) => wanted.includes(x.name))
    if (r) return r
  }
  return undefined
}

// Прогоны бэкапа клиента (helper backup-setup 0.32) и обычный прирост за прогон после сжатия -
// медиана последних 14, как у алерта о скачке
function backupRuns(s: Server): BackupRun[] {
  const b = s.last_report?.extras?.['backup-runs']
  return b && b.v === 1 && Array.isArray(b.runs) ? b.runs.slice().sort((x, y) => x.ts - y.ts) : []
}
function usualGrowth(runs: BackupRun[]): number {
  const v = runs.slice(-14).map((r) => r.st).sort((a, b) => a - b)
  if (!v.length) return 0
  const m = Math.floor(v.length / 2)
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2
}

// Сколько каждый прогон добавил в репозиторий (после сжатия) - столбиками, свежий справа;
// подробности прогона - в подсказке столбика
function GrowthBars({ runs }: { runs: BackupRun[] }) {
  const { t } = useI18n()
  const max = Math.max(...runs.map((r) => r.st), 1)
  const w = 6
  const gap = 2
  const h = 44
  return (
    <svg className="growth-bars" width={runs.length * (w + gap)} height={h} role="img"
      aria-label={t('Прирост бэкапа по прогонам')}>
      {runs.map((r, i) => {
        const bh = Math.max(1, Math.round((r.st / max) * (h - 2)))
        return (
          <rect key={r.ts} x={i * (w + gap)} y={h - bh} width={w} height={bh} rx={1}>
            <title>
              {`${new Date(r.ts * 1000).toLocaleString()}\n` +
                t('добавлено {a} (после сжатия {s}), прочитано {p} за {d}', {
                  a: fmtBytes(r.add), s: fmtBytes(r.st), p: fmtBytes(r.proc), d: fmtDur(r.dur),
                })}
            </title>
          </rect>
        )
      })}
    </svg>
  )
}

function fmtAgo(ts?: number): string {
  if (!ts) return '—'
  const sec = Math.max(0, Date.now() / 1000 - ts)
  const en = currentLang() === 'en'
  const ago = (n: number, ru: string, e: string) => (en ? `${n} ${e} ago` : `${n} ${ru} назад`)
  if (sec < 3600) return ago(Math.round(sec / 60), 'мин', 'min')
  if (sec < 86400) return ago(Math.round(sec / 3600), 'ч', 'h')
  return ago(Math.round(sec / 86400), 'д', 'd')
}
// «restic 0.18.1 compiled with go1.25.1 on linux/amd64» → «restic 0.18.1»
function shortRestic(v?: string): string {
  if (!v) return ''
  const m = v.match(/restic\s+(\S+)/i)
  return m ? `restic ${m[1]}` : v.split(/\s+/).slice(0, 2).join(' ')
}
// Редактор списка путей с подсветкой прямо в поле: прозрачный <textarea> поверх
// раскрашенного <pre>. Строки из junkSet (стандартный мусор) — серым, остальные (своё) —
// белым. Метрики textarea и pre идентичны (общий класс .backup-paths), поэтому текст,
// строки и каретка совпадают; скролл синхронизируем вручную.
function PathsEditor({ value, onChange, junkSet }: {
  value: string; onChange: (v: string) => void; junkSet: Set<string>
}) {
  const taRef = useRef<HTMLTextAreaElement>(null)
  const preRef = useRef<HTMLPreElement>(null)
  const syncScroll = () => {
    const ta = taRef.current, pre = preRef.current
    if (ta && pre) { pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft }
  }
  const lines = value.split('\n')
  return (
    <div className="backup-paths-wrap">
      <pre className="backup-paths backup-paths-hl" ref={preRef} aria-hidden="true">
        {lines.map((ln, i) => {
          const p = ln.trim()
          const junk = p !== '' && junkSet.has(p)
          return (
            <span key={i} className={junk ? 'excl-junk' : 'excl-keep'}>
              {ln}{i < lines.length - 1 ? '\n' : ''}
            </span>
          )
        })}
      </pre>
      <textarea
        ref={taRef}
        className="backup-paths backup-paths-edit"
        value={value}
        wrap="off"
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
        onScroll={syncScroll}
        placeholder={'/etc\n/var/www\n/home'}
      />
    </div>
  )
}

function ManageSection({ server: s, backup: b, onChanged }: { server: Server; backup: BackupInfo; onChanged: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [schedule, setSchedule] = useState(() => {
    const [h, m] = (b.schedule || '').split(':')
    return h && m ? `${h.padStart(2, '0')}:${m}` : ''
  })
  const [deadline, setDeadline] = useState(s.backup_deadline_hour ?? 8)
  const [anytime, setAnytime] = useState(!!s.backup_anytime)
  const [mode, setMode] = useState<'include' | 'exclude'>((b.mode as 'include' | 'exclude') || 'exclude')
  const [paths, setPaths] = useState(() =>
    ((mode === 'include' ? b.includes : b.excludes) || []).join('\n'),
  )

  const run = async (label: string, body: Parameters<typeof backupCommand>[1]) => {
    setBusy(label)
    setMsg(null)
    try {
      const res = await runAndWait(s.id, body)
      setMsg({ ok: res.status === 'done', text: helperText(res.result, t) || (res.status === 'done' ? t('готово') : t('ошибка')) })
      onChanged()
    } catch {
      setMsg({ ok: false, text: t('ошибка') })
    } finally {
      setBusy(null)
    }
  }

  // исходные (сохранённые) значения — чтобы одна кнопка «Сохранить» слала только изменённое
  const normHM = (v: string) => { const [h, m] = (v || '').split(':'); return h && m ? `${h.padStart(2, '0')}:${m}` : '' }
  const origSchedule = normHM(b.schedule || '')
  const origDeadline = s.backup_deadline_hour ?? 8
  const origAnytime = !!s.backup_anytime
  const origMode = (b.mode as 'include' | 'exclude') || 'exclude'
  const curList = paths.split('\n').map((x) => x.trim()).filter(Boolean)
  // «стандартный мусор» (DEFAULT_EXCLUDES) в поле красим серым, всё остальное (своё) —
  // белым, чтобы нестандартные пути были видны прямо в списке. Только в exclude-режиме;
  // в include каждый путь осмысленный → все белые.
  const junkSet = mode === 'exclude' ? new Set(DEFAULT_EXCLUDES) : new Set<string>()
  const savedListForMode = ((mode === 'include' ? b.includes : b.excludes) || []).join('\n')
  const schedDirty = schedule !== origSchedule
  const winDirty = deadline !== origDeadline || anytime !== origAnytime
  const pathsDirty = mode !== origMode || curList.join('\n') !== savedListForMode
  const dirty = schedDirty || winDirty || pathsDirty

  // одна кнопка на всё: расписание и пути — команды ноде (узкий helper), окно — настройка
  // панели (updateServer). Шлём последовательно только то, что изменилось.
  const saveAll = async () => {
    if (!dirty) { setMsg({ ok: true, text: t('нет изменений') }); return }
    if (schedDirty && !/^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(schedule)) {
      setMsg({ ok: false, text: t('время в формате ЧЧ:ММ') }); return
    }
    if (pathsDirty && curList.length === 0) {
      setMsg({ ok: false, text: t('список путей пуст') }); return
    }
    // смена путей может урезать бэкап — подтверждаем только если пути реально менялись
    if (pathsDirty && !window.confirm(t('Применить новый список путей ({mode}, {n} шт.)?', { mode, n: curList.length }))) return
    setBusy('save'); setMsg(null)
    try {
      if (winDirty) await updateServer(s.id, { backup_deadline_hour: deadline, backup_anytime: anytime })
      if (schedDirty) {
        const r = await runAndWait(s.id, { action: 'set_schedule', schedule })
        if (r.status !== 'done') throw new Error(helperText(r.result, t) || t('ошибка'))
      }
      if (pathsDirty) {
        const r = await runAndWait(s.id, { action: 'set_paths', mode, paths: curList })
        if (r.status !== 'done') throw new Error(helperText(r.result, t) || t('ошибка'))
      }
      setMsg({ ok: true, text: t('Сохранено') })
      onChanged()
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : t('ошибка') })
    } finally {
      setBusy(null)
    }
  }
  const runNow = () => {
    if (!window.confirm(t('Запустить бэкап сейчас? Обычно не нужно — он идёт ночью по расписанию.'))) return
    run('now', { action: 'run_now' })
  }

  return (
    <div className="backup-manage" id="backup-manage-schedule">
      <div className="backup-manage-row">
        <label className="backup-manage-label">{t('Расписание (ежедневно)')}</label>
        {/* type="time" — браузер сам не даёт набрать «02333:00323»: раньше это было
            обычное текстовое поле, мусор ловился только валидацией при отправке */}
        <input
          className="field-inp backup-time-inp"
          type="time"
          step={60}
          value={schedule}
          onChange={(e) => setSchedule(e.target.value)}
        />
      </div>

      {/* «Окно» бэкапа: до какого часа должен закончиться. Позже — мягкое уведомление
          в панели (не Telegram). anytime — для нод, где дневной бэкап это норма. */}
      <div className="backup-manage-row">
        <label className="backup-manage-label">{t('Должен закончиться до, ч')}</label>
        <input
          className="field-inp backup-time-inp" type="number" min={0} max={23}
          value={deadline} disabled={anytime}
          onChange={(e) => setDeadline(Math.min(23, Math.max(0, Number(e.target.value) || 0)))}
        />
      </div>
      <label className="check-toggle backup-anytime-row">
        <input type="checkbox" checked={anytime} disabled={busy != null}
          onChange={(e) => setAnytime(e.target.checked)} />
        <span className="muted small">{t('Бэкап в любое время — не уведомлять о выходе за окно (дневные сервисы)')}</span>
      </label>

      <div className="backup-manage-row">
        <label className="backup-manage-label">{t('Что бэкапить')}</label>
        <div className="win-switch">
          <button className={`win-btn${mode === 'exclude' ? ' win-btn-active' : ''}`} onClick={() => { setMode('exclude'); setPaths((b.excludes || []).join('\n')) }}>
            {t('Всё, кроме (exclude)')}
          </button>
          <button className={`win-btn${mode === 'include' ? ' win-btn-active' : ''}`} onClick={() => { setMode('include'); setPaths((b.includes || []).join('\n')) }}>
            {t('Только (include)')}
          </button>
        </div>
      </div>
      {/* мусор (стандартные excludes) — серым, своё — белым; всё в одном редактируемом поле */}
      <PathsEditor value={paths} onChange={setPaths} junkSet={junkSet} />
      {mode === 'exclude' && (
        <div className="muted small backup-paths-hint">
          {t('Серым — стандартный мусор, белым — добавленное вами.')}
        </div>
      )}
      {/* одна кнопка «Сохранить» на всё (расписание + окно + пути); шлёт только изменённое.
          «Запустить сейчас» намеренно приглушён — бэкап и так идёт ночью по расписанию. */}
      <div className="backup-manage-actions">
        <button disabled={busy != null || !dirty} onClick={saveAll}>
          {busy === 'save' ? '…' : t('Сохранить')}
        </button>
        <button className="backup-runnow-link" disabled={busy != null} onClick={runNow}>
          {busy === 'now' ? t('идёт…') : t('запустить сейчас')}
        </button>
      </div>
      {msg && <div className={`small ${msg.ok ? 't-up' : 'form-error'}`}>{msg.text}</div>}
      <div className="muted small">
        {t('Изменения пишутся прямо в конфиг бэкапа на ноде через узкий helper. Внимание: повторный прогон Ansible может их перезаписать.')}
      </div>
    </div>
  )
}

// целевая версия restic для всего парка (совпадает с RESTIC_TARGET_VER в helper)
const RESTIC_TARGET = '0.19.1'
// целевая версия образа rest-server (совпадает с REST_IMAGE в backupserver-setup.sh).
// Тег «latest» когда-то застрял на 0.11.0 (docker не перекачивал) — держим фиксированный.
const RESTSRV_TARGET = '0.14.0'
// покомпонентное сравнение «мажор.минор.патч» → v < target
function verLt(v: string, target: string): boolean {
  const a = v.split('.').map((x) => parseInt(x, 10))
  const b = target.split('.').map((x) => parseInt(x, 10))
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] || 0, y = b[i] || 0
    if (x !== y) return x < y
  }
  return false
}
// ---------- свои бэкапы ноды: настроены без панели, находит helper ----------
const OWN_PROBLEM = ['failed', 'stale', 'disabled']
function ownBackupProblems(s: Server): CustomBackup[] {
  return (s.custom_backups ?? []).filter((j) => OWN_PROBLEM.includes(j.status))
}

// «0 4 * * *» и «*-*-* 04:00:00» — это «ежедневно 04:00»; остальное показываем как есть
function fmtSchedule(sched: string, t: (k: string, p?: Record<string, string | number>) => string): string {
  const v = (sched || '').trim()
  const two = (n: string) => n.padStart(2, '0')
  let m = v.match(/^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*$/)
  if (m) return t('ежедневно {time}', { time: `${two(m[2])}:${two(m[1])}` })
  m = v.match(/^(?:\*-\*-\*\s+)?(\d{1,2}):(\d{2})(?::00)?$/)
  if (m) return t('ежедневно {time}', { time: `${two(m[1])}:${m[2]}` })
  if (v === '@daily' || v === 'daily') return t('ежедневно')
  if (v === '@hourly' || v === 'hourly') return t('ежечасно')
  if (v === '@weekly' || v === 'weekly') return t('еженедельно')
  return v || '—'
}

function OwnBackupsChip({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const jobs = (s.custom_backups ?? []).filter((j) => !j.ignored)
  if (jobs.length === 0) return null
  const bad = ownBackupProblems(s).length
  return (
    <span className={`type-chip${bad ? ' t-down' : ''}`} title={jobs.map((j) => j.name).join(', ')}>
      {bad
        ? t('свои бэкапы: {n}, с проблемой: {b}', { n: jobs.length, b: bad })
        : t('свои бэкапы: {n}', { n: jobs.length })}
    </span>
  )
}

function OwnBackupRow({ job: j, canManage, busy, onToggle }: {
  job: CustomBackup; canManage: boolean; busy: boolean; onToggle: () => void
}) {
  const { t } = useI18n()
  const label: Record<CustomBackup['status'], string> = {
    ok: t('работает'), failed: t('ошибка'), stale: t('не свежий'), disabled: t('таймер выключен'), off: t('выключен'),
    running: t('идёт сейчас'), ran: t('запускается'), unknown: t('не видно'), ignored: t('не отслеживается'),
  }
  const tone: Record<CustomBackup['status'], string> = {
    ok: 't-up', running: 't-up', failed: 't-down', stale: 't-degraded', disabled: 't-degraded', off: 'muted',
    ran: '', unknown: 'muted', ignored: 'muted',
  }
  const dot: Record<CustomBackup['status'], string> = {
    ok: 'sdot-up', running: 'sdot-up', failed: 'sdot-down', stale: 'sdot-degraded',
    disabled: 'sdot-degraded', off: 'sdot-unknown', ran: 'sdot-unknown', unknown: 'sdot-unknown', ignored: 'sdot-unknown',
  }
  const where = j.kind === 'cron'
    ? `cron · ${fmtSchedule(j.schedule, t)}`
    : j.kind === 'systemd' ? `${t('таймер')} · ${fmtSchedule(j.schedule, t)}` : t('метрики')
  // «последнее» зависит от того, чем подтверждён итог: у задания без собственного статуса
  // это самый свежий файл бэкапа, у restic — снимок, который он сохранил
  const lastLbl =
    j.metrics || j.kind !== 'cron' || j.evidence === 'state' ? t('последний успех')
      : j.evidence === 'restic' ? t('последний снимок restic')
        : t('свежий файл бэкапа')
  const bits: string[] = []
  if (j.ok_ts) bits.push(`${lastLbl}: ${fmtAgo(j.ok_ts)}`)
  // запуск — отдельно, если он не тот же, что успех: иначе видно, что задание запускалось
  // уже после последнего успеха
  if (j.run_ts && (!j.ok_ts || Math.abs(j.run_ts - j.ok_ts) > 600))
    bits.push(`${t('последний запуск')}: ${fmtAgo(j.run_ts)}`)
  if (j.size_bytes) bits.push(fmtBytes(j.size_bytes))
  if (j.duration_sec) bits.push(`⏱ ${fmtDur(j.duration_sec)}`)
  const src = [
    j.dir ? `📁 ${j.dir}` : '',
    j.state ? `${t('отметка')}: ${j.state}` : '',
    j.evidence === 'restic' ? t('по кэшу restic') : '',
    j.log ? `${t('лог')}: ${j.log}` : '',
    j.metrics ? `${t('метрики')}: ${j.metrics.split('/').pop()}` : '',
    j.unit,
  ].filter(Boolean)
  return (
    <div className={`own-backup${j.ignored ? ' own-backup-ignored' : ''}`}>
      <span className={`sdot ${dot[j.status]}`} />
      <div className="own-backup-main">
        <div className="own-backup-name">
          <span className="mono">{j.name}</span>
          {j.engines.map((e) => <span key={e} className="type-chip">{e}</span>)}
          {j.containers.map((c) => <span key={c} className="type-chip mono">🐳 {c}</span>)}
          <span className="type-chip mono">{where}</span>
        </div>
        {j.desc && <div className="muted small">{j.desc}</div>}
        {bits.length > 0 && <div className="small">{bits.join(' · ')}</div>}
        {j.dbs.length > 0 && (
          <div className="muted small">
            {t('базы')}: {j.dbs.map((d) => `${d.name}${d.ok === 1 ? ' ✓' : d.ok === 0 ? ' ✗' : ''}`).join(', ')}
          </div>
        )}
        {j.problem && <div className="t-down small">{j.problem}</div>}
        {j.status === 'unknown' && (
          <div className="muted small">
            {j.scan_stale
              ? t('Сведения с ноды давно не обновлялись: helper бэкапа там не запускается. Переустановите его.')
              : t('Не видно, когда задание отработало: метрик у него нет, а файлов бэкапа панель не нашла.')}
          </div>
        )}
        {j.status === 'off' && (
          <div className="muted small">
            {j.ok_ts || j.run_ts
              ? t('Таймер выключен. Алерт придет, если свежего бэкапа не будет дольше обычного.')
              : t('Таймер выключен, задание еще ни разу не запускалось.')}
          </div>
        )}
        {j.status === 'ran' && (
          <div className="muted small">
            {t('Запуски видны по журналу cron, а итог — нет: скрипт не пишет ни метрик, ни отметки о результате, а файлов бэкапа панель не нашла.')}
          </div>
        )}
        {src.length > 0 && <div className="muted small mono own-backup-src">{src.join(' · ')}</div>}
      </div>
      <div className="own-backup-side">
        <span className={`small ${tone[j.status]}`}>{label[j.status]}</span>
        {canManage && (
          <button className="ghost small" disabled={busy} onClick={onToggle}>
            {j.ignored ? t('отслеживать') : t('не отслеживать')}
          </button>
        )}
      </div>
    </div>
  )
}

function OwnBackups({ server: s, canManage, onChanged, bare = false }: {
  server: Server; canManage: boolean; onChanged: () => void
  bare?: boolean // без своего заголовка: он уже в шапке раздела окна
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState('')
  const [showIgnored, setShowIgnored] = useState(false)
  const jobs = s.custom_backups ?? []
  if (jobs.length === 0) return null
  const active = jobs.filter((j) => !j.ignored)
  const ignored = jobs.filter((j) => j.ignored)
  const toggle = async (j: CustomBackup) => {
    setBusy(j.id)
    setErr('')
    try {
      await setCustomBackupIgnored(s.id, j.id, !j.ignored)
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(null)
    }
  }
  const row = (j: CustomBackup) => (
    <OwnBackupRow key={j.id} job={j} canManage={canManage} busy={busy === j.id} onToggle={() => toggle(j)} />
  )
  return (
    <div className="chart-card net-card net-card-wide own-backups">
      {!bare && (
        <div className="net-head">
          <div className="chart-cap net-cap">
            {t('Свои бэкапы на ноде')}
            <span className={`net-n${ownBackupProblems(s).length ? ' net-n-bad' : ''}`}>{active.length}</span>
          </div>
        </div>
      )}
      <div className="net-sub">
        {t('Настроены без панели. Панель нашла их сама и следит за их работой, ничего на ноде не меняя.')}
      </div>
      {active.length > 0 && <div className="own-backups-list">{active.map(row)}</div>}
      {err && <p className="form-error small">{err}</p>}
      {ignored.length > 0 && (
        <button className="ghost small stale-more" onClick={() => setShowIgnored(!showIgnored)}>
          {showIgnored ? t('скрыть неотслеживаемые') : t('не отслеживаются ({n})', { n: ignored.length })}
        </button>
      )}
      {showIgnored && <div className="own-backups-list">{ignored.map(row)}</div>}
    </div>
  )
}

// Окно бэкапа ноды: меню разделов слева, на экране один раздел, как у карточки сервера
// (cardLayout). Раньше сверху шли плитки, под ними тот же набор строкой "ключ - значение", потом
// покрытие, свои бэкапы и управление одной лентой. Куда бэкапится - в строке под именем
const BK_SECTIONS: SecDef[] = [
  { id: 'overview', label: 'Сводка', group: '' },
  { id: 'runs', label: 'Прогоны', group: 'Бэкап' },
  { id: 'coverage', label: 'Покрытие', group: 'Бэкап' },
  { id: 'own', label: 'Свои бэкапы', group: 'Бэкап' },
  { id: 'restore', label: 'Восстановление', group: 'Бэкап' },
  { id: 'manage', label: 'Расписание и пути', group: 'Настройка' },
]
const BK_SEC_IDS = new Set(BK_SECTIONS.map((x) => x.id))
// раздел по виду проблемы, если бэкенд его не назвал
const BK_SEC_OF_KIND: Record<string, string> = {
  backup_dump: 'coverage', backup_dump_space: 'coverage', backup_cron: 'coverage', backup_restore: 'restore',
}

// Перевод на https - задача на бэкенде (api/servers.py _orchestrate_to_https): шаги и итог
function useHttpsSteps(): Record<string, string> {
  const { t } = useI18n()
  return {
    'tls-front': t('TLS-фронт на бэкап-сервере'),
    'get-cert': t('сертификат'),
    'allow-tls': t('TLS-порт клиенту на сервере бэкапов'),
    'to-https': t('проверка по https и переключение'),
  }
}

// ждать задачу до конца, отдавая каждое новое состояние
async function waitJob(serverId: number, j: BackupSetupJob, alive: { current: boolean },
  onStep: (j: BackupSetupJob) => void): Promise<BackupSetupJob> {
  let cur = j
  while (cur.status === 'running' && alive.current) {
    await new Promise((r) => setTimeout(r, 2500))
    if (!alive.current) break
    cur = await backupSetupStatus(serverId, cur.id)
    onStep(cur)
  }
  return cur
}

function HttpsSteps({ job }: { job: BackupSetupJob }) {
  const { t, lang } = useI18n()
  const lbl = useHttpsSteps()
  return (
    <div className="backup-setup-progress">
      {job.steps.map((st, i) => (
        <div key={i} className={`setup-step ${st.ok ? 't-up' : 't-down'}`}>
          <span className="mono">{st.ok ? '✓' : '✗'}</span> {lbl[st.step] || st.step}
          {!st.ok && st.detail && <span className="muted small"> - {st.detail}</span>}
        </div>
      ))}
      {job.status === 'running' && (
        <div className="setup-step muted"><span className="sdot sdot-unknown" /> {t('выполняется…')}</div>
      )}
      {job.status !== 'running' && (
        <div className={`setup-final ${job.status === 'done' ? 't-up' : 'form-error'}`}>
          {job.status === 'done' ? `✅ ${t('бэкап идет по https')}` : `❌ ${lang === 'en' ? trBackend(job.message) : job.message}`}
        </div>
      )}
    </div>
  )
}

const HTTPS_HOW = 'Перевод на TLS rest-server того же сервера бэкапов (порт 64101, сертификат самоподписанный): сервер бэкапов открывает клиенту порт, клиент читает репозиторий по https и переключается, только если это получилось. Не получилось - бэкап остается на http. Логин, пароль и репозиторий те же, снапшоты остаются.'

// Бэкап идет по http через интернет (app/backup_target.py): логин и пароль rest-server летят открытым
// текстом. Не авария, а дело: пункт "Требует действий", здесь - кнопка "перевести на https" и решение
// "оставить http" (alert_mutes bkhttp:repo), принятое видно приглушенным с "вернуть"
function BackupHttp({ server: s, canManage, onChanged }: {
  server: Server
  canManage: boolean
  onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [job, setJob] = useState<BackupSetupJob | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  const tg = s.backup_target
  if (!tg) return null
  // перешел на https: панель ставит новый адрес сразу по ответу helper'а, плашка уходит вместе с
  // пунктом в меню, а итог шагов остается на экране
  if (!tg.insecure) return job ? <HttpsSteps job={job} /> : null
  const blockers = s.backup_https?.blockers ?? []
  const running = job?.status === 'running'
  const toggle = () => {
    const cur = s.alert_mutes ?? []
    setBusy(true)
    updateServer(s.id, { alert_mutes: tg.muted ? cur.filter((m) => m !== BACKUP_HTTP_MUTE) : [...new Set([...cur, BACKUP_HTTP_MUTE])] })
      .then(onChanged)
      .catch(() => {})
      .finally(() => setBusy(false))
  }
  const move = async () => {
    if (!window.confirm(t('Перевести бэкап {name} на https? Если проверка по https не пройдет, бэкап останется на http.', { name: s.name }))) return
    setErr(null)
    try {
      const j = await backupToHttps(s.id)
      setJob(j)
      const fin = await waitJob(s.id, j, alive, setJob)
      if (fin.status === 'done' && alive.current) onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('ошибка'))
    }
  }
  return (
    <div>
      <div className={`osupd-adv ${tg.muted ? 'osupd-adv-off' : 'net-note'}`}>
        <span className="osupd-adv-text">
          {t('бэкап идет на {where} по http без шифрования: логин и пароль репозитория видны по дороге', { where: backupDestWhere(tg) })}
        </span>
        {tg.muted && <span className="osupd-adv-why">{t('http оставлен')}</span>}
        {canManage && !tg.muted && job?.status !== 'done' && (
          <button className="ghost small row-act" disabled={running || blockers.length > 0} onClick={move}
            title={blockers.length ? blockers.join('; ') : t(HTTPS_HOW)}>
            🔒 {t('перевести на https')}
          </button>
        )}
        {canManage && (
          <button className="ghost small row-act" disabled={busy || running} onClick={toggle}
            title={tg.muted ? undefined : t('Так задумано: убрать из "Требует действий"')}>
            {tg.muted ? t('вернуть') : t('оставить http')}
          </button>
        )}
      </div>
      {!tg.muted && !job && (
        <div className="muted small">
          {blockers.length > 0 ? `${t('Перевести пока нельзя')}: ${blockers.join('; ')}` : t(HTTPS_HOW)}
        </div>
      )}
      {err && <div className="form-error small">{err}</div>}
      {job && <HttpsSteps job={job} />}
    </div>
  )
}

// Клиенты этого сервера бэкапов, которые ходят по http: перевод на https галками, по очереди
// (бэкенд ведет очередь сам, следующий начинается после предыдущего). Итог - по каждому серверу
function BsrvHttps({ server: bs, clients, canAct, onChanged }: {
  server: Server; clients: Server[]; canAct: boolean; onChanged: () => void
}) {
  const { t, lang } = useI18n()
  const lbl = useHttpsSteps()
  const [sel, setSel] = useState<Set<number>>(new Set())
  const [jobs, setJobs] = useState<Record<number, BackupSetupJob>>({})
  const [active, setActive] = useState<number | null>(null) // чей перевод идет сейчас
  // кого отправили переводить: перешедший на https уходит из списка клиентов по http, а его итог
  // должен остаться на экране вместе с остальными
  const [sent, setSent] = useState<Server[]>([])
  const rows = [...clients, ...sent.filter((x) => !clients.some((c) => c.id === x.id))]
  const [err, setErr] = useState<string | null>(null)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  const running = Object.values(jobs).some((j) => j.status === 'running')
  const ready = clients.filter((c) => !(c.backup_https?.blockers ?? []).length)
  const flip = (id: number) => setSel((prev) => {
    const n = new Set(prev)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    return n
  })
  const run = async () => {
    const ids = clients.filter((c) => sel.has(c.id)).map((c) => c.id)
    if (!ids.length) return
    if (!window.confirm(t('Перевести на https по очереди: {n}? У кого проверка по https не пройдет, бэкап останется на http.', { n: ids.length }))) return
    setErr(null)
    try {
      const list = await backupServerToHttps(bs.id, ids)
      setSent(clients.filter((c) => sel.has(c.id)))
      setJobs(Object.fromEntries(list.map((j) => [j.server_id, j])))
      setSel(new Set())
      // очередь идет на бэкенде; опрашиваем по порядку - пока ждем первого, остальные в очереди
      for (const j of list) {
        if (!alive.current) return
        setActive(j.server_id)
        await waitJob(j.server_id, j, alive, (cur) => setJobs((prev) => ({ ...prev, [j.server_id]: cur })))
      }
      setActive(null)
      if (alive.current) onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('ошибка'))
    }
  }
  const state = (c: Server): ReactNode => {
    const j = jobs[c.id]
    if (!j) return null
    if (j.status === 'done') return <span className="t-up">✓ {t('по https')}</span>
    if (j.status === 'error') return <span className="t-down">✗ {lang === 'en' ? trBackend(j.message) : j.message}</span>
    if (c.id !== active) return <span className="muted">{t('в очереди')}</span>
    const last = j.steps[j.steps.length - 1]
    return <span className="muted">{last ? `${t('идет')}: ${lbl[last.step] || last.step}` : t('идет')}</span>
  }
  return (
    <div className="srv-sec">
      <div className="muted small">{t(HTTPS_HOW)}</div>
      <div className="chart-card net-card net-card-wide">
        <div className="net-head">
          <div className="chart-cap net-cap">
            {t('Клиенты по http')}
            <span className="net-n">{clients.length}</span>
          </div>
          {canAct && (
            <div className="backup-manage-actions">
              <button className="ghost small" disabled={running || !ready.length}
                onClick={() => setSel(new Set(ready.filter((c) => !c.backup_target?.muted).map((c) => c.id)))}>
                {t('выбрать всех, кого можно')}
              </button>
              <button className="small" disabled={running || sel.size === 0} onClick={run}>
                🔒 {t('перевести на https: {n}', { n: sel.size })}
              </button>
            </div>
          )}
        </div>
        {err && <div className="form-error small">{err}</div>}
        <div className="loc-results">
          {rows.map((c) => {
            const why = c.backup_https?.blockers ?? []
            return (
              <label key={c.id} className="loc-res bsrv-https-row">
                <input type="checkbox" checked={sel.has(c.id)} disabled={!canAct || running || why.length > 0 || jobs[c.id]?.status === 'done'}
                  onChange={() => flip(c.id)} />
                <span className="hs-host-main">
                  <span className="hs-host-name">
                    <CountryFlag code={c.country} />
                    {c.name}
                    {c.backup_target?.muted && <span className="type-chip off">{t('http оставлен')}</span>}
                  </span>
                  <span className="backup-row-meta mono muted small">{backupDestWhere(c.backup_target)}</span>
                  {why.length > 0 && <span className="muted small">{t('Перевести пока нельзя')}: {why.join('; ')}</span>}
                </span>
                <span className="small">{state(c)}</span>
              </label>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function BackupModal({
  server: s,
  backup: b,
  repo,
  dest,
  canManage,
  initialSec,
  onChanged,
  onClose,
  onReinstall,
  onOpenDest,
}: {
  server: Server
  backup: BackupInfo
  repo?: RepoStat
  dest: BackupDest | null // сервер бэкапов, куда идет этот бэкап, и адрес
  canManage: boolean
  initialSec?: string | null // из ссылки алерта или с главной: ?backup=id&sec=coverage
  onChanged: () => void
  onClose: () => void
  onReinstall?: () => void // "Настроить бэкап" поверх существующего (форма спросит согласие)
  onOpenDest?: (s: Server) => void // открыть сервер бэкапов из строки "куда"
}) {
  const { isAdmin } = useAuth()
  const { t } = useI18n()
  const [rbusy, setRbusy] = useState(false)
  const [rmsg, setRmsg] = useState<string | null>(null)
  // оптимистично прячем кнопку сразу после успеха: версия в отчёте обновится лишь со
  // следующим тиком агента (~15с), а «кнопка не пропала» читается как «не сработало»
  const [rdone, setRdone] = useState(false)
  const [active, setActive] = useState<string>(() => (BK_SEC_IDS.has(initialSec ?? '') ? initialSec! : 'overview'))
  const layoutRef = useRef<HTMLDivElement>(null)
  const go = (id: string) => {
    setActive(id)
    const el = layoutRef.current
    if (el && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: 'start' })
  }
  const st = backupStatus(b)
  const stLabel: Record<BStatus, string> = {
    ok: t('свежий'), failed: t('ошибка'), stale: t('не свежий'),
    skipped: t('пропущен'), unknown: t('нет метрики'),
  }
  const resticVer = (b.restic_version || '').match(/restic\s+(\S+)/i)?.[1] ?? ''
  const resticOld = !!resticVer && resticVer !== RESTIC_TARGET
  const updateRestic = async () => {
    if (!window.confirm(t('Обновить restic до {v}? Скачает с github, сверит sha256, заменит бинарь. Конфиг бэкапа не трогается.', { v: RESTIC_TARGET }))) return
    setRbusy(true); setRmsg(null)
    try {
      const res = await runAndWait(s.id, { action: 'restic_update' })
      const ok = res.status !== 'error'
      setRmsg(ok ? (helperText(res.result, t) || t('готово')) : (helperText(res.result, t) || t('не удалось')))
      if (ok) setRdone(true)   // прячем кнопку до подтверждения отчётом
      onChanged()
    } catch (e) {
      setRmsg(e instanceof Error ? e.message : t('ошибка'))
    } finally {
      setRbusy(false)
    }
  }
  const row = (k: string, v: ReactNode) => (
    <div className="kv-row">
      <span className="kv-k muted small">{k}</span>
      <span className="kv-v mono small">{v}</span>
    </div>
  )
  // сколько restic добавляет за прогон: это и раздувает бэкап-сервер
  const runs = backupRuns(s)
  const last = runs.length ? runs[runs.length - 1] : null
  const usual = runs.length > 1 ? usualGrowth(runs.slice(0, -1)) : 0
  const dumpBytes = (b.dumps ?? []).reduce((a, d) => a + (d.size_bytes || 0), 0)
  const dumpFiles = (b.dumps ?? []).reduce((a, d) => a + (d.files || 0), 0)
  const hm = (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  // full > restic заметно → показываем обе: дамп занимает время ДО restic,
  // и по одной restic-длительности не понять, когда бэкап реально закончился
  const withDumps = (b.full_duration_sec || 0) > (b.duration_sec || 0) + 2
  const win = backupWindowNote(s)
  // последний прогон упал: по метрике, а без нее - по Result юнита (backup_state.backup_failed)
  const runFailed = b.metric_present ? b.success === 0
    : b.ts_source === 'systemd' && !!b.service_result && b.service_result !== 'success'
  const v = s.backup_verify
  const helperOld = canManage && b.manageable && (b.helper_version || 0) < CURRENT_BACKUP_HELPER
  const audit = (s.backup_audit ?? []).filter((x) => !x.muted)
  const own = s.custom_backups ?? []
  const ownBad = ownBackupProblems(s)
  const mutes = new Set(s.alert_mutes ?? [])
  // бэкап по http через интернет и не решено "оставить": пункт "Требует действий"
  const httpOpen = !!s.backup_target?.insecure && !s.backup_target.muted

  const shown: Record<string, boolean> = {
    overview: true,
    runs: true,
    coverage: (s.backup_audit?.length ?? 0) > 0,
    own: own.length > 0,
    restore: true,
    manage: true,
  }
  const secs = BK_SECTIONS.filter((x) => shown[x.id])
  const cur = secs.find((x) => x.id === active) ?? secs[0]
  useUrlParam('sec', cur.id === 'overview' ? null : cur.id)

  // Замечания по разделам - те же, что в "Что сломано" на главной: прогон с ошибкой, бэкап не
  // свежий, скачок прироста, свои бэкапы, дампы и проверка восстановления с бэкенда. Отдельный
  // дамп для базы - совет из "Требует действий"
  const items: Record<string, SecItem[]> = {}
  const add = (sec: string, x: SecItem) => (items[sec] ??= []).push(x)
  if (s.online) {
    if (b.metric_present && b.success === 0)
      add('runs', {
        level: 2, since: s.alert_since?.backup_failed,
        text: s.backup_state?.reason ? t('бэкап завершился с ошибкой: {why}', { why: s.backup_state.reason }) : t('бэкап завершился с ошибкой'),
      })
    else if (b.metric_present && b.last_backup_ts && Date.now() / 1000 - b.last_backup_ts > STALE_SECONDS)
      add('runs', { level: 1, text: t('бэкап не свежий: последний {ago}', { ago: fmtAgo(b.last_backup_ts) }), since: s.alert_since?.backup_stale })
    const g = s.backup_growth
    if (g && !mutes.has('backup_growth'))
      add('runs', { level: 1, text: g.text, since: new Date(g.ts * 1000).toISOString() })
    if (ownBad.length)
      add('own', {
        level: ownBad.some((j) => j.status === 'failed') ? 2 : 1,
        count: ownBad.length,
        since: s.alert_since?.backup_custom,
        text: t('свой бэкап не отрабатывает: {jobs}', { jobs: ownBad.map((j) => `${j.name} (${j.problem})`).join(', ') }),
      })
    for (const i of srvIssues(s, t).filter((x) => x.section === 'backups' && !x.srv)) {
      const sec = BK_SEC_IDS.has(i.sec ?? '') ? i.sec! : (BK_SEC_OF_KIND[i.kind ?? ''] ?? 'runs')
      add(sec, { level: i.tone === 't-down' ? 2 : 1, text: i.text, since: i.since })
    }
    if (httpOpen)
      add('manage', { level: 1, text: t('бэкап идет по http без шифрования: логин и пароль репозитория видны по дороге') })
    const dbs = audit.filter((x) => x.kind === 'db')
    if (dbs.length && !s.db_dumps_ok)
      add('coverage', {
        level: 1,
        count: dbs.length,
        text: t('{db} - нужен отдельный дамп', { db: shortList(dbAuditNames(dbs), t, 3) }),
      })
  }
  const okDb = audit.filter((x) => x.kind === 'db_ok').length
  const ok: Record<string, string> = {
    runs: b.last_backup_ts
      ? last
        ? t('последний {ago}, добавил {s}', { ago: fmtAgo(b.last_backup_ts), s: fmtBytes(last.st) })
        : t('последний {ago}', { ago: fmtAgo(b.last_backup_ts) })
      : t('бэкапа еще не было'),
    coverage: okDb ? t('бэкапится баз: {n}', { n: okDb }) : t('баз не найдено'),
    own: t('заданий: {n}, все отрабатывают', { n: own.filter((j) => !j.ignored).length }),
    restore: v
      ? t('проверено {d}: файлов совпало {n}{dumps}', {
          d: v.ts ? new Date(v.ts).toLocaleDateString() : '?',
          n: v.files_ok,
          dumps: v.dumps_ok.length ? t(', дампы читаются: {e}', { e: v.dumps_ok.join(', ') }) : '',
        })
      : t('проверки еще не было'),
    manage: b.manageable
      ? subOf(
          b.schedule && t('каждый день {time}', { time: b.schedule }),
          b.mode === 'include'
            ? t('только {n} путей', { n: (b.includes ?? []).length })
            : t('исключено путей: {n}', { n: (b.excludes ?? []).length }),
        )
      : t('управление недоступно без helper'),
  }
  const info: Record<string, SecInfo> = {}
  for (const { id } of BK_SECTIONS) info[id] = secInfoOf(items[id] ?? [], ok[id] ?? '', t)
  // Не проблема, но и не "все в порядке": прогон не уложился в окно или пропущен, дыры в
  // покрытии, старый restic или helper. Желтым на карте, без числа в меню
  const soft = (id: string, line: string) => {
    if (!info[id].level) info[id] = { ...info[id], level: 1, line }
  }
  if (win) soft('runs', win)
  else if (st !== 'ok' && st !== 'unknown' && !items.runs) soft('runs', stLabel[st])
  const gaps = audit.filter((x) => x.gap)
  if (gaps.length) soft('coverage', t('не бэкапится: {list}', { list: shortList(gaps.map((x) => x.subject), t, 3) }))
  if (!b.restic_found) soft('manage', t('restic не найден'))
  else if (resticOld) soft('manage', t('restic {v}, есть {t}', { v: resticVer, t: RESTIC_TARGET }))
  else if (helperOld) soft('manage', t('helper на ноде устарел'))
  if (!v && !info.restore.level) info.restore.idle = true
  markMuted(info, srvMuted(s, t).filter((i) => i.section === 'backups' && !i.srv).map((i) => ({
    sec: BK_SEC_IDS.has(i.sec ?? '') ? i.sec! : (BK_SEC_OF_KIND[i.kind ?? ''] ?? 'runs'), text: i.text,
  })), t)

  const rows: OvRow[] = BK_SECTIONS.flatMap(({ id, label }) =>
    (items[id] ?? []).map((x) => ({
      tone: x.level >= 2 ? ('t-down' as const) : ('t-degraded' as const),
      text: x.text,
      since: x.since,
      act: shown[id] ? { label: t(label), onClick: () => go(id) } : undefined,
    })),
  ).sort((a, b) => Number(b.tone === 't-down') - Number(a.tone === 't-down'))
  const tileLabel = (sec: string, label: string) =>
    info[sec].fresh ? <>{label} <FreshChip since={info[sec].fresh!} /></> : label

  return createPortal(
    <div className="modal-backdrop">
      <div className="card detail detail-server hs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
            <OsIcon os={s.os} size={18} />
            <CountryFlag code={s.country} />
            <h3>{s.name}</h3>
            {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          </div>
          <div className="detail-head-actions">
            <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
              ✕
            </button>
          </div>
        </div>
        <div className="srv-meta srv-meta-line">
          {dest && (
            <span className="srv-meta-item">
              <span className="srv-meta-k">{t('куда')}</span> <DestLabel dest={dest} onOpen={onOpenDest} />
            </span>
          )}
          <span className="srv-meta-item">
            <span className="srv-meta-k">restic</span>{' '}
            <span className={!b.restic_found ? 't-down' : resticOld ? 't-degraded' : undefined}>
              {resticVer || (b.restic_found ? t('найден') : t('не найден'))}
            </span>
          </span>
          {b.schedule && (
            <span className="srv-meta-item">
              <span className="srv-meta-k">{t('расписание')}</span> {b.schedule}
              {s.backup_anytime ? '' : `, ${t('до {h}:00', { h: s.backup_deadline_hour ?? 8 })}`}
            </span>
          )}
          {!!b.helper_version && (
            <span className="srv-meta-item"><span className="srv-meta-k">helper</span> {b.helper_version}</span>
          )}
          <span className="srv-meta-item">
            <span className="srv-meta-k">{t('обновлено')}</span> {fmtRel(s.last_seen)}
          </span>
        </div>
        <div className="srv-layout" ref={layoutRef}>
          <CardNav secs={secs} active={cur.id} info={info} onGo={go} />
          <div className="srv-main">
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
            </div>

            {cur.id === 'overview' && (
              <div className="srv-sec">
                <OvRows rows={rows} />
                <div className="net-tiles srv-tiles">
                  <button className="srv-tile-btn" onClick={() => go('runs')} title={t('к разделу')}>
                    <NetTile label={tileLabel('runs', t('Последний бэкап'))} value={fmtAgo(b.last_backup_ts)} sub={stLabel[st]} tone={TILE[st]} />
                  </button>
                  <button className="srv-tile-btn" onClick={() => go('runs')} title={t('к разделу')}>
                    <NetTile
                      label={t('Длительность')}
                      value={fmtDur(withDumps ? b.full_duration_sec : b.duration_sec)}
                      sub={subOf(
                        withDumps && t('с дампами'),
                        (b.started_ts || 0) > 0 && !!b.last_backup_ts && `${hm(b.started_ts!)}-${hm(b.last_backup_ts)}`,
                      ) || undefined}
                      tone={win ? 'warn' : undefined}
                    />
                  </button>
                  {repo && (repo.size_bytes || 0) > 0 && (
                    <button className="srv-tile-btn" onClick={() => go('runs')} title={t('к разделу')}>
                      <NetTile label={t('В репозитории')} value={fmtBytes(repo.size_bytes)} sub={t('снапшотов: {n}', { n: repo.snapshots })} />
                    </button>
                  )}
                  {v && (
                    <button className="srv-tile-btn" onClick={() => go('restore')} title={t('к разделу')}>
                      <NetTile
                        label={tileLabel('restore', t('Восстановление'))}
                        value={v.level >= 2 ? t('не прошло') : v.problems.length ? t('замечания') : t('проверено')}
                        sub={v.ts ? new Date(v.ts).toLocaleDateString() : undefined}
                        tone={v.level >= 2 ? 'bad' : v.problems.length ? 'warn' : 'ok'}
                      />
                    </button>
                  )}
                </div>
                <OvAreas
                  cap={t('Бэкап по областям')}
                  items={BK_SECTIONS.filter((x) => x.id !== 'overview' && shown[x.id]).map((x) => ({ id: x.id, label: x.label, info: info[x.id] }))}
                  onGo={go}
                />
              </div>
            )}

            {cur.id === 'runs' && (
              <div className="srv-sec">
                {(b.notes ?? []).map((n, i) => (
                  <div key={i} className="net-prob">{n}</div>
                ))}
                {win && <div className="net-note">{t('Не уложился в окно')}: {win}</div>}
                {/* почему упал прогон: коротко, под ним строка restic свернутой (helper backup-setup 0.39+) */}
                {runFailed && s.backup_state?.reason && (
                  <div className="net-prob">
                    {t('Почему ошибка')}: {s.backup_state.reason}
                    {s.backup_state.reason_raw && s.backup_state.reason_raw !== s.backup_state.reason && (
                      <details className="fix-more">
                        <summary>{t('текст ошибки restic')}</summary>
                        <pre className="web-err-lines mono">{s.backup_state.reason_raw}</pre>
                      </details>
                    )}
                  </div>
                )}
                <div className="chart-card net-card net-card-wide">
                  <div className="kv-list">
                    {row(t('Последний бэкап'), b.last_backup_ts
                      ? `${fmtAgo(b.last_backup_ts)} · ${new Date(b.last_backup_ts * 1000).toLocaleString()}`
                      : '—')}
                    {row(t('Результат'), b.metric_present
                      ? (b.success === 1 ? t('успех') : t('ошибка'))
                      : b.ts_source === 'systemd'
                        ? `${b.service_result === 'success' ? t('успех') : b.service_result || t('нет данных')} ${t('(по systemd)')}`
                        : t('нет метрики'))}
                    {row(t('Длительность'), withDumps
                      ? `${fmtDur(b.full_duration_sec)} ${t('(дампы + restic)')} · restic ${fmtDur(b.duration_sec)}`
                      : fmtDur(b.duration_sec))}
                    {(b.started_ts || 0) > 0 && b.last_backup_ts && row(t('Окно бэкапа'),
                      `${hm(b.started_ts!)} → ${hm(b.last_backup_ts)}`)}
                    {row(t('Пропущен (лок занят)'), b.skipped ? t('да') : '—')}
                    {row(t('Таймер'), `${b.timer_enabled ? t('включён') : t('выключен')} · ${b.timer_active ? t('активен') : t('неактивен')}`)}
                    {b.service_result && row(t('Сервис'), b.service_result)}
                    {/* объёмы: дампы БД (что кладём на диск перед restic) + физический размер репо
                        на бэкап-сервере (вся история, дедуп+сжатие). Логический размер снапшота требует
                        пароля репо (агенту недоступен) — не показываем, репо-размер понятнее. */}
                    {dumpBytes > 0 && row(t('Дампы БД'), `${fmtBytes(dumpBytes)} · ${t('файлов: {n}', { n: dumpFiles })}`)}
                    {repo && (repo.size_bytes || 0) > 0 && row(t('В репозитории'),
                      `${fmtBytes(repo.size_bytes)} · ${t('снапшотов: {n}', { n: repo.snapshots })}`)}
                    {last && row(t('Последний прогон'), t('добавил {s} (до сжатия {a}) · прочитано {p} за {d}', {
                      s: fmtBytes(last.st), a: fmtBytes(last.add), p: fmtBytes(last.proc), d: fmtDur(last.dur),
                    }))}
                    {runs.length > 1 && row(t('Обычно за прогон'), fmtBytes(usual))}
                  </div>
                </div>
                {last && (
                  <div className="chart-card net-card net-card-wide">
                    <div className="net-head">
                      <div className="chart-cap">{t('Прирост по прогонам')}</div>
                    </div>
                    <div className="growth-wrap">
                      <GrowthBars runs={runs.slice(-40)} />
                      <div className="muted small">
                        {t('сколько каждый прогон добавил в репозиторий, последние {n}, свежий справа', { n: Math.min(runs.length, 40) })}
                      </div>
                      {s.backup_growth && <div className="small t-degraded">⚠ {s.backup_growth.text}</div>}
                    </div>
                  </div>
                )}
              </div>
            )}

            {cur.id === 'coverage' && (
              <CoverageAudit server={s} canManage={canManage} onChanged={onChanged} bare
                onSchedule={b.manageable && canManage ? () => go('manage') : undefined} />
            )}

            {cur.id === 'own' && <OwnBackups server={s} canManage={canManage} onChanged={onChanged} bare />}

            {cur.id === 'restore' && (
              <div className="srv-sec">
                {v ? (
                  v.problems.length ? (
                    v.problems.map((p) => (
                      <div key={p} className={v.level >= 2 ? 'net-prob' : 'net-note'}>{p}</div>
                    ))
                  ) : (
                    <div className="small t-up">{ok.restore}</div>
                  )
                ) : (
                  <div className="srv-empty">{t('Проверки восстановления еще не было: ее делает helper бэкапа на ноде (backup-setup 0.34 и новее).')}</div>
                )}
                {/* пароль репозитория = ключ дешифровки бэкапа: только админу (см. бэкенд) */}
                {isAdmin && b.manageable && (
                  <div className="chart-card net-card net-card-wide">
                    <RestoreInfo server={s} />
                  </div>
                )}
              </div>
            )}

            {cur.id === 'manage' && (
              <div className="srv-sec">
                <div className="kv-list">
                  {row('restic', (
                    <span className="restic-row">
                      {b.restic_found ? (shortRestic(b.restic_version) || t('найден')) : t('не найден')}
                      {canManage && resticOld && !rdone && (
                        <button className="restic-update-btn" disabled={rbusy} onClick={updateRestic}>
                          {rbusy ? '…' : `⬆ ${t('обновить до {v}', { v: RESTIC_TARGET })}`}
                        </button>
                      )}
                      {rdone && <span className="t-up small">✓ {t('обновлено до {v} — версия в шапке обновится через минуту', { v: RESTIC_TARGET })}</span>}
                      {rmsg && !rdone && <span className="muted small"> · {rmsg}</span>}
                    </span>
                  ))}
                </div>
                {s.backup_target && <BackupHttp server={s} canManage={canManage} onChanged={onChanged} />}
                {helperOld && (
                  <div className="net-note">{t('helper на ноде устарел — переустановите, чтобы подхватить новые фичи/фиксы:')}</div>
                )}
                {helperOld && <EnableManage />}
                {canManage && b.manageable ? (
                  <ManageSection server={s} backup={b} onChanged={onChanged} />
                ) : (
                  <div className="net-foot">
                    {t('Статус читается без секретов. Управление доступно после установки helper на ноде (кнопка ниже).')}
                  </div>
                )}
                {!b.manageable && <EnableManage />}
                {canManage && onReinstall && (
                  <div className="backup-manage-actions">
                    <button className="ghost" onClick={onReinstall}>{t('Переустановить бэкап')}</button>
                    <span className="muted small">{t('настройка заново поверх этой: бэкап-сервер, пути, расписание')}</span>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}


// подсказка для включения управления (backup-setup.sh на клиентской ноде)
function EnableManage() {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const cmd = `curl -fsSL ${window.location.origin}/api/agent/backup-setup.sh | sudo bash`
  return (
    <div className="backup-enable">
      <div className="muted small">
        {t('Чтобы управлять бэкапом из панели, поставьте узкий helper (root + sudoers только на бэкап-операции, не cluster-admin):')}
      </div>
      <div className="agent-advice-cmd">
        <pre>{cmd}</pre>
        <button
          className="ghost"
          onClick={() => {
            navigator.clipboard?.writeText(cmd)
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          }}
        >
          {copied ? t('Скопировано') : t('Копировать')}
        </button>
      </div>
    </div>
  )
}

// «Данные для восстановления» — по кнопке достаёт с ноды repo URL + пароль (ключ дешифровки).
// В БД не хранится; показываем осознанно, с предупреждением сохранить в vault.
function RestoreInfo({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const [creds, setCreds] = useState<BackupCreds | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [copied, setCopied] = useState('')
  const reveal = async () => {
    setBusy(true); setErr(null)
    try {
      setCreds(await backupCredentials(s.id))
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('ошибка'))
    } finally {
      setBusy(false)
    }
  }
  const copy = (v: string, key: string) => {
    navigator.clipboard?.writeText(v); setCopied(key); window.setTimeout(() => setCopied(''), 1500)
  }
  // локальный путь репо на бэкап-сервере: из fallback (repo_local) или из URL клиента
  const localRepo = creds
    ? (creds.repo_local || '/app/rest-server/data/' + (creds.repo_url.split('/').pop() || ''))
    : ''
  const restoreCmd = creds
    ? `RESTIC_REPOSITORY='${localRepo}' RESTIC_PASSWORD='${creds.repopass}' restic snapshots`
    : ''
  return (
    <div className="restore-info">
      {!creds ? (
        <div className="backup-manage-actions">
          <button className="ghost" disabled={busy} onClick={reveal}>
            {busy ? '…' : `🔑 ${t('Данные для восстановления')}`}
          </button>
          {err && <span className="form-error small">{err}</span>}
        </div>
      ) : (
        <div className="restore-creds">
          <div className="form-error small">⚠️ {t('Это ключ дешифровки бэкапа. Сохраните в vault и никому не передавайте.')}</div>
          {creds.source === 'backup-server' && (
            <div className="muted small">{t('Клиент недоступен — пароль взят с бэкап-сервера')}{creds.server_name ? ` (${creds.server_name})` : ''}.</div>
          )}
          <div className="creds-row">
            <span className="muted small">{t('Пароль репозитория')}</span>
            <code className="mono">{creds.repopass}</code>
            <button className="ghost" onClick={() => copy(creds.repopass, 'p')}>{copied === 'p' ? '✓' : t('Копировать')}</button>
          </div>
          {creds.repo_url && (
            <div className="creds-row">
              <span className="muted small">Repo URL</span>
              <code className="mono creds-url">{creds.repo_url}</code>
              <button className="ghost" onClick={() => copy(creds.repo_url, 'u')}>{copied === 'u' ? '✓' : t('Копировать')}</button>
            </div>
          )}
          <div className="muted small">{t('Восстановление проще всего на самом бэкап-сервере (локально):')}</div>
          <div className="agent-advice-cmd">
            <pre>{restoreCmd}</pre>
            <button className="ghost" onClick={() => copy(restoreCmd, 'c')}>{copied === 'c' ? '✓' : t('Копировать')}</button>
          </div>
        </div>
      )}
    </div>
  )
}

// systemd-таймер бэкапа → иконка + подпись + тон (для строки клиента)
function timerBits(b: BackupInfo, t: (k: string) => string): { icon: string; text: string; tone: string } {
  if (!b.timer_enabled) return { icon: '⚪', text: t('таймер выкл'), tone: 'muted' }
  if (!b.timer_active) return { icon: '🟡', text: t('неактивен'), tone: 't-degraded' }
  return { icon: '🟢', text: t('активен'), tone: 't-up' }
}

function HostRow({
  server: s,
  backup: b,
  showGroup,
  dest,
  hint,
  onOpen,
}: {
  server: Server
  backup: BackupInfo
  showGroup?: boolean
  dest?: BackupDest | null
  hint?: string | null // что из нажатой плитки нашлось у клиента
  onOpen: () => void
}) {
  const { t } = useI18n()
  const st = backupStatus(b)
  const stLabel: Record<BStatus, string> = {
    ok: t('свежий'), failed: t('ошибка'), stale: t('не свежий'),
    skipped: t('пропущен'), unknown: t('нет метрики'),
  }
  const tm = timerBits(b, t)
  // «покрытие» — базы/тома, которые решили не бэкапить: аудит по ним молчит навсегда
  const auditMutes = collectMutes(s, t, ['audit'])
  return (
    <button className="loc-res loc-res-btn hs-host" onClick={onOpen}>
      <span className={`lk-dot ${s.online ? 'lk-dot-ok' : 'lk-dot-bad'}`} title={s.online ? t('на связи') : t('не на связи')} />
      <div className="hs-host-main">
        <div className="hs-host-name">
          <OsIcon os={s.os} />
          <CountryFlag code={s.country} />
          {s.name}
          {showGroup && s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          {b.restic_version && <span className="type-chip mono">{shortRestic(b.restic_version)}</span>}
          {!b.configured && <span className="type-chip off">{t('не настроен')}</span>}
          <OwnBackupsChip server={s} />
          {auditMutes.length > 0 && <MuteChip items={auditMutes} t={t} />}
        </div>
        <div className="backup-row-meta mono muted small">
          {serverAddr(s) && <span title={t('адрес ноды')}>{serverAddr(s)}</span>}
          {b.schedule && (
            <span title={t('ежедневно в')+' '+b.schedule}>🕒 {b.schedule}</span>
          )}
          <span title={t('последний бэкап')}>↻ {fmtAgo(b.last_backup_ts)}</span>
          {b.duration_sec ? <span title={t('длительность')}>⏱ {fmtDur(b.duration_sec)}</span> : null}
          <span className={tm.tone} title={t('systemd-таймер бэкапа')}>{tm.icon} {tm.text}</span>
        </div>
        {hint && <div className="hs-host-hit">{hint}</div>}
      </div>
      {/* куда бэкапится — справа, отдельной колонкой: это «адрес назначения», а не
          свойство самой ноды, и глазами удобнее сверять по одной вертикали */}
      {dest && (
        <div className="backup-dest mono muted small" title={t('куда бэкапится')}>
          🗄 <DestLabel dest={dest} />
        </div>
      )}
      <div className="hs-host-count mono">
        <span className={statusTone(st)}>{stLabel[st]}</span>
      </div>
    </button>
  )
}

// Лок держится всё время бэкапа и освежается раз в 5 мин — сам по себе он значит
// «бэкап идёт», а не «сломалось». Проблема — лок, который перестали освежать.
const LOCK_STUCK_SECONDS = 30 * 60
// Висячий лок моложе суток снимают сами чистка и клиент (restic unlock перед работой).
// Дольше суток он уже точно остановил ежедневную чистку: с этого порога шлется алерт
// backup_lock, и тут с него же репозиторий считается проблемным.
const LOCK_LONG_SECONDS = 86400
// сколько секунд висит висячий лок (0 - лока нет или он живой); helper 0.23 отдает
// самый старый висячий лок, так что это время, с которого репозиторий заблокирован
function lockAge(r: RepoStat): number {
  if (!r.locked || !r.lock_ts) return 0
  const age = Date.now() / 1000 - r.lock_ts
  return age > LOCK_STUCK_SECONDS ? age : 0
}
function lockStuck(r: RepoStat): boolean {
  if (!r.locked) return false
  if (!r.lock_ts) return true // helper < v5 времени лока не отдаёт — считаем висячим
  return lockAge(r) > 0
}
function lockLong(r: RepoStat): boolean {
  return !!r.locked && (!r.lock_ts || lockAge(r) > LOCK_LONG_SECONDS)
}
// stuck - висячий лок моложе суток: видно, но проблемой не считается (уйдет сам)
type RepoState = 'archived' | 'muted' | 'invalid' | 'locked' | 'stuck' | 'running' | 'stale' | 'ok'
// Что панель знает о репозиториях сервера сверх них самих: приглушенные, архив и причины, по
// которым репозиторий сейчас в алерте backup_repo. Причины считает бэкенд: с ритмом бэкапов
// (клиент без агента опаздывает через сутки с небольшим, недельный бэкап не устаревает за 3
// дня), так что список, счетчики и алерт расходиться не могут.
// prune - репозитории, у которых не работает чистка (не открывает, падает, не запускается).
// checks - не прошла последняя недельная проверка, моложе двух недель (как у алерта backup_check).
type RepoCtx = {
  muted: Set<string>; archived: Set<string>; reasons: Record<string, string>; prune: Record<string, string>
  checks: Set<string>
}
function repoCtx(s: Server): RepoCtx {
  const now = Date.now() / 1000
  return {
    muted: new Set(s.backup_repo_mutes ?? []),
    archived: new Set(Object.keys(s.backup_repo_archive ?? {}).filter((k) => !k.includes('|'))),
    reasons: s.bsrv_repo_reasons ?? {},
    prune: s.bsrv_prune ?? {},
    checks: new Set(Object.entries(bsrvExtra(s)?.repos ?? {})
      .filter(([, x]) => x.check_ok === 0 && !!x.check_ts && now - x.check_ts < 14 * 86400)
      .map(([n]) => n)),
  }
}
// интервал бэкапов словами: раз в сутки, раз в 6 ч, раз в неделю
function everyLabel(sec: number, t: (s: string, v?: Record<string, string | number>) => string): string {
  const h = sec / 3600
  if (Math.abs(h - 24) <= 2) return t('раз в сутки')
  if (Math.abs(h - 168) <= 12) return t('раз в неделю')
  if (Math.abs(h - 1) <= 0.25) return t('раз в час')
  if (h < 48) return t('раз в {n} ч', { n: Math.round(h) })
  return t('раз в {n} дн.', { n: Math.round(h / 24) })
}
function fmtDay(ts: number): string {
  return ts ? new Date(ts * 1000).toLocaleDateString() : '-'
}
function repoState(r: RepoStat, c: RepoCtx): RepoState {
  if (c.archived.has(r.name)) return 'archived'
  if (c.muted.has(r.name)) return 'muted'
  if (!r.valid) return 'invalid'
  if (lockLong(r)) return 'locked'
  if (r.locked && !lockStuck(r)) return 'running'
  if (c.reasons[r.name]) return 'stale'
  if (lockStuck(r)) return 'stuck'
  return 'ok'
}
// проблемный = не ок, не заглушен и не в архиве (для счетчиков и акцентов)
function repoBad(r: RepoStat, c: RepoCtx): boolean {
  const st = repoState(r, c)
  if (st === 'muted' || st === 'archived') return false
  // неработающая чистка - проблема при любом другом состоянии: нет ни ротации, ни проверки.
  // Не прошедшая проверка - тоже: в окне сервера было "все ок" при красной "проверка не прошла"
  return (st !== 'ok' && st !== 'running' && st !== 'stuck') || !!c.prune[r.name] || c.checks.has(r.name)
}
// Блок helper'а backupserver-setup 0.23: кто чистит репозитории и когда из них что-то
// удалялось. Старше 15 минут - helper встал, по такому блоку не судим.
function bsrvExtra(s: Server): BackupServerExtra | null {
  const b = s.last_report?.extras?.['backup-server']
  if (!b || b.v !== 1 || !b.ts) return null
  const ref = s.last_report?.clock_unix || Date.now() / 1000
  return ref - b.ts > 15 * 60 ? null : b
}
// "30 16 * * *" -> "16:30"; остальное расписание показываем как есть
function cronTime(sched: string): string {
  const m = sched.match(/^(\d{1,2}) (\d{1,2}) \* \* \*$/)
  return m ? `${m[2].padStart(2, '0')}:${m[1].padStart(2, '0')}` : sched
}
function fmtBytes(n?: number): string {
  if (!n) return '—'
  const u = byteUnits()
  let v = n
  let i = 0
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${u[i]}`
}

// подсказка для включения статистики репо (backupserver-setup.sh на бэкап-сервере)
function EnableServerStats() {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const cmd = `curl -fsSL ${window.location.origin}/api/agent/backupserver-setup.sh | sudo bash`
  return (
    <div className="backup-enable">
      <div className="muted small">
        {t('Статистики репозиториев пока нет. Включите её на бэкап-сервере (read-only helper, root-таймер пишет статистику в файл, без паролей):')}
      </div>
      <div className="agent-advice-cmd">
        <pre>{cmd}</pre>
        <button
          className="ghost"
          onClick={() => {
            navigator.clipboard?.writeText(cmd)
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          }}
        >
          {copied ? t('Скопировано') : t('Копировать')}
        </button>
      </div>
    </div>
  )
}

// restic самого сервера бэкапов старше целевой версии. Его обновляет установка helper'а, так
// что обычно это значит, что скачать с github не вышло. Root-команды панель не запускает,
// поэтому показываем команду для сервера.
function ServerResticOld({ ver, target }: { ver: string; target: string }) {
  const { t } = useI18n()
  const cmd = 'sudo /lib65/kervax/kervax-backupserver-helper restic-update'
  return (
    <div className="net-note">
      {t('На сервере restic {v}, нужен {target}, как у клиентов', { v: ver, target })}
      <FixSteps steps={[{ id: 'upd', cmd }]} text={() => t('Обновить restic на сервере')}
        foot={t('Обычно его обновляет установка helper\'а: значит, скачать с GitHub не вышло.')} />
    </div>
  )
}

// Раскладка бэкап-сервера — та же, что создаёт backupserver-setup.sh / deploy-server.
const BSRV_ROOT = '/app/rest-server'

// Битую репу панель удалить НЕ может и не должна (иначе взлом панели = стирание бэкапов).
// Поэтому даём готовую команду для root'а на самом бэкап-сервере: скопировал → вставил.
// Показываем ТОЛЬКО когда терять нечего: репо не инициализирован И снапшотов ноль.
// Удаление репозитория — всегда РУЧНАЯ операция от root на бэкап-сервере: у панели
// намеренно нет примитива удаления бэкапов (взлом панели не должен стирать копии).
// Два случая: пустой мусор (терять нечего) и живой, но заброшенный репозиторий —
// там мы обязаны показать, что именно исчезнет, иначе команда копируется не глядя.
/** Состояние ротации репозитория: когда чистилось, сколько сняло, каков старейший
 * снапшот. Возраст старейшего — главный признак: он переживает свою политику ровно
 * тогда, когда чистка встала, какой бы ни была причина. */
function RotationInfo({ r, x, legacy, prune, t }: {
  r: RepoStat
  x?: BackupServerRepoExtra // кто чистит и когда удаляли (helper 0.23), нет - helper старый
  legacy?: BackupServerLegacy | null
  prune?: string // почему не работает чистка (бэкенд), пусто - работает
  t: (s: string, v?: Record<string, string | number>) => string
}) {
  const oldest = r.oldest_snapshot ?? 0
  const ts = r.rotation_ts ?? 0
  // Метрик нет вовсе — значит на сервере нет и prune-скрипта для этого репозитория.
  // Раньше строка просто исчезала, а политика хранения рядом оставалась: выглядело
  // так, будто старое вычищается по ней. На деле политику панель показывает из
  // старого общего конфига, который никто не исполняет, и чистит репозиторий разве
  // что сам клиент со своей стороны.
  // С helper'ом 0.23 это уже не догадка: он сам говорит, кто чистит репозиторий. Под
  // "сервер не чистит" раньше попадали и те, кого чистил старый общий скрипт из cron.
  const noCleaner = (
    <span
      className="muted"
      title={x
        ? t('Ни своего prune-скрипта, ни блока в старом общем скрипте у репозитория нет: сервер его не чистит. Старое убирает только сам клиент, если у него это настроено.')
        : t('У репозитория нет своего prune-скрипта: политика показана из старого общего конфига, но сервер по ней ничего не удаляет. Старое убирает только сам клиент, если у него это настроено.')}
    >
      {t('сервер не чистит')}
    </span>
  )
  if (!x && !oldest && !ts) return noCleaner
  const limit = rotationLimitDays(r)
  const ageDays = oldest ? Math.floor((Date.now() / 1000 - oldest) / 86400) : 0
  const removed = r.rotation_removed ?? -1
  const now = Date.now() / 1000
  // Снапшотов не больше, чем политика может оставить: их она и оставила. keep-daily считает
  // дни с бэкапами, а не календарные, и у редко бэкапящегося клиента старый снапшот законен.
  // Так же считает rotation_stale_repos на бэкенде.
  const policyMax = (r.keep_last ?? 0) + (r.keep_daily ?? 0) + (r.keep_weekly ?? 0) + (r.keep_monthly ?? 0)
  // чистку выключили руками (helper 0.34): старье - следствие решения, не поломка
  const off = (x?.prune_off ?? 0) === 1
  const stale = !off && !!(oldest && limit && ageDays > limit && r.snapshots > policyMax)
  // чистка что-то удаляет: тогда переживший политику снапшот почти наверняка другой группы
  const alive = removed > 0 || !!(x?.removed_ts && now - x.removed_ts < 3 * 86400)
  // без метрик чистки helper сам видит, когда из репозитория пропадали снапшоты
  const idleDays = x && !x.removed_ts && x.seen_since ? Math.floor((now - x.seen_since) / 86400) : 0
  const lastRun = legacy?.log_ts ? t('последний прогон {ago}', { ago: fmtAgo(legacy.log_ts) }) : ''
  return (
    <>
      {ts <= 0 && x?.cleaner === 'legacy' && legacy && (legacy.where ? (
        <span title={[`${legacy.script}: ${legacy.where}, ${legacy.schedule}`, lastRun].filter(Boolean).join('\n')}>
          {t('чистит старый скрипт')}: {cronTime(legacy.schedule)}
        </span>
      ) : (
        <span className="t-down" title={t('Репозиторий описан в {script}, но этот скрипт не запланирован ни в cron, ни в systemd: старые снапшоты никто не удаляет.', { script: legacy.script })}>
          {t('старый скрипт чистки не запускается')}
        </span>
      ))}
      {ts <= 0 && x?.cleaner === 'script' && (
        <span className="muted" title={t('Свой prune-скрипт есть, но метрик он не пишет: когда он отработал, панель не знает.')}>
          {t('свой prune-скрипт без метрик')}
        </span>
      )}
      {ts <= 0 && x && !x.cleaner && noCleaner}
      {ts <= 0 && x && x.removed_ts > 0 && (
        <span title={t('когда из репозитория последний раз удалялись снапшоты (кто бы их ни удалял)')}>
          {t('удаляли')}: {fmtAgo(x.removed_ts)}
        </span>
      )}
      {ts <= 0 && idleDays >= 1 && (
        <span className="muted" title={t('панель следит за удалениями с {d}', { d: new Date((x?.seen_since ?? 0) * 1000).toLocaleDateString() })}>
          {t('удалений нет {n} дн.', { n: idleDays })}
        </span>
      )}
      {x && x.check_ts && (x.check_ok ?? -1) >= 0 ? (
        <span className={x.check_ok === 0 ? 't-down' : ''}
          title={x.check_ok === 0
            ? t('restic check нашел ошибки, вывод в логе prune-скрипта на бэкап-сервере')
            : (x.check_parts ?? 0) > 0
              ? t('Недельная проверка целостности: структура и индекс, плюс каждую неделю следующая часть данных. За {n} недель прочитывается весь репозиторий.', { n: x.check_parts ?? 0 })
              : t('недельная проверка целостности (restic check)')}>
          {x.check_ok === 0 ? t('проверка не прошла') : t('проверка')}: {fmtAgo(x.check_ts)}
          {(x.check_parts ?? 0) > 0 && ` (${t('данные {p}/{n}', { p: x.check_part ?? 0, n: x.check_parts ?? 0 })})`}
        </span>
      ) : null}
      {off && (
        <span className="muted" title={[x?.prune_off_why, t('Включить: вкладка "Чистка", или на сервере бэкапов kervax-backupserver-helper prune-on {name}', { name: r.name })].filter(Boolean).join('\n')}>
          {t('чистка выключена вручную')}
        </span>
      )}
      {ts > 0 && (
        <span className={prune ? 't-down' : ''} title={!prune && r.rotation_ok === 0 ? t('последний прогон завершился ошибкой') : undefined}>
          {t('чистка')}: {fmtAgo(ts)}
          {removed >= 0 && ` (${t('снято {n}', { n: removed })})`}
        </span>
      )}
      {oldest > 0 && (
        <span
          className={stale ? 't-down' : ''}
          title={!limit
            ? t('политика хранения не задана, судить не о чем')
            : stale && alive
              ? t('Чистка идет, а старые снапшоты остаются: похоже, это другая группа, менялись хост или пути бэкапа. Последние снапшоты каждой группы restic хранит, пока их не удалить вручную.')
              : t('политика допускает снапшоты возрастом до {n} дн.', { n: limit })}
        >
          {t('старейший')}: {ageDays} {t('дн.')}
          {stale && ` > ${limit}`}
        </span>
      )}
    </>
  )
}

/** Сколько дней снапшоты вправе жить по политике репозитория. Повторяет
 * rotation_max_age_days на бэкенде — держать в одном месте нельзя, считают обе
 * стороны: панель рисует, сборщик алертит. */
function rotationLimitDays(r: RepoStat): number {
  const span = (r.keep_daily ?? 0) + (r.keep_weekly ?? 0) * 7 + (r.keep_monthly ?? 0) * 31
  return span > 0 ? span + 10 : 0
}

function RepoCleanup({ r }: { r: RepoStat }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const hasData = r.snapshots > 0
  const cmd = [
    `R=${r.name}`,
    `sudo rm -rf ${BSRV_ROOT}/data/$R`,
    `sudo htpasswd -D ${BSRV_ROOT}/data/.htpasswd $R`,
    `sudo rm -f ${BSRV_ROOT}/system/scripts/restic-prune-$R.sh ${BSRV_ROOT}/system/envs/$R.env`,
    `sudo rm -f /etc/cron.d/kervax-prune-$R`,
    `sudo /lib65/kervax/kervax-backupserver-helper refresh`,
  ].join('\n')
  return (
    <div className="repo-cleanup">
      <button className="ghost repo-cleanup-toggle" onClick={() => setOpen(!open)}>
        {open ? t('скрыть') : t('🧹 как удалить')}
      </button>
      {open && (
        <>
          {hasData ? (
            <div className="form-error small">
              ⚠️ {t('Будет стерто безвозвратно: {n} снапшотов, {sz}, последний {ago}. Других копий у панели нет.', {
                n: r.snapshots, sz: fmtBytes(r.size_bytes), ago: fmtAgo(r.last_activity),
              })}
              <div className="muted small">
                {t('Если бэкап еще может понадобиться, приглушите репозиторий (🔕): данные останутся.')}
              </div>
            </div>
          ) : (
            <div className="muted small">
              {t('Репозиторий пустой и не инициализирован, удалять нечего, кроме мусора.')}
            </div>
          )}
          <FixSteps steps={[{ id: 'rm', cmd }]} text={() => t('Удалить на бэкап-сервере от root')}
            foot={t('Панель бэкапы не удаляет, это защита от взлома. Если репозиторий заводил Ansible, проверьте еще строку в sudo crontab -l.')} />
        </>
      )}
    </div>
  )
}

// Лок в репозитории. Снимать его панель НЕ будет: команда пишет в репо, а панель по
// договорённости не имеет ни одной операции записи/удаления над бэкапами. Показываем
// готовую команду для root'а на бэкап-сервере.
function RepoUnlock({ r, onChanged }: { r: RepoStat; onChanged: () => void }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [checking, setChecking] = useState(false)
  // пароль берём из prune-env ВНУТРИ sudo bash -c: так он не светится в списке процессов
  const cmd = [
    // Тело в ОДИНАРНЫХ кавычках: имя репо подставляет панель, а внешний шелл ничего
    // не разворачивает. Логика через if, а не `A && B || C`: раньше при неверном пароле
    // срабатывала ветка «restic не установлен» и человек получал второе, ложное сообщение.
    `sudo bash -c '`,
    `R=${r.name}`,
    `REPO=${BSRV_ROOT}/data/$R; ENV=${BSRV_ROOT}/system/envs/$R.env`,
    `RESTIC=$(command -v restic || echo /lib65/kervax/restic)`,
    `if [ -x "$RESTIC" ] && [ -f "$ENV" ] && (set -a; . "$ENV"; set +a; "$RESTIC" cat config >/dev/null 2>&1); then`,
    `  (set -a; . "$ENV"; set +a; "$RESTIC" unlock) && echo "${tr('лок снят штатно')}"`,
    `else`,
    // Репозитории из старых деплоев созданы с другим паролем, и env к ним не подходит —
    // штатный unlock невозможен в принципе. Лок restic — это обычный файл в репо: удалить
    // его руками равносильно unlock и пароля не требует. Данные при этом не трогаются.
    // Удаляем только локи старше 30 минут, как и restic unlock: живой лок освежается раз
    // в 5 минут, и идущий бэкап или чистка не пострадают. Раньше удалялись все лок-файлы,
    // и перед командой приходилось проверять, что по репозиторию ничего не идет.
    // У репозиториев старого общего скрипта env нет вовсе, поэтому ветка частая.
    `  echo "${tr('подходящего пароля в env нет - снимаю висячие локи файлами')}"`,
    `  find "$REPO"/locks -maxdepth 1 -type f -mmin +30 -print -delete`,
    `  echo "${tr('готово, живые локи (моложе 30 минут) не тронуты')}"`,
    `fi`,
    `'`,
  ].join('\n')
  // имя репо уходит в rm/пути: то же правило, что в массовой зачистке. Панель получает
  // имя от агента, но команду с подозрительным именем показывать нельзя — её скопируют.
  if (!safeRepoName(r.name)) {
    return (
      <div className="repo-cleanup form-error small">
        {t('Имя репозитория выглядит небезопасно — команда не показана, разберитесь на сервере вручную.')}
      </div>
    )
  }
  return (
    <div className="repo-cleanup">
      <button className="ghost repo-cleanup-toggle" onClick={() => setOpen(!open)}>
        {open ? t('скрыть') : t('🔓 как снять лок')}
      </button>
      {open && (
        <>
          <FixSteps steps={[{ id: 'unlock', cmd }]} text={() => t('Снять висячие локи на бэкап-сервере от root')}
            foot={t('Живой лок идущего бэкапа или чистки (моложе 30 минут) команда не тронет. Пароль читается из env внутри команды и в списке процессов не виден.')} />
          {/* Статус снимается не мгновенно: helper пересчитывает статистику раз в минуту,
              потом отчёт агента и опрос UI. Без этой подсказки снятый лок выглядел как
              «панель не заметила», и человек шёл проверять второй раз. */}
          <div className="repo-unlock-after">
            <button className="ghost" disabled={checking} onClick={() => {
              setChecking(true)
              onChanged()
              window.setTimeout(() => setChecking(false), 2000)
            }}>{checking ? t('Проверяю…') : t('Снял, проверить')}</button>
            <span className="muted small">
              {t('Статус обновится сам в течение минуты.')}
            </span>
          </div>
        </>
      )}
    </div>
  )
}

function RepoRow({ server: s, r, ctx, canAct, onChanged, growth, unmonitored }: {
  server: Server; r: RepoStat; ctx: RepoCtx; canAct: boolean; onChanged: () => void
  growth?: number // обычный прирост клиента за прогон (после сжатия)
  unmonitored?: boolean // клиента нет в панели
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [archiving, setArchiving] = useState(false)
  const st = repoState(r, ctx)
  const isMuted = st === 'muted'
  // именно repoBad, а не своя копия условия: иначе строка и счётчики проблем расходятся
  // (так «идёт бэкап» — нормальное состояние — какое-то время красилось красным)
  const tone = isMuted ? 't-muted' : repoBad(r, ctx) ? 't-down' : 't-up'
  const now = Date.now() / 1000
  const ageSec = r.last_activity ? now - r.last_activity : 0
  // Обычный интервал бэкапов (бэкенд считает его по последним снапшотам, helper 0.28): с ним
  // видно, когда ждать следующий бэкап. Без него как раньше: дольше 2 дней - желтым.
  const cad = s.bsrv_cadence?.[r.name] ?? 0
  const freshTone = !r.last_activity ? 'muted' : st === 'stale' ? 't-down'
    : (cad ? ageSec > cad + 1800 : ageSec > 2 * 86400) ? 't-degraded' : 't-up'
  const keep = [r.keep_last, r.keep_daily, r.keep_weekly, r.keep_monthly]
  const hasKeep = keep.some((v) => (v ?? 0) > 0)
  const toggleMute = async () => {
    setBusy(true)
    try {
      await backupRepoMute(s.id, r.name, !isMuted)
      onChanged()
    } finally {
      setBusy(false)
    }
  }
  // «нечего терять»: репо не инициализирован и снапшотов нет → предлагаем зачистку.
  // Если снапшоты ЕСТЬ, а config побит — команду НЕ даём: данные могут быть восстановимы,
  // такое разбирают руками, а не копипастой rm -rf.
  const junk = st === 'invalid' && r.snapshots === 0
  const brokenWithData = st === 'invalid' && r.snapshots > 0
  const ext = bsrvExtra(s)
  const lockDays = Math.floor(lockAge(r) / 86400)
  const lockHours = Math.max(1, Math.floor(lockAge(r) / 3600))
  const staleTitle = cad
    ? t('Бэкап опоздал: обычно {every}, последний {ago}.', { every: everyLabel(cad, t), ago: fmtAgo(r.last_activity) })
    : t('Нет новых бэкапов {n} дн.', { n: Math.floor(ageSec / 86400) })
  const nextAt = cad > 0 && cad < 2 * 86400 && r.last_activity && st !== 'stale'
    ? new Date((r.last_activity + cad) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : ''
  return (
    <div className={`loc-res docker-row repo-row repo-row-col ${tone}`}>
      <div className="repo-row-main">
      <span className={`lk-dot${DOT[tone] ?? ''}`} />
      <div className="docker-c-main">
        <div className="docker-c-name mono">
          {r.name}
          {isMuted && <span className="type-chip">{t('приглушён')}</span>}
          {st === 'invalid' && <span className="type-chip off">{t('невалиден')}</span>}
          {st === 'locked' && (
            <span className="type-chip off" title={t('Висячий лок держится больше суток: чистка и restic check по репозиторию не идут, пока его не снять.')}>
              {lockDays > 0 ? t('залочен {n} дн.', { n: lockDays }) : t('залочен')}
            </span>
          )}
          {st === 'stuck' && (
            <span className="type-chip" title={t('Лок не обновлялся больше 30 минут: процесс, который его взял, умер. Чистка и клиенты обычно снимают такие локи сами, если лок продержится сутки, придет алерт.')}>
              {t('висячий лок {n} ч', { n: lockHours })}
            </span>
          )}
          {st === 'running' && <span className="type-chip">{t('идёт бэкап')}</span>}
          {unmonitored && (
            <span className="type-chip" title={t('Клиента этого репозитория нет в панели. Панель следит за ним по ритму бэкапов: опоздавший бэкап видно через интервал и еще четверть его, ежедневный через 30 часов. Агент на самой ноде сказал бы сразу, как бэкап упал.')}>
              {t('нет в панели')}
            </span>
          )}
          {st === 'stale' && <span className="type-chip off" title={staleTitle}>{cad ? t('опоздал') : t('устарел')}</span>}
          {ctx.prune[r.name] && !isMuted && (
            <span className="type-chip off">{t('чистка не работает')}</span>
          )}
          {(growth ?? 0) > 0 && (
            <span className="type-chip" title={t('обычный прирост за прогон после сжатия')}>
              +{fmtBytes(growth ?? 0)}
            </span>
          )}
        </div>
        <div className="docker-c-img mono muted small repo-meta">
          <span>{t('снапшотов: {n}', { n: r.snapshots })}</span>
          <span>{fmtBytes(r.size_bytes)}</span>
          <span className={freshTone === 'muted' ? '' : freshTone}>{t('обновлён')}: {fmtAgo(r.last_activity)}</span>
          {cad > 0 && (
            <span title={t('по времени последних снапшотов')}>
              {everyLabel(cad, t)}{nextAt && `, ${t('следующий к {time}', { time: nextAt })}`}
            </span>
          )}
          {hasKeep && (
            <span title={t('политика хранения: последних / дневных / недельных / месячных')}>
              {t('хранит')}: {keep.map((v) => v ?? 0).join('/')}
            </span>
          )}
          <RotationInfo r={r} x={ext?.repos?.[r.name]} legacy={ext?.legacy} prune={ctx.prune[r.name]} t={t} />
        </div>
      </div>
      {/* В архив - то, что хранится как есть: сервера больше нет, бэкапить больше не нужно,
          разовый бэкап, проект заморожен. Предлагаем там, где бэкапы и так не приходят. */}
      {canAct && (st === 'stale' || isMuted) && (
        <button className="ghost icon-btn repo-mute-btn" disabled={busy} onClick={() => setArchiving(!archiving)}
          title={t('В архив: хранить как есть, без алертов (сервера больше нет, разовый бэкап, проект заморожен)')}>
          🗄
        </button>
      )}
      {/* Приглушать нечего, пока репозиторий здоров: кнопка на каждой зелёной строке —
          это десятки бесполезных элементов, среди которых теряются нужные. Показываем
          только там, где есть что глушить, и у уже приглушённых (чтобы вернуть). */}
      {canAct && st !== 'ok' && st !== 'running' && st !== 'stuck' && (
        <button className="ghost icon-btn repo-mute-btn" disabled={busy} onClick={toggleMute}
          title={isMuted ? t('Снять приглушение') : t('Приглушить (разовый/неактуальный)')}>
          {busy ? '…' : isMuted ? '🔔' : '🔕'}
        </button>
      )}
      </div>
      {ctx.prune[r.name] && !isMuted && <div className="small t-down repo-prune-why">{ctx.prune[r.name]}</div>}
      {archiving && (
        <ArchiveForm server={s} repo={r.name} host="" onCancel={() => setArchiving(false)}
          onDone={() => { setArchiving(false); onChanged() }} />
      )}
      <RepoGroups server={s} repo={r.name} ext={ext} canAct={canAct} onChanged={onChanged} />
      {/* «как удалить» даём только у проблемных: пустой мусор и заброшенные репо — это те,
          что реально выводят из эксплуатации. У живого репозитория с ежедневным бэкапом
          такая кнопка была бы приглашением к беде. */}
      {canAct && (junk || st === 'stale') && <RepoCleanup r={r} />}
      {canAct && (st === 'locked' || st === 'stuck') && <RepoUnlock r={r} onChanged={onChanged} />}
      {canAct && brokenWithData && (
        <div className="muted small repo-cleanup">
          ⚠️ {t('Нет config, но снапшоты есть ({n} шт., {sz}) — данные могут быть восстановимы. Вслепую не удаляйте, разберитесь на сервере.', { n: r.snapshots, sz: fmtBytes(r.size_bytes) })}
        </div>
      )}
    </div>
  )
}

// Почему убираем в архив: одним нажатием, свое можно дописать.
const ARCHIVE_NOTES = ['сервера больше нет', 'больше не бэкапится', 'разовый бэкап', 'проект заморожен']

// Убрать в архив репозиторий (host пустой) или одну старую группу его снапшотов. На сервере
// ничего не меняется: чистка по политике у репозитория без новых снапшотов и так ничего не
// удаляет, а группу удаляют только руками. Архив просто перестает быть проблемой.
function ArchiveForm({ server: s, repo, host, onCancel, onDone }: {
  server: Server; repo: string; host: string; onCancel: () => void; onDone: () => void
}) {
  const { t } = useI18n()
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const save = async () => {
    setBusy(true)
    setErr('')
    try {
      await backupRepoArchive(s.id, repo, host, note.trim(), true)
      onDone()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
      setBusy(false)
    }
  }
  return (
    <div className="repo-cleanup repo-archive-form">
      <div className="muted small">
        {host
          ? t('Группа {host} останется в репозитории как есть и больше не будет считаться встающей ротацией.', { host })
          : t('Репозиторий останется как есть: чистка по политике у него ничего не удаляет, раз новых снапшотов нет. Он уйдет в раздел "Архив" и перестанет быть проблемой, а проверка целостности продолжится.')}
      </div>
      <div className="repo-archive-notes">
        {ARCHIVE_NOTES.map((n) => (
          <button key={n} type="button" className={`type-chip ${note === t(n) ? 'on' : ''}`} onClick={() => setNote(t(n))}>
            {t(n)}
          </button>
        ))}
      </div>
      <div className="repo-archive-row">
        <input className="field-inp" maxLength={200} placeholder={t('Зачем в архив (необязательно)')}
          value={note} onChange={(e) => setNote(e.target.value)} />
        <button disabled={busy} onClick={save}>{busy ? '...' : t('В архив')}</button>
        <button className="ghost" disabled={busy} onClick={onCancel}>{t('Отмена')}</button>
      </div>
      {err && <div className="form-error small">{err}</div>}
    </div>
  )
}

// Склейку переименованного сервера с его прежним именем (merge-group) helper умеет с 0.33
const MERGE_HELPER = '0.33'

function verAtLeast(a: string | undefined | null, b: string): boolean {
  if (!a) return false
  const pa = a.split('.').map((x) => parseInt(x, 10) || 0)
  const pb = b.split('.').map((x) => parseInt(x, 10) || 0)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] ?? 0) !== (pb[i] ?? 0)) return (pa[i] ?? 0) > (pb[i] ?? 0)
  }
  return true
}

// Под каким именем сервер бэкапится теперь, если старая группа - его прежнее имя (как renamed_to
// на бэкенде): самая свежая группа началась не позже недели после конца старой, пути и теги те же
function renamedTo(groups: BackupGroup[], g: BackupGroup): string {
  const cur = groups.reduce((a, b) => (b.last > a.last ? b : a))
  if (!cur.host || cur.host === g.host) return ''
  const same = (a: string[], b: string[]) => [...a].sort().join('\n') === [...b].sort().join('\n')
  return same(cur.paths, g.paths) && same(cur.tags, g.tags) && Math.abs(cur.first - g.last) <= 7 * 86400 ? cur.host : ''
}

// Группы снапшотов репозитория (хост и теги, как их видит forget) от helper'а 0.28. Старая
// группа - хост, который больше не бэкапится сюда: переименовали, сервера нет, разовый бэкап.
// Политика держит её последние снапшоты вечно. Переименованный сервер можно склеить с прежним
// именем (дальше историю чистит общая политика), старую группу - убрать в архив (хранить как
// есть) или удалить. Склейка и удаление - командой на сервере, руками и только старой группы.
function RepoGroups({ server: s, repo, ext, canAct, onChanged }: {
  server: Server; repo: string; ext: BackupServerExtra | null; canAct: boolean; onChanged: () => void
}) {
  const { t } = useI18n()
  const [archiving, setArchiving] = useState('')
  // какая команда раскрыта: "merge:<хост>" или "forget:<хост>"
  const [showCmd, setShowCmd] = useState('')
  const [copied, setCopied] = useState('')
  const [busy, setBusy] = useState(false)
  const now = Date.now() / 1000
  const g = ext?.repos?.[repo]?.groups
  const groups: BackupGroup[] = g && now - g.ts < 3 * 86400 ? g.groups ?? [] : []
  if (groups.length < 2) return null
  const arch = s.backup_repo_archive ?? {}
  const newest = Math.max(...groups.map((x) => x.last))
  const old = groups.filter((x) => x.last < newest && now - x.last > 7 * 86400)
  if (old.length === 0) return null
  const unarchive = async (host: string) => {
    setBusy(true)
    try {
      await backupRepoArchive(s.id, repo, host, '', false)
      onChanged()
    } finally {
      setBusy(false)
    }
  }
  const copy = (key: string, v: string) => {
    navigator.clipboard?.writeText(v)
    setCopied(key)
    window.setTimeout(() => setCopied(''), 1500)
  }
  const toggleCmd = (key: string) => setShowCmd(showCmd === key ? '' : key)
  const helperVer = s.last_report?.setup_versions?.['backupserver-setup']
  const canMerge = verAtLeast(helperVer == null ? '' : String(helperVer), MERGE_HELPER)
  const live = old.filter((x) => !arch[`${repo}|${x.host}`])
  return (
    <div className="repo-groups small">
      {live.length > 0 && (
        <div className="muted">
          {t('Чистка restic идет по группам (хост и теги) и последние снапшоты каждой группы держит всегда: группа, в которую больше не бэкапятся, сама не уйдет.')}
        </div>
      )}
      {old.map((x) => {
        const entry: BackupArchiveEntry | undefined = arch[`${repo}|${x.host}`]
        const to = renamedTo(groups, x)
        // шаги под эту группу собирает бэкенд (app/bsrv_cleanup.py): пробный прогон, применение,
        // проверка; без них - одна команда, как раньше
        const fx = s.bsrv_cleanup?.groups?.[repo]?.[x.host]
        const helper = 'sudo /lib65/kervax/kervax-backupserver-helper'
        const cmd = `${helper} forget-group ${repo} ${x.host}`
        const mergeCmd = `${helper} merge-group ${repo} ${x.host} ${to}`
        return (
          <div key={x.host + x.tags.join(',')} className="repo-group">
            <div className="repo-group-line">
              <span className={entry ? 'muted' : 't-degraded'}>
                {entry ? t('группа {host} в архиве', { host: x.host })
                  : to ? t('прежнее имя сервера {host}', { host: x.host }) : t('старая группа {host}', { host: x.host })}
                {`: ${t('снапшотов: {n}', { n: x.n })}, ${fmtDay(x.first)} - ${fmtDay(x.last)}`}
                {entry?.note ? ` (${entry.note})` : ''}
              </span>
              {canAct && !entry && (
                <>
                  {to && (
                    <button className="ghost small-btn" onClick={() => toggleCmd(`merge:${x.host}`)}
                      title={t('Отдать эти снапшоты новому имени: дальше их чистит общая политика')}>
                      {t('склеить с {host}', { host: to })}
                    </button>
                  )}
                  <button className="ghost small-btn" onClick={() => setArchiving(archiving === x.host ? '' : x.host)}
                    title={t('Хранить группу как есть и не считать её встающей ротацией')}>
                    🗄 {t('в архив')}
                  </button>
                  <button className="ghost small-btn" onClick={() => toggleCmd(`forget:${x.host}`)}>
                    {t('как удалить')}
                  </button>
                </>
              )}
              {canAct && entry && (
                <button className="ghost small-btn" disabled={busy} onClick={() => unarchive(x.host)}>{t('вернуть')}</button>
              )}
            </div>
            {!entry && x.paths.length > 0 && <div className="muted">{t('пути')}: <span className="mono">{x.paths.join(' ')}</span></div>}
            {archiving === x.host && (
              <ArchiveForm server={s} repo={repo} host={x.host} onCancel={() => setArchiving('')}
                onDone={() => { setArchiving(''); onChanged() }} />
            )}
            {showCmd === `merge:${x.host}` && fx?.merge && (
              <FixSteps steps={fx.merge} text={(st) => bsrvFixText(st, t)} />
            )}
            {showCmd === `merge:${x.host}` && !fx?.merge && (
              <div className="repo-cleanup">
                {canMerge ? (
                  <>
                    <div className="muted">
                      {t('На бэкап-сервере. Снапшоты прежнего имени получат имя {to}, и дальше их чистит общая политика: лишние уйдут ночной чисткой, остальные останутся, пока нужны политике. Данные не копируются. Без --apply команда только покажет, что сделает.', { to })}
                    </div>
                    <div className="agent-advice-cmd">
                      <pre>{mergeCmd}</pre>
                      <button className="ghost" onClick={() => copy(`m:${x.host}`, mergeCmd)}>{copied === `m:${x.host}` ? t('Скопировано') : t('Копировать')}</button>
                    </div>
                  </>
                ) : (
                  <div className="muted">
                    {t('Склейку helper backupserver-setup умеет с версии {v}, здесь {cur}: обновите helper на этом сервере.', { v: MERGE_HELPER, cur: helperVer == null ? '?' : String(helperVer) })}
                  </div>
                )}
              </div>
            )}
            {showCmd === `forget:${x.host}` && fx?.forget && (
              <FixSteps steps={fx.forget} text={(st) => bsrvFixText(st, t)} />
            )}
            {showCmd === `forget:${x.host}` && !fx?.forget && (
              <div className="repo-cleanup">
                <div className="muted">
                  {t('На бэкап-сервере. Без --apply команда только покажет, что удалится, с --apply удалит снапшоты группы, место вернет ночная чистка. Группу, в которую бэкапились последнюю неделю, команда удалять откажется.')}
                </div>
                <div className="agent-advice-cmd">
                  <pre>{cmd}</pre>
                  <button className="ghost" onClick={() => copy(x.host, cmd)}>{copied === x.host ? t('Скопировано') : t('Копировать')}</button>
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// Строка репозитория в архиве: спокойная, без красного. Видно, зачем он там, с какого дня,
// когда был последний бэкап и цел ли он (проверка целостности идет и для архива).
function ArchivedRow({ server: s, r, entry, canAct, onChanged }: {
  server: Server; r: RepoStat; entry: BackupArchiveEntry; canAct: boolean; onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const ext = bsrvExtra(s)
  const x = ext?.repos?.[r.name]
  const restore = async () => {
    setBusy(true)
    try {
      await backupRepoArchive(s.id, r.name, '', '', false)
      onChanged()
    } finally {
      setBusy(false)
    }
  }
  const since = entry.ts ? new Date(entry.ts).toLocaleDateString() : ''
  return (
    <div className="loc-res docker-row repo-row repo-row-col repo-archived">
      <div className="repo-row-main">
        <span className="lk-dot" />
        <div className="docker-c-main">
          <div className="docker-c-name mono">
            {r.name}
            <span className="type-chip">🗄 {t('архив')}</span>
            {entry.note && <span className="muted small">{entry.note}</span>}
          </div>
          <div className="docker-c-img mono muted small repo-meta">
            <span>{t('снапшотов: {n}', { n: r.snapshots })}</span>
            <span>{fmtBytes(r.size_bytes)}</span>
            <span>{t('последний бэкап')}: {fmtDay(r.last_activity ?? 0)}</span>
            {since && <span title={entry.by ? t('убрал {who}', { who: entry.by }) : ''}>{t('в архиве с {d}', { d: since })}</span>}
            {x && x.check_ts && (x.check_ok ?? -1) >= 0 ? (
              <span className={x.check_ok === 0 ? 't-down' : ''}>
                {x.check_ok === 0 ? t('проверка не прошла') : t('проверка')}: {fmtAgo(x.check_ts)}
              </span>
            ) : null}
            {r.rotation_ok === 0 && (r.rotation_removed ?? -1) < 0 && (
              <span className="t-degraded" title={x?.prune_err || ''}>
                {t('чистка и проверка не открывают репозиторий')}
              </span>
            )}
          </div>
        </div>
        {canAct && (
          <button className="ghost small-btn" disabled={busy} onClick={restore} title={t('Вернуть из архива: снова следить за свежестью')}>
            {busy ? '...' : t('вернуть')}
          </button>
        )}
      </div>
    </div>
  )
}

// Обновление образа rest-server до RESTSRV_TARGET. Данные (репозитории) лежат в bind-mount
// /data, не в образе — обновление их не трогает. Кнопку показываем только если версия ниже
// целевой (стухший «latest» на старых серверах). Образ зашит в helper — панель его не выбирает.
function RestServerUpdate({ server: s, info, onChanged }: {
  server: Server; info: BackupServerInfo; onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const ver = info.version || ''
  const old = !!ver && verLt(ver, RESTSRV_TARGET)
  if (!old || done) {
    return done ? (
      <span className="t-up small">✓ {t('обновлено до {v} — версия в шапке обновится через минуту', { v: RESTSRV_TARGET })}</span>
    ) : null
  }
  const update = async () => {
    if (!window.confirm(t('Обновить образ rest-server до {v}? Перекачает образ и перезапустит контейнер (~30с недоступности). Репозитории лежат вне образа — не пострадают.', { v: RESTSRV_TARGET }))) return
    setBusy(true); setMsg(null)
    try {
      const res = await runAndWait(s.id, { action: 'update_image' })
      const ok = res.status !== 'error'
      setMsg(ok ? (helperText(res.result, t) || t('готово')) : (helperText(res.result, t) || t('не удалось')))
      if (ok) setDone(true)
      onChanged()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : t('ошибка'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <button className="restic-update-btn" disabled={busy} onClick={update}>
        {busy ? '…' : `⬆ ${t('обновить до {v}', { v: RESTSRV_TARGET })}`}
      </button>
      {msg && <span className="muted small"> · {msg}</span>}
    </>
  )
}

// Миграция существующего HTTP rest-server на HTTPS: поднимает self-signed TLS-фронт (:64101)
// рядом, HTTP :64100 не трогает (старые клиенты живут). Кнопка в шапке модалки, если tls нет.
function EnableTls({ server: s, onChanged }: { server: Server; onChanged: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  if (done) {
    return <span className="t-up small">✓ {t('HTTPS включён — бейдж обновится через минуту')}</span>
  }
  const enable = async () => {
    if (!window.confirm(t('Поднять self-signed HTTPS-фронт на :64101? HTTP :64100 продолжит работать — старые клиенты не сломаются. Новых/мигрируемых клиентов подключайте с TLS (галка при энролле).'))) return
    setBusy(true); setMsg(null)
    try {
      const j = await enableBackupTls(s.id)
      let cur = j
      while (cur.status === 'running' && alive.current) {
        await new Promise((r) => setTimeout(r, 2500))
        if (!alive.current) return
        cur = await backupSetupStatus(s.id, j.id)
      }
      const ok = cur.status === 'done'
      setMsg(ok ? (cur.message || t('готово')) : (cur.message || t('не удалось')))
      if (ok) { setDone(true); onChanged() }
    } catch (e) {
      setMsg(e instanceof Error ? e.message : t('ошибка'))
    } finally {
      if (alive.current) setBusy(false)
    }
  }
  return (
    <>
      <button className="restic-update-btn" disabled={busy} onClick={enable}>
        {busy ? '…' : `🔒 ${t('включить HTTPS')}`}
      </button>
      {msg && <span className="muted small"> · {msg}</span>}
    </>
  )
}

// Окно сервера бэкапов: меню разделов слева, на экране один раздел, как у карточек (cardLayout).
// Раньше шли плитки (в двух еще и кнопки обновления rest-server и включения HTTPS), потом длинная
// карточка репозиториев с архивом и массовой чисткой внутри, потом покрытие
const BSRV_SECTIONS: SecDef[] = [
  { id: 'overview', label: 'Сводка', group: '' },
  { id: 'repos', label: 'Репозитории', group: 'Репозитории' },
  { id: 'archive', label: 'Архив', group: 'Репозитории' },
  { id: 'cleanup', label: 'Чистка', group: 'Репозитории' },
  { id: 'server', label: 'Сервер', group: 'Нода' },
  { id: 'https', label: 'Переход на https', group: 'Нода' },
  { id: 'coverage', label: 'Покрытие', group: 'Нода' },
]
const BSRV_SEC_IDS = new Set(BSRV_SECTIONS.map((x) => x.id))

function BackupServerModal({ server: s, info, servers, canAct, initialSec, onChanged, onClose }: {
  server: Server; info: BackupServerInfo; servers: Server[]; canAct: boolean
  initialSec?: string | null // из ссылки алерта или с главной: ?backupsrv=id&sec=cleanup
  onChanged: () => void; onClose: () => void
}) {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<'problems' | 'name' | 'size' | 'age' | 'snapshots' | 'growth'>('problems')
  const [active, setActive] = useState<string>(() => (BSRV_SEC_IDS.has(initialSec ?? '') ? initialSec! : 'overview'))
  const layoutRef = useRef<HTMLDivElement>(null)
  const go = (id: string) => {
    setActive(id)
    setQ('')
    const el = layoutRef.current
    if (el && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: 'start' })
  }
  // клиенты без агента в панели: сломанный бэкап панель видит только по ритму бэкапов
  const unmon = new Set(s.bsrv_unmonitored ?? [])
  const [onlyUnmon, setOnlyUnmon] = useState(false)
  // Кто раздувает сервер: обычный прирост за прогон у клиента, чей репозиторий здесь. Репозиторий
  // назван по клиенту (так его заводит helper), поэтому сопоставляем по имени.
  const growthOf = new Map<string, number>()
  for (const c of servers) {
    const runs = backupRuns(c)
    if (!runs.length) continue
    const g = usualGrowth(runs)
    for (const n of [c.hostname, c.name]) if (n) growthOf.set(n, g)
  }
  const ctx = repoCtx(s)
  const muted = ctx.muted
  const every = info.repos ?? []
  // архив отдельным разделом: в общем списке он выглядел бы заброшкой среди живых
  const all = every.filter((r) => !ctx.archived.has(r.name))
  const archivedRepos = every.filter((r) => ctx.archived.has(r.name)).sort((a, b) => a.name.localeCompare(b.name))
  const archivedBytes = archivedRepos.reduce((a, r) => a + (r.size_bytes || 0), 0)
  const bad = all.filter((r) => repoBad(r, ctx)).length
  const ql = q.trim().toLowerCase()
  const rank = (r: RepoStat) => (repoBad(r, ctx) ? 0 : muted.has(r.name) ? 2 : 1)
  const rcmp: Record<string, (a: RepoStat, b: RepoStat) => number> = {
    problems: (a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name),
    name: (a, b) => a.name.localeCompare(b.name),
    size: (a, b) => (b.size_bytes ?? 0) - (a.size_bytes ?? 0) || a.name.localeCompare(b.name),
    age: (a, b) => (a.last_activity || Infinity) - (b.last_activity || Infinity) || a.name.localeCompare(b.name),
    snapshots: (a, b) => b.snapshots - a.snapshots || a.name.localeCompare(b.name),
    growth: (a, b) => (growthOf.get(b.name) ?? -1) - (growthOf.get(a.name) ?? -1) || a.name.localeCompare(b.name),
  }
  const growers = all
    .map((r) => ({ name: r.name, g: growthOf.get(r.name) ?? 0 }))
    .filter((x) => x.g > 0)
    .sort((a, b) => b.g - a.g)
  const growTotal = growers.reduce((a, x) => a + x.g, 0)
  const ext = bsrvExtra(s)
  // вкладка "Чистка": пункты с причинами и шагами считает бэкенд (app/bsrv_cleanup.py)
  const cl = s.bsrv_cleanup ?? null
  // репозитории, где старье не уходит, кроме тех, что уже в плашке старого скрипта: там та же беда
  // (скрипт не запущен) с теми же шагами, второй строкой они только путали
  const rotRows = (cl?.rotation ?? []).filter((x) =>
    !(x.cause === 'legacy_off' && cl?.legacy?.repos.some((r) => r.name === x.repo)))
  const repos = all
    .filter((r) => !ql || r.name.toLowerCase().includes(ql))
    .filter((r) => !onlyUnmon || unmon.has(r.name))
    .slice()
    .sort(rcmp[sort])
  const pct = diskPct(info)
  const free = info.disk_free ?? Math.max((info.disk_total || 0) - (info.disk_used || 0), 0)
  const srvOld = !!info.version && verLt(info.version, RESTSRV_TARGET)
  const tlsPort = info.tls_port || 64101
  const now = Date.now() / 1000
  // недельная проверка целостности за две недели: сколько прошло из тех, что вообще проверялись.
  // check_ok -1 - проверки еще не было: helper заранее ставит дату первой, и такой репозиторий
  // выглядел бы проваленным ("1 из 4")
  const checked = Object.entries(ext?.repos ?? {}).filter(([n, x]) =>
    !ctx.archived.has(n) && (x.check_ok ?? -1) >= 0 && !!x.check_ts && now - x.check_ts < 14 * 86400)
  const checkedOk = checked.filter(([, x]) => x.check_ok === 1).length
  const smuted = new Set(s.alert_mutes ?? [])
  const audit = (s.backup_audit ?? []).filter((x) => !x.muted)
  const pruneNames = Object.keys(ctx.prune).filter((n) => !muted.has(n) && !ctx.archived.has(n)).sort()
  const rot = s.bsrv_rotation ?? []
  // клиенты, чей бэкап идет сюда по http (бэкенд: backup_https.bs_id - этот сервер)
  const httpClients = servers.filter((c) => c.backup_https?.bs_id === s.id).sort((a, b) => a.name.localeCompare(b.name))
  const httpOpen = httpClients.filter((c) => !c.backup_target?.muted)

  const shown: Record<string, boolean> = {
    overview: true,
    repos: true,
    archive: archivedRepos.length > 0,
    cleanup: all.length > 0,
    server: true,
    // открытый раздел не прячем, когда последний клиент перешел на https: на нем итоги перевода
    https: httpClients.length > 0 || active === 'https',
    coverage: (s.backup_audit?.length ?? 0) > 0,
  }
  const secs = BSRV_SECTIONS.filter((x) => shown[x.id])
  const cur = secs.find((x) => x.id === active) ?? secs[0]
  useUrlParam('sec', cur.id === 'overview' ? null : cur.id)

  // Замечания по разделам - те же, что в "Что сломано" на главной: rest-server остановлен,
  // висячие локи, опоздавшие и битые репозитории, неработающая чистка, невычищаемые снапшоты,
  // проверка целостности с бэкенда. Старый restic, пустая статистика и отдельный дамп для базы -
  // советы из "Требует действий"
  const items: Record<string, SecItem[]> = {}
  const add = (sec: string, x: SecItem) => (items[sec] ??= []).push(x)
  if (s.online) {
    if (!info.running) add('server', { level: 2, text: t('rest-server остановлен') })
    const locked = all.filter((r) => !muted.has(r.name) && r.locked && r.lock_ts && now - r.lock_ts > 86400)
    if (locked.length && !smuted.has('backup_lock'))
      add('repos', {
        level: 2,
        count: locked.length,
        since: new Date(Math.min(...locked.map((r) => r.lock_ts ?? now)) * 1000).toISOString(),
        text: t('залочен больше суток, чистка не идет: {list}', {
          list: locked.map((r) => `${r.name} (${Math.floor((now - (r.lock_ts ?? now)) / 86400)} ${t('дн.')})`).join(', '),
        }),
      })
    const badNames = Object.keys(s.bsrv_repo_reasons ?? {}).sort()
    if (badNames.length)
      add('repos', {
        level: 2,
        count: badNames.length,
        since: s.alert_since?.backup_repo,
        text: t('репозиториев с проблемой: {n} ({list})', { n: badNames.length, list: shortList(badNames, t, 3) }),
      })
    for (const i of srvIssues(s, t).filter((x) => x.section === 'backups' && x.srv))
      add(BSRV_SEC_IDS.has(i.sec ?? '') ? i.sec! : 'repos', { level: i.tone === 't-down' ? 2 : 1, text: i.text, since: i.since })
    if (pruneNames.length && !smuted.has('backup_prune'))
      add('cleanup', { level: 2, count: pruneNames.length, text: t('чистка не работает: {list}', { list: shortList(pruneNames, t, 3) }) })
    // по строке на то, что делать: перевести на чистку панели, удалить старые группы и т.п.
    if (rot.length && !smuted.has('backup_rotation'))
      for (const x of rotationLines(s, t)) add('cleanup', { level: 1, count: x.n, text: x.text })
    // чистку выключили, а бэкапы идут; чистка идет, хотя ее выключали в crontab (helper 0.34)
    for (const x of offLines(s, t)) add('cleanup', { level: 1, count: x.n, text: x.text })
    if (s.bsrv_restic_old)
      add('server', { level: 1, text: t('на сервере бэкапов старый restic {v} - обновите', { v: s.bsrv_restic_old }) })
    if (!every.length)
      add('repos', { level: 1, text: t('статистики репозиториев нет: включите ее (backupserver-setup)') })
    const dbs = audit.filter((x) => x.kind === 'db')
    if (dbs.length && !s.db_dumps_ok)
      add('coverage', {
        level: 1,
        count: dbs.length,
        text: t('{db} - нужен отдельный дамп', { db: shortList(dbAuditNames(dbs), t, 3) }),
      })
  }
  const okDb = audit.filter((x) => x.kind === 'db_ok').length
  const ok: Record<string, string> = {
    repos: all.length ? t('репозиториев: {n}, все в порядке', { n: all.length }) : '',
    archive: t('репозиториев: {n}, {sz}', { n: archivedRepos.length, sz: fmtBytes(archivedBytes) }),
    cleanup: ext ? t('чистка и проверка идут у всех') : t('нет данных'),
    server: subOf(
      info.version && `rest-server ${info.version}`,
      info.tls_front ? 'HTTPS' : 'HTTP',
      info.running ? t('запущен') : t('остановлен'),
    ),
    coverage: okDb ? t('бэкапится баз: {n}', { n: okDb }) : t('баз не найдено'),
    https: t('все клиенты по https'),
  }
  const infoSec: Record<string, SecInfo> = {}
  for (const { id } of BSRV_SECTIONS) infoSec[id] = secInfoOf(items[id] ?? [], ok[id] ?? '', t)
  // не проблема, но и не "все в порядке": старый rest-server, трафик без HTTPS
  const soft = (id: string, line: string) => {
    if (!infoSec[id].level) infoSec[id] = { ...infoSec[id], level: 1, line }
  }
  if (httpOpen.length) soft('https', t('по http: {list}', { list: shortList(httpOpen.map((c) => c.name), t, 3) }))
  if (srvOld) soft('server', t('rest-server {v}, есть {t}', { v: info.version ?? '', t: RESTSRV_TARGET }))
  else if (!info.tls_front) soft('server', t('клиенты ходят по HTTP, без TLS'))
  const gaps = audit.filter((x) => x.gap)
  if (gaps.length) soft('coverage', t('не бэкапится: {list}', { list: shortList(gaps.map((x) => x.subject), t, 3) }))
  if (!ext && !infoSec.cleanup.level) infoSec.cleanup.idle = true
  if (s.online) {
    const lockedAll = all.filter((r) => !muted.has(r.name) && r.locked && r.lock_ts && now - r.lock_ts > 86400)
    markMuted(infoSec, [
      ...srvMuted(s, t).filter((i) => i.section === 'backups' && i.srv)
        .map((i) => ({ sec: BSRV_SEC_IDS.has(i.sec ?? '') ? i.sec! : 'repos', text: i.text })),
      ...(lockedAll.length && smuted.has('backup_lock')
        ? [{ sec: 'repos', text: t('залочен больше суток: {list}', { list: shortList(lockedAll.map((r) => r.name), t, 3) }) }] : []),
      ...(pruneNames.length && smuted.has('backup_prune')
        ? [{ sec: 'cleanup', text: t('чистка не работает: {list}', { list: shortList(pruneNames, t, 3) }) }] : []),
      ...(rot.length && smuted.has('backup_rotation')
        ? rotationLines(s, t).map((x) => ({ sec: 'cleanup', text: x.text })) : []),
    ], t)
  }

  const rows: OvRow[] = BSRV_SECTIONS.flatMap(({ id, label }) =>
    (items[id] ?? []).map((x) => ({
      tone: x.level >= 2 ? ('t-down' as const) : ('t-degraded' as const),
      text: x.text,
      since: x.since,
      act: shown[id] ? { label: t(label), onClick: () => go(id) } : undefined,
    })),
  ).sort((a, b) => Number(b.tone === 't-down') - Number(a.tone === 't-down'))
  const tileLabel = (sec: string, label: string) =>
    infoSec[sec].fresh ? <>{label} <FreshChip since={infoSec[sec].fresh!} /></> : label
  const row = (k: string, v: ReactNode) => (
    <div className="kv-row">
      <span className="kv-k muted small">{k}</span>
      <span className="kv-v mono small">{v}</span>
    </div>
  )

  return createPortal(
    <div className="modal-backdrop">
      <div className="card detail detail-server hs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
            <OsIcon os={s.os} size={18} />
            <CountryFlag code={s.country} />
            <h3>{s.name}</h3>
            {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          </div>
          <div className="detail-head-actions">
            <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
              ✕
            </button>
          </div>
        </div>
        <div className="srv-meta srv-meta-line">
          <span className="srv-meta-item">
            <span className="srv-meta-k">rest-server</span>{' '}
            <span className={!info.running ? 't-down' : srvOld ? 't-degraded' : undefined}>{info.version || '—'}</span>
          </span>
          <span className="srv-meta-item">
            <span className={info.tls_front ? 't-up' : undefined}>{info.tls_front ? 'HTTPS' : 'HTTP'}</span>
            {info.tls_front && ` :${tlsPort}`}
          </span>
          {ext?.restic && (
            <span className="srv-meta-item">
              <span className="srv-meta-k">restic</span>{' '}
              <span className={s.bsrv_restic_old ? 't-degraded' : undefined}>{ext.restic}</span>
            </span>
          )}
          {info.data_dir && (
            <span className="srv-meta-item"><span className="srv-meta-k">{t('данные')}</span> <span className="mono">{info.data_dir}</span></span>
          )}
          <span className="srv-meta-item">
            <span className="srv-meta-k">{t('обновлено')}</span> {fmtRel(s.last_seen)}
          </span>
        </div>
        <div className="srv-layout" ref={layoutRef}>
          <CardNav secs={secs} active={cur.id} info={infoSec} onGo={go} />
          <div className="srv-main">
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
              {cur.id === 'repos' && all.length > 0 && (
                <div className="modal-toolbar srv-head-tools">
                  {all.length > 5 && (
                    <input className="checks-search modal-search" placeholder={t('Фильтр: имя репозитория…')}
                      value={q} onChange={(e) => setQ(e.target.value)} />
                  )}
                  <select className="modal-sort" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
                    <option value="problems">{t('сорт: проблемные')}</option>
                    <option value="name">{t('сорт: имя')}</option>
                    <option value="size">{t('сорт: размер')}</option>
                    <option value="age">{t('сорт: старые')}</option>
                    <option value="snapshots">{t('сорт: снапшоты')}</option>
                    {growers.length > 0 && <option value="growth">{t('сорт: прирост')}</option>}
                  </select>
                  {unmon.size > 0 && (
                    <button className={`type-chip ${onlyUnmon ? 'on' : ''}`} onClick={() => setOnlyUnmon(!onlyUnmon)}
                      title={t('Клиенты этих репозиториев не подключены к панели. Сломанный бэкап панель видит только по ритму: опоздавший бэкап через интервал и еще четверть его, ежедневный через 30 часов. Агент на ноде сказал бы сразу, как бэкап упал.')}>
                      {t('без агента в панели: {n}', { n: unmon.size })}
                    </button>
                  )}
                </div>
              )}
            </div>

            {cur.id === 'overview' && (
              <div className="srv-sec">
                <OvRows rows={rows} />
                <div className="net-tiles srv-tiles">
                  {/* Место на томе с репозиториями - первой плиткой: кончившееся место останавливает
                      бэкапы всего парка разом. Меряем df ПО КАТАЛОГУ ДАННЫХ (root-helper), а не общий
                      диск ноды: репозитории обычно на отдельном томе. На helper старше 0.18 данных нет. */}
                  {pct != null && (
                    <button className="srv-tile-btn" onClick={() => go('server')} title={t('к разделу')}>
                      <NetTile
                        label={t('Место на диске')}
                        value={`${pct}%`}
                        sub={t('свободно {f} из {tt}', { f: fmtBytes(free), tt: fmtBytes(info.disk_total) })}
                        tone={pct >= 90 ? 'bad' : pct >= 80 ? 'warn' : 'ok'}
                        title={info.data_dir || undefined}
                      />
                    </button>
                  )}
                  <button className="srv-tile-btn" onClick={() => go('repos')} title={t('к разделу')}>
                    <NetTile
                      label={tileLabel('repos', t('Репозитории'))}
                      value={String(all.length)}
                      sub={subOf(
                        bad > 0 && t('проблемных: {b}', { b: bad }),
                        archivedRepos.length > 0 && t('в архиве: {n}', { n: archivedRepos.length }),
                      ) || (all.length ? t('все ок') : undefined)}
                      tone={bad ? 'bad' : all.length ? 'ok' : undefined}
                    />
                  </button>
                  {checked.length > 0 && (
                    <button className="srv-tile-btn" onClick={() => go('repos')} title={t('к разделу')}>
                      <NetTile
                        label={t('Проверка целостности')}
                        value={String(checkedOk)}
                        unit={t('из {n}', { n: checked.length })}
                        sub={t('за 14 дней')}
                        tone={ctx.checks.size ? 'bad' : 'ok'}
                      />
                    </button>
                  )}
                  {growers.length > 0 && (
                    <button className="srv-tile-btn" onClick={() => { go('repos'); setSort('growth') }} title={t('к разделу')}>
                      <NetTile
                        label={t('Прирост за прогон')}
                        value={`+${fmtBytes(growTotal)}`}
                        sub={t('больше всех {name}: {s}', { name: growers[0].name, s: fmtBytes(growers[0].g) })}
                      />
                    </button>
                  )}
                </div>
                <OvAreas
                  cap={t('Сервер по областям')}
                  items={BSRV_SECTIONS.filter((x) => x.id !== 'overview' && shown[x.id]).map((x) => ({ id: x.id, label: x.label, info: infoSec[x.id] }))}
                  onGo={go}
                />
              </div>
            )}

            {cur.id === 'repos' && (
              <div className="srv-sec">
                {growers.length > 0 && (
                  <div className="muted small">
                    {t('За прогон клиенты добавляют около {s}: {top}', {
                      s: fmtBytes(growTotal),
                      top: growers.slice(0, 4).map((x) => `${x.name} ${fmtBytes(x.g)}`).join(', '),
                    })}
                  </div>
                )}
                {all.length === 0 ? (
                  <EnableServerStats />
                ) : (
                  <div className="loc-results docker-clist">
                    {repos.length === 0 ? (
                      <div className="srv-empty">{t('Ничего не найдено.')}</div>
                    ) : (
                      repos.map((r) => (
                        <RepoRow key={r.name} server={s} r={r} ctx={ctx} canAct={canAct} onChanged={onChanged}
                          growth={growthOf.get(r.name)} unmonitored={unmon.has(r.name)} />
                      ))
                    )}
                  </div>
                )}
                <div className="net-foot">
                  {t('Читается на сервере без паролей: config (валидность), снапшоты, размер, свежесть, лок, политика хранения. Опоздавшие и устаревшие - красным и алертят. То, что хранится как есть (сервера больше нет, разовый бэкап, проект заморожен), уберите в архив (🗄), временное приглушите (🔕).')}
                </div>
              </div>
            )}

            {cur.id === 'archive' && (
              <div className="srv-sec">
                <div className="muted small">
                  {t('Архив: {n} ({sz})', { n: archivedRepos.length, sz: fmtBytes(archivedBytes) })}
                  {' · '}{t('хранятся как есть, без алертов; проверка целостности идет')}
                </div>
                <div className="loc-results docker-clist">
                  {archivedRepos.map((r) => (
                    <ArchivedRow key={r.name} server={s} r={r} entry={(s.backup_repo_archive ?? {})[r.name]}
                      canAct={canAct} onChanged={onChanged} />
                  ))}
                </div>
              </div>
            )}

            {cur.id === 'cleanup' && (
              <div className="srv-sec">
                {/* У каждого пункта короткая шапка, что не так, список и "как исправить" с командами
                    под этот сервер (FixSteps). Старый скрипт - только с живыми репозиториями: архив
                    чистить не нужно, он идет числом */}
                {cl?.legacy && (
                  <LegacyPlaque leg={cl.legacy} when={cronTime(cl.legacy.schedule)}
                    last={cl.legacy.log_ts ? fmtAgo(cl.legacy.log_ts) : ''} />
                )}
                {cl?.dead && <DeadBlocks dead={cl.dead} />}
                {/* где не работает чистка - один список с причинами; в списке репозиториев
                    причина строкой под самим репозиторием */}
                {pruneNames.length > 0 && (
                  <div className="loc-results docker-clist">
                    {pruneNames.map((n) => (
                      <div key={n} className="loc-res docker-row repo-row repo-row-col t-down">
                        <div className="repo-row-main">
                          <span className="lk-dot lk-dot-bad" />
                          <div className="docker-c-main">
                            <div className="docker-c-name mono">{n}</div>
                            <div className="docker-c-img small t-down">{ctx.prune[n]}</div>
                          </div>
                        </div>
                        <RowFix steps={cl?.prune.find((x) => x.repo === n)?.fix ?? []} />
                      </div>
                    ))}
                  </div>
                )}
                {/* один репозиторий - одна строка со всеми причинами, под ней что сделать и шаги;
                    у старой группы - склеить с прежним именем, в архив, удалить */}
                {rotRows.length > 0 && (
                  <div className="loc-results docker-clist">
                    {rotRows.map((x) => (
                      <div key={x.repo} className="loc-res docker-row repo-row repo-row-col t-degraded">
                        <div className="repo-row-main">
                          <span className="lk-dot lk-dot-warn" />
                          <div className="docker-c-main">
                            <div className="docker-c-name mono">{x.repo}</div>
                            <div className="docker-c-img small repo-rot-why">
                              {x.reasons.length ? `${t('старые снапшоты не вычищаются')}: ${x.reasons.join(', ')}` : t('старые снапшоты не вычищаются')}
                            </div>
                            <div className="muted small">{rotationAction(x.cause, t)}</div>
                          </div>
                        </div>
                        <RowFix steps={x.fix} />
                        <RepoGroups server={s} repo={x.repo} ext={ext} canAct={canAct} onChanged={onChanged} />
                      </div>
                    ))}
                  </div>
                )}
                {/* чистка выключена вручную: спокойно, если чистить нечего (архив, бэкапов нет),
                    пунктом с шагами включить, если бэкапы идут */}
                <OffRows off={cl?.off ?? []} hand={cl?.hand ?? []} fmtAgo={fmtAgo} />
                {!pruneNames.length && !rotRows.length && !cl?.legacy && !cl?.dead && !cl?.off?.length && !cl?.hand?.length && (
                  <div className="srv-empty">{ok.cleanup}</div>
                )}
                {canAct && <BulkCleanup server={s} all={all} muted={muted} />}
              </div>
            )}

            {cur.id === 'server' && (
              <div className="srv-sec">
                {!info.running && <div className="net-prob">{t('rest-server остановлен')}</div>}
                <div className="kv-list">
                  {row('rest-server', (
                    <span className="restic-row">
                      {info.version || '—'} · {info.running ? t('запущен') : t('остановлен')}
                      {srvOld && <span className="t-degraded"> · {t('есть {v}', { v: RESTSRV_TARGET })}</span>}
                    </span>
                  ))}
                  {row(t('Протокол'), info.tls_front
                    ? `HTTPS · ${t('порт {p}', { p: tlsPort })}`
                    : `HTTP · ${t('без TLS')}`)}
                  {ext?.restic && row('restic', `${ext.restic} · ${t('для чистки и проверки')}`)}
                  {info.data_dir && row(t('данные'), info.data_dir)}
                  {pct != null && row(t('Место на диске'), `${pct}% · ${t('свободно {f} из {tt}', { f: fmtBytes(free), tt: fmtBytes(info.disk_total) })}`)}
                </div>
                {canAct && srvOld && <RestServerUpdate server={s} info={info} onChanged={onChanged} />}
                {!info.tls_front && (
                  <div className="net-note">
                    {t('трафик по HTTP без TLS. Данные шифрует restic на клиенте, но логин/пароль и метаданные идут открыто. Можно включить HTTPS (self-signed) кнопкой ниже.')}
                  </div>
                )}
                {canAct && !info.tls_front && <EnableTls server={s} onChanged={onChanged} />}
                {s.bsrv_restic_old && <ServerResticOld ver={s.bsrv_restic_old} target={ext?.restic_target || ''} />}
              </div>
            )}

            {cur.id === 'https' && <BsrvHttps server={s} clients={httpClients} canAct={canAct} onChanged={onChanged} />}
            {cur.id === 'coverage' && <CoverageAudit server={s} canManage={canAct} onChanged={onChanged} bare />}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

// Массовая зачистка: на серверах, живущих годами, «устарел» копится десятками — разбирать
// их по одной кнопке никто не станет. Отбор ТОЛЬКО по явному признаку (приглушён вручную
// или давно не обновлялся), здоровые репозитории под него не попадают ни при каких настройках.
function BulkCleanup({ server, all, muted }: {
  server: Server; all: RepoStat[]; muted: Set<string>
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<'stale' | 'muted'>('stale')
  const [days, setDays] = useState(365)
  const [copied, setCopied] = useState(false)
  const now = Date.now() / 1000
  const picked = all.filter((r) => {
    if (mode === 'muted') return muted.has(r.name)
    // «давно не обновлялся» — только по факту последнего бэкапа, а не по «выглядит плохо»
    const age = r.last_activity ? (now - r.last_activity) / 86400 : Infinity
    return age >= days
  })
  const unsafe = picked.filter((r) => !safeRepoName(r.name))
  const script = bulkCleanupScript(picked, BSRV_ROOT, server.name)
  const totalBytes = picked.reduce((a, r) => a + (r.size_bytes || 0), 0)
  if (all.length < 5) return null  // на паре репозиториев массовость смысла не имеет
  return (
    <div className="repo-cleanup bulk-cleanup">
      <button className="ghost repo-cleanup-toggle" onClick={() => setOpen(!open)}>
        {open ? t('скрыть') : t('🧹 массовая зачистка')}
      </button>
      {open && (
        <>
          <div className="bulk-row">
            <select className="modal-sort" value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
              <option value="stale">{t('не обновлялись дольше')}</option>
              <option value="muted">{t('приглушённые вручную')}</option>
            </select>
            {mode === 'stale' && (
              <>
                <input className="field-inp bulk-days" type="number" min={30} value={days}
                  onChange={(e) => setDays(Math.max(30, Number(e.target.value) || 365))} />
                <span className="muted small">{t('дней')}</span>
              </>
            )}
          </div>
          {picked.length === 0 ? (
            <div className="muted small">{t('Под условие никто не попал.')}</div>
          ) : (
            <>
              <div className="form-error small">
                ⚠️ {t('Попадает под удаление: {n} шт., {sz}. Восстановить будет нечем.', {
                  n: picked.length, sz: fmtBytes(totalBytes),
                })}
              </div>
              {unsafe.length > 0 && (
                <div className="form-error small">
                  {t('Пропущено с подозрительными именами: {n} — удалите вручную, разобравшись.', { n: unsafe.length })}
                </div>
              )}
              <div className="agent-advice-cmd">
                <pre>{script}</pre>
                <button className="ghost" onClick={() => {
                  navigator.clipboard?.writeText(script)
                  setCopied(true); window.setTimeout(() => setCopied(false), 1500)
                }}>{copied ? t('Скопировано') : t('Копировать')}</button>
              </div>
              <div className="muted small">
                {t('Скрипт спросит подтверждение числом. Панель ничего не удаляет — это защита на случай её взлома.')}
              </div>
            </>
          )}
        </>
      )}
    </div>
  )
}

function BackupServerRow({ server: s, info, onOpen }: { server: Server; info: BackupServerInfo; onOpen: () => void }) {
  const { t } = useI18n()
  const ctx = repoCtx(s)
  const repos = (info.repos ?? []).filter((r) => !ctx.archived.has(r.name))
  const archivedN = (info.repos ?? []).length - repos.length
  const bad = repos.filter((r) => repoBad(r, ctx)).length
  const pct = diskPct(info)
  // и покрытие тоже: бэкап-сервер в списке клиентов не рендерится, так что его
  // заглушённые пункты покрытия иначе оказались бы только в сводке
  const repoMutes = collectMutes(s, t, ['repo', 'audit'])
  return (
    <button className="loc-res loc-res-btn hs-host" onClick={onOpen}>
      <span className={`lk-dot ${s.online ? 'lk-dot-ok' : 'lk-dot-bad'}`} title={s.online ? t('на связи') : t('не на связи')} />
      <div className="hs-host-main">
        <div className="hs-host-name">
          <OsIcon os={s.os} />
          <CountryFlag code={s.country} />
          {s.name}
          {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          <span className="type-chip">rest-server {info.version || '—'}</span>
          {repoMutes.length > 0 && <MuteChip items={repoMutes} t={t} />}
          {!info.running && <span className="type-chip off">{t('остановлен')}</span>}
          {!!info.version && verLt(info.version, RESTSRV_TARGET) && (
            <span className="type-chip upd" title={t('доступно обновление до {v}', { v: RESTSRV_TARGET })}>
              ⬆ {RESTSRV_TARGET}
            </span>
          )}
        </div>
        <div className="mono muted small">
          {t('репозиториев: {n}', { n: repos.length })}
          {bad > 0 ? ` · ${t('проблемных: {b}', { b: bad })}` : ''}
          {archivedN > 0 ? ` · ${t('в архиве: {n}', { n: archivedN })}` : ''}
          {pct != null && (
            <>
              {' · '}
              <span className={pct >= 90 ? 't-down' : pct >= 80 ? 't-degraded' : ''}>{t('диск занят на {p}%', { p: pct })}</span>
            </>
          )}
        </div>
      </div>
      <div className="hs-host-count mono">
        <span className={bad > 0 ? 't-down' : 't-up'}>
          {bad > 0 ? `${bad} ⚠` : `${repos.length}`}
        </span>
      </div>
    </button>
  )
}

// строка сервера без настроенного бэкапа: кнопка «Настроить бэкап» + чекбокс «не требуется»
// Нода без бэкапа (или с галкой «не требуется») окна бэкапа не имеет, но находки аудита и свои
// бэкапы у неё есть - показываем их в окне того же вида, что окно бэкапа ноды: широкое, меню
// разделов слева. Раздел один - меню не нужно, одна широкая колонка
const NODE_COV_SECTIONS: SecDef[] = [
  { id: 'coverage', label: 'Покрытие', group: '' },
  { id: 'own', label: 'Свои бэкапы', group: '' },
]

function NodeCoverageModal({ server: s, canManage, initialSec, onChanged, onClose }: {
  server: Server; canManage: boolean; initialSec?: string | null; onChanged: () => void; onClose: () => void
}) {
  const { t } = useI18n()
  const [active, setActive] = useState<string>(initialSec ?? '')
  const layoutRef = useRef<HTMLDivElement>(null)
  const go = (id: string) => {
    setActive(id)
    const el = layoutRef.current
    if (el && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: 'start' })
  }
  const audit = (s.backup_audit ?? []).filter((x) => !x.muted)
  const own = s.custom_backups ?? []
  const ownBad = ownBackupProblems(s)
  const shown: Record<string, boolean> = { coverage: (s.backup_audit?.length ?? 0) > 0, own: own.length > 0 }
  const secs = NODE_COV_SECTIONS.filter((x) => shown[x.id])
  const cur = secs.find((x) => x.id === active) ?? secs[0]

  // числа в меню - как в окне бэкапа ноды: что не бэкапится и какой свой бэкап не отрабатывает
  const items: Record<string, SecItem[]> = {}
  const gaps = audit.filter((x) => x.gap)
  if (gaps.length)
    items.coverage = [{ level: 1, count: gaps.length, text: t('не бэкапится: {list}', { list: shortList(gaps.map((x) => x.subject), t, 3) }) }]
  if (ownBad.length)
    items.own = [{
      level: ownBad.some((j) => j.status === 'failed') ? 2 : 1,
      count: ownBad.length,
      text: t('свой бэкап не отрабатывает: {jobs}', { jobs: ownBad.map((j) => `${j.name} (${j.problem})`).join(', ') }),
    }]
  const info: Record<string, SecInfo> = {}
  for (const { id } of NODE_COV_SECTIONS) info[id] = secInfoOf(items[id] ?? [], '', t)
  const side: Record<string, string> = { own: String(own.filter((j) => !j.ignored).length) }

  const body = cur && (
    <>
      <div className="srv-main-head">
        <h4>{t(cur.label)}</h4>
      </div>
      {cur.id === 'coverage' && <CoverageAudit server={s} canManage={canManage} onChanged={onChanged} bare />}
      {cur.id === 'own' && <OwnBackups server={s} canManage={canManage} onChanged={onChanged} bare />}
    </>
  )
  return createPortal(
    <div className="modal-backdrop">
      <div className="card detail detail-server hs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
            <OsIcon os={s.os} size={18} />
            <CountryFlag code={s.country} />
            <h3>{s.name}</h3>
            {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
            {s.backup_not_required && <span className="type-chip">{t('бэкап не требуется')}</span>}
          </div>
          <div className="detail-head-actions">
            <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
              ✕
            </button>
          </div>
        </div>
        <div className="srv-meta srv-meta-line">
          <span className="srv-meta-item muted">
            {s.backup_not_required
              ? t('Бэкап на этой ноде помечен как не требующийся, но данные на ней есть')
              : hasOwnFileBackup(s)
                ? t('Панельного бэкапа на ноде нет, но есть свой - за ним панель следит')
                : t('Файловый бэкап на ноде не настроен. Что на ней обнаружено')}
          </span>
          {s.agent_ip && <span className="srv-meta-item mono">{s.agent_ip}</span>}
          <span className="srv-meta-item">
            <span className="srv-meta-k">{t('обновлено')}</span> {fmtRel(s.last_seen)}
          </span>
        </div>
        {secs.length > 1 ? (
          <div className="srv-layout" ref={layoutRef}>
            <CardNav secs={secs} active={cur.id} info={info} side={side} onGo={go} />
            <div className="srv-main">{body}</div>
          </div>
        ) : (
          <div className="srv-main srv-main-solo">
            {body ?? <div className="srv-empty">{t('На ноде ничего не найдено')}</div>}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

function NoBackupRow({ server: s, canAct, onSetup, onDeploy, onCoverage, onChanged, focus }: {
  server: Server; canAct: boolean; onSetup: () => void; onDeploy: () => void
  onCoverage?: () => void; onChanged: () => void; focus?: boolean
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const optedOut = s.backup_not_required
  const rowRef = useFocusRow(focus)
  const toggle = async () => {
    setBusy(true)
    try {
      await updateServer(s.id, { backup_not_required: !optedOut })
      onChanged()
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className={`loc-res hs-host hs-nb${focus ? ' hs-focus' : ''}`} ref={rowRef}>
      <span className={`lk-dot${optedOut ? '' : ' lk-dot-bad'}`} />
      <div className="hs-host-main">
        <div className="hs-host-name">
          <CountryFlag code={s.country} />
          {s.name}
          {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          {optedOut && <span className="type-chip">{t('не требуется')}</span>}
          <OwnBackupsChip server={s} />
        </div>
        <div className="mono muted small">{serverAddr(s)}</div>
        {s.backup_state?.traces && <div className="muted small">{s.backup_state.traces}</div>}
      </div>
      <div className="nobackup-actions">
        {canAct && !optedOut && (
          <button onClick={onSetup}>{t('Настроить бэкап')}</button>
        )}
        {canAct && !optedOut && (
          <button className="ghost" onClick={onDeploy}>{t('Сделать бэкап-сервером')}</button>
        )}
        {onCoverage && (
          <button className="ghost" onClick={onCoverage}>{t('Что на ноде')}</button>
        )}
        {canAct && (
          <label className="nobackup-check">
            <input type="checkbox" checked={!!optedOut} disabled={busy} onChange={toggle} />
            {t('бэкапить не требуется')}
          </label>
        )}
      </div>
    </div>
  )
}

function serverAddr(s: Server): string {
  return s.agent_ip || s.hostname || ''
}

// строка, на которую ведет ссылка с главной (?backup=id у ноды без бэкапа): прокрутить к ней
function useFocusRow(focus?: boolean) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (focus) ref.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [focus])
  return ref
}

// "Удалить настройку" умеют агент 2.45 и helper backup-setup 0.39
const DEPROV_AGENT = '2.45'
const DEPROV_HELPER = '0.39'

// Настройка бэкапа из панели упала на полпути: бэкапа нет, а на клиенте могли остаться restic,
// env с паролем и юниты. Причина - что ответила настройка; переделать поверх или снять то, что
// она успела поставить (репозиторий на бэкап-сервере не трогается)
function SetupProblemRow({ server: s, canAct, onRedo, onChanged, focus }: {
  server: Server; canAct: boolean; onRedo: () => void; onChanged: () => void; focus?: boolean
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const rowRef = useFocusRow(focus)
  const st = s.backup_state
  const helper = s.last_report?.setup_versions?.['backup-setup']
  const can = verAtLeast(s.agent_version, DEPROV_AGENT) && verAtLeast(helper != null ? String(helper) : '', DEPROV_HELPER)
  const remove = async () => {
    if (!window.confirm(t('Снять с {name} то, что успела поставить настройка бэкапа (юниты, env с паролем, скрипт, restic панели)? Репозиторий на бэкап-сервере останется.', { name: s.name }))) return
    setBusy(true)
    setErr(null)
    try {
      const c = await backupCommand(s.id, { action: 'deprovision' })
      let last = c
      // агент за ретранслятором или DPI забирает команду не сразу - ждем до полутора минут
      for (let i = 0; i < 60 && last.status !== 'done' && last.status !== 'error'; i++) {
        await new Promise((r) => setTimeout(r, 1500))
        last = await backupCommandStatus(s.id, c.id)
      }
      if (last.status !== 'done') setErr(last.result || t('не удалось'))
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('ошибка'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className={`loc-res hs-host hs-nb${focus ? ' hs-focus' : ''}`} ref={rowRef}>
      <span className="lk-dot lk-dot-bad" />
      <div className="hs-host-main">
        <div className="hs-host-name">
          <CountryFlag code={s.country} />
          {s.name}
          {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          {st?.since && <span className="muted small">{fmtRel(st.since)}</span>}
        </div>
        <div className="mono muted small">{serverAddr(s)}</div>
        <div className="small t-down">{t('настройка бэкапа не прошла: {why}', { why: st?.reason || t('без причины') })}</div>
        {err && <div className="form-error small">{err}</div>}
      </div>
      {canAct && (
        <div className="nobackup-actions">
          <button disabled={busy} onClick={onRedo}>{t('Переделать настройку')}</button>
          <button className="ghost" disabled={busy || !can} onClick={remove}
            title={can ? undefined : t('нужны агент {a} и helper backup-setup {h} - обновите их на сервере', { a: DEPROV_AGENT, h: DEPROV_HELPER })}>
            {busy ? '…' : t('Удалить настройку')}
          </button>
        </div>
      )}
    </div>
  )
}

// дефолтный exclude-список (синхронен с бэкендом) — бэкапим «/» кроме мусора.
// НЕ исключаем данные: /etc /home /root /srv /opt /var/www /var/lib/<БД> /lib65 и
// /var/lib/docker/volumes (тома) бэкапятся. Исключаем только псевдо-ФС, temp/кэш,
// переустанавливаемое из пакетов и слои образов контейнеров (тянутся из реестра).
const DEFAULT_EXCLUDES = [
  // псевдо-ФС (не файлы)
  '/proc', '/sys', '/dev', '/run', '/var/lib/lxcfs',
  // temp / кэш / мусор
  '/tmp', '/var/tmp', '/var/cache', '/var/lib/apt/lists', '/var/lib/systemd/coredump', '/lost+found',
  // swap
  '/swapfile', '/swap.img',
  // переустанавливаемое из пакетов
  '/usr', '/boot', '/snap', '/lib/modules', '/lib/firmware',
  // слои образов docker/containerd (регенерируются; тома /var/lib/docker/volumes НЕ трогаем)
  '/var/lib/docker/overlay2', '/var/lib/docker/containers', '/var/lib/docker/tmp',
  '/var/lib/docker/buildkit', '/var/lib/containerd',
  // точки монтирования внешних/сменных носителей и логи
  '/mnt', '/media', '/var/log',
]

// «Сделать бэкап-сервером»: поднять rest-server на ноде с нуля. Два состояния — helper ещё
// не установлен (показываем команду) и установлен (порт + TLS → развёртывание с прогрессом).
function DeployServerModal({ server: s, onClose, onDone }: {
  server: Server; onClose: () => void; onDone: () => void
}) {
  const { t } = useI18n()
  const [port, setPort] = useState(64100)
  const [tls, setTls] = useState(true)
  const [job, setJob] = useState<BackupSetupJob | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  // агент не отличает «helper есть, сервер не развёрнут» от «ничего нет» → смотрим версии
  const helperReady = !!s.last_report?.setup_versions?.['backupserver-setup']
  const cmd = `curl -fsSL ${window.location.origin}/api/agent/backupserver-setup.sh | sudo bash`

  const STEP_LABEL: Record<string, string> = {
    'deploy-server': t('rest-server (docker, restic, htpasswd)'),
    'tls-front': t('TLS-фронт на бэкап-сервере'),
  }

  const run = async () => {
    setErr(null); setBusy(true)
    try {
      const j = await deployBackupServer(s.id, { port, tls })
      setJob(j)
      let cur = j
      while (cur.status === 'running' && alive.current) {
        await new Promise((r) => setTimeout(r, 2500))
        if (!alive.current) return
        cur = await backupSetupStatus(s.id, j.id)
        setJob(cur)
      }
      if (cur.status === 'done' && alive.current) window.setTimeout(() => alive.current && onDone(), 1400)
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('ошибка'))
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  return createPortal(
    <div className="modal-backdrop">
      <div className="modal-card backup-manage-modal">
        <div className="modal-head">
          <h3>{t('Сделать бэкап-сервером')}: {s.name}</h3>
          <button className="ghost" onClick={onClose}>{t('Закрыть')}</button>
        </div>
        {!helperReady ? (
          <div className="backup-setup-form">
            <div className="muted small">
              {t('Сначала поставьте на ноду helper бэкап-сервера (root). Он же ставит docker, restic и htpasswd, если их нет:')}
            </div>
            <div className="agent-advice-cmd">
              <pre>{cmd}</pre>
              <button className="ghost" onClick={() => {
                navigator.clipboard?.writeText(cmd); setCopied(true)
                window.setTimeout(() => setCopied(false), 1500)
              }}>{copied ? t('Скопировано') : t('Копировать')}</button>
            </div>
            <div className="muted small">
              {t('После установки вернитесь сюда — появится кнопка развёртывания.')}
            </div>
          </div>
        ) : !job ? (
          <div className="backup-setup-form">
            <div>
              <label className="backup-manage-label">{t('Порт rest-server')}</label>
              <input className="field-inp backup-time-inp" type="number" min={1024} max={65535}
                value={port} onChange={(e) => setPort(Math.min(65535, Math.max(1024, +e.target.value || 64100)))} />
            </div>
            <label className="nobackup-check">
              <input type="checkbox" checked={tls} onChange={(e) => setTls(e.target.checked)} />
              {t('HTTPS/TLS (self-signed на порту 64101)')}
            </label>
            {err && <div className="form-error small">{err}</div>}
            <div className="backup-manage-actions">
              <button disabled={busy} onClick={run}>{busy ? '…' : t('▶ Развернуть')}</button>
            </div>
            <div className="muted small">
              {t('Панель поставит docker/restic/htpasswd из штатных реп дистрибутива и поднимет rest-server в режиме append-only + private-repos. Репозитории клиентов создаются потом, отдельно. До ~2 мин.')}
            </div>
          </div>
        ) : (
          <div className="backup-setup-progress">
            {job.steps.map((st, i) => (
              <div key={i} className={`setup-step ${st.ok ? 't-up' : 't-down'}`}>
                <span className="mono">{st.ok ? '✓' : '✗'}</span> {STEP_LABEL[st.step] || st.step}
                {!st.ok && st.detail && <span className="muted small"> — {st.detail}</span>}
              </div>
            ))}
            {job.status === 'running' && (
              <div className="setup-step muted"><span className="sdot sdot-unknown" /> {t('выполняется…')}</div>
            )}
            <div className={`setup-final ${job.status === 'done' ? 't-up' : job.status === 'error' ? 'form-error' : 'muted'}`}>
              {job.status === 'done' ? `✅ ${t('бэкап-сервер развёрнут')}` : job.status === 'error' ? `❌ ${job.message}` : ''}
            </div>
            {job.status !== 'running' && (
              <div className="backup-manage-actions">
                <button onClick={job.status === 'done' ? onDone : onClose}>{t('Закрыть')}</button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

// «Настроить бэкап»: выбор бэкап-сервера/режима/путей/времени/retention → оркестрация с прогрессом
function BackupSetupModal({ server: s, backupServers, onClose, onDone }: {
  server: Server; backupServers: Server[]; onClose: () => void; onDone: () => void
}) {
  const { t } = useI18n()
  // на ноде уже есть бэкап (то же правило, что у раздела): настройка только поверх, с согласия.
  // found: список устарел, а бэкап на сервере уже есть (ответ 409) - тоже только поверх
  const [found, setFound] = useState(false)
  const hasBackup = backupAlive(s.last_report?.backup) || found
  // Переустановка поверх (в том числе перевод на https) начинает с текущих настроек: сервер бэкапов,
  // пути, время и хранение. С умолчаниями она молча сбрасывала свои пути на стандартные
  const cur = hasBackup && s.last_report?.backup?.manageable ? s.last_report.backup : null
  const curSrv = cur ? backupDest(s, backupServers)?.srv : undefined
  const curRepo = cur ? backupRepoStat(cur.repo_dest, backupServers, s) : undefined
  const curMode: 'exclude' | 'include' = cur?.mode === 'include' ? 'include' : 'exclude'
  const [bsId, setBsId] = useState<number>(
    curSrv && backupServers.some((x) => x.id === curSrv.id) ? curSrv.id : backupServers[0]?.id ?? 0)
  const [mode, setMode] = useState<'exclude' | 'include'>(curMode)
  const [paths, setPaths] = useState(
    cur ? ((curMode === 'include' ? cur.includes : cur.excludes) ?? []).join('\n') : DEFAULT_EXCLUDES.join('\n'))
  const [schedule, setSchedule] = useState(cur?.schedule || '23:00')
  const [tls, setTls] = useState(true)
  const [ret, setRet] = useState({
    last: curRepo?.keep_last ?? 3, daily: curRepo?.keep_daily ?? 7,
    weekly: curRepo?.keep_weekly ?? 4, monthly: curRepo?.keep_monthly ?? 6,
  })
  const [reinstall, setReinstall] = useState(false)
  const [job, setJob] = useState<BackupSetupJob | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  const switchMode = (m: 'exclude' | 'include') => {
    setMode(m)
    setPaths(m === 'exclude' ? DEFAULT_EXCLUDES.join('\n') : '')
  }

  const run = async () => {
    setErr(null)
    if (!bsId) { setErr(t('выберите бэкап-сервер')); return }
    if (!/^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(schedule)) { setErr(t('время в формате ЧЧ:ММ')); return }
    const list = paths.split('\n').map((x) => x.trim()).filter(Boolean)
    if (mode === 'include' && list.length === 0) { setErr(t('для режима «только» укажите пути')); return }
    if (hasBackup && !reinstall) { setErr(t('на сервере уже есть бэкап - отметьте "переустановить поверх"')); return }
    setBusy(true)
    try {
      const j = await backupSetup(s.id, {
        backup_server_id: bsId, mode, paths: list, schedule, tls,
        keep_last: ret.last, keep_daily: ret.daily, keep_weekly: ret.weekly, keep_monthly: ret.monthly,
        reinstall: hasBackup && reinstall,
      })
      setJob(j)
      let cur = j
      while (cur.status === 'running' && alive.current) {
        await new Promise((r) => setTimeout(r, 2500))
        if (!alive.current) return
        cur = await backupSetupStatus(s.id, j.id)
        setJob(cur)
      }
      if (cur.status === 'done' && alive.current) window.setTimeout(() => alive.current && onDone(), 1400)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // пока окно было открыто, бэкап настроили (или агент о нем только что сообщил)
        setFound(true)
        setErr(t('На сервере уже есть бэкап: отметьте "переустановить поверх", если его надо переписать'))
        return
      }
      setErr(e instanceof Error ? e.message : t('ошибка'))
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  const STEP_LABEL: Record<string, string> = {
    'tls-front': t('TLS-фронт на бэкап-сервере'),
    'get-cert': t('сертификат'),
    'provision-repo': t('репозиторий на бэкап-сервере'),
    'existing-repo': t('репозиторий уже был — подключились к нему, история сохранена'),
    'provision-client': t('бэкап на клиенте'),
    'first-backup': t('первый бэкап'),
  }

  return createPortal(
    <div className="modal-backdrop">
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Настроить бэкап')}: {s.name}</h3>
          <button className="ghost" onClick={onClose}>{t('Закрыть')}</button>
        </div>
        {backupServers.length === 0 ? (
          <div className="backup-setup-form">
            <div className="card muted">
              {t('На этой панели нет бэкап-серверов. Чтобы настраивать бэкапы отсюда, добавьте под мониторинг ноду с rest-server — она появится как бэкап-сервер, и её можно будет выбрать целью.')}
            </div>
            <div className="backup-manage-actions">
              <button onClick={onClose}>{t('Закрыть')}</button>
            </div>
          </div>
        ) : !job ? (
          <div className="backup-setup-form">
            {s.backup_state?.state === 'partial' && (
              <div className="card muted small">
                {t('Прошлая настройка не прошла: {why}. Настройка поверх перепишет то, что она успела поставить.', { why: s.backup_state.reason || t('без причины') })}
              </div>
            )}
            {!hasBackup && s.backup_state?.traces && (
              <div className="card muted small">{t('{traces} - настройка поставит бэкап заново.', { traces: s.backup_state.traces })}</div>
            )}
            {hasBackup && (
              <div className="card small">
                <div>{t('На сервере уже есть бэкап. Настройка поверх перепишет его на клиенте: расписание, пути, пароль и адрес репозитория. Репозиторий и история на бэкап-сервере останутся, если выбран тот же сервер.')}</div>
                <label className="nobackup-check">
                  <input type="checkbox" checked={reinstall} onChange={(e) => setReinstall(e.target.checked)} />
                  {t('переустановить поверх')}
                </label>
              </div>
            )}
            <label className="backup-manage-label">{t('Бэкап-сервер')}</label>
            <select className="field-inp" value={bsId} onChange={(e) => setBsId(Number(e.target.value))}>
              {backupServers.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>

            <div className="backup-manage-row">
              <label className="backup-manage-label">{t('Что бэкапить')}</label>
              <div className="win-switch">
                <button className={`win-btn${mode === 'exclude' ? ' win-btn-active' : ''}`} onClick={() => switchMode('exclude')}>{t('Всё, кроме (exclude)')}</button>
                <button className={`win-btn${mode === 'include' ? ' win-btn-active' : ''}`} onClick={() => switchMode('include')}>{t('Только (include)')}</button>
              </div>
            </div>
            <textarea className="backup-paths" value={paths} onChange={(e) => setPaths(e.target.value)}
              placeholder={mode === 'include' ? '/etc\n/var/www\n/home' : '/proc\n/sys'} />

            <div>
              <label className="backup-manage-label">{t('Время (ежедневно)')}</label>
              <input className="field-inp backup-time-inp" type="time" step={60}
                value={schedule} onChange={(e) => setSchedule(e.target.value)} />
            </div>
            <div>
              <label className="backup-manage-label">{t('Сколько копий хранить')}</label>
              <div className="retention-row">
                {([['last', t('последних')], ['daily', t('дневных')], ['weekly', t('недельных')], ['monthly', t('месячных')]] as const).map(([k, lbl]) => (
                  // все поля >=1: нельзя сконфигурить 0 и потерять последний срез бэкапа
                  <label key={k} className="retention-field">
                    <span className="muted small">{lbl}</span>
                    <input type="number" min={1} max={3650} value={ret[k]}
                      onChange={(e) => setRet({ ...ret, [k]: Math.max(1, +e.target.value || 1) })} />
                  </label>
                ))}
              </div>
            </div>
            <label className="nobackup-check">
              <input type="checkbox" checked={tls} onChange={(e) => setTls(e.target.checked)} />
              {t('HTTPS/TLS (self-signed на порту 64101)')}
            </label>
            {err && <div className="form-error small">{err}</div>}
            <div className="backup-manage-actions">
              <button disabled={busy || (hasBackup && !reinstall)} onClick={run}>{busy ? '…' : hasBackup ? t('▶ Переустановить и запустить') : t('▶ Настроить и запустить')}</button>
            </div>
            <div className="muted small">
              {t('Панель создаст репозиторий на бэкап-сервере (htpasswd/init/prune) и настроит restic-бэкап на клиенте. Данные шифруются на клиенте. До ~2 мин.')}
            </div>
          </div>
        ) : (
          <div className="backup-setup-progress">
            {job.steps.map((st, i) => (
              <div key={i} className={`setup-step ${st.ok ? 't-up' : 't-down'}`}>
                <span className="mono">{st.ok ? '✓' : '✗'}</span> {STEP_LABEL[st.step] || st.step}
                {!st.ok && st.detail && <span className="muted small"> — {st.detail}</span>}
              </div>
            ))}
            {job.status === 'running' && (
              <div className="setup-step muted"><span className="sdot sdot-unknown" /> {t('выполняется…')}</div>
            )}
            <div className={`setup-final ${job.status === 'done' ? 't-up' : job.status === 'error' ? 'form-error' : 'muted'}`}>
              {job.status === 'done' ? `✅ ${t('бэкап настроен')}` : job.status === 'error' ? `❌ ${job.message}` : ''}
            </div>
            {job.status !== 'running' && (
              <div className="backup-manage-actions">
                <button onClick={job.status === 'done' ? onDone : onClose}>{t('Закрыть')}</button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

type BHost = { s: Server; d: BackupInfo }
type BFilter = 'clients' | 'nobackup' | 'servers' | 'verify'

// Плитка "Восстановление проверено X из N": под нее попадают клиенты без пройденной проверки,
// N - X строк. Строка под именем - что с проверкой (тексты бэкенда и так начинаются с того, что
// не так); null - проверка пройдена
function verifyHit(s: Server, t: (k: string, p?: Record<string, string | number>) => string): string | null {
  const v = s.backup_verify
  if (!v) return t('восстановление не проверялось')
  if (v.level < 1) return null
  if (v.problems.length) return v.problems.join('; ')
  return v.level >= 2 ? t('восстановление не прошло') : t('восстановление с замечаниями')
}

export function BackupsPage({
  onUnauthorized,
  openHostId = null,
  openSec = null,
  openSrvHostId = null,
  openSrvSec = null,
  onConsumed,
  onSrvConsumed,
}: {
  onUnauthorized: () => void
  openHostId?: number | null
  openSec?: string | null // раздел окна бэкапа ноды из ссылки: ?backup=id&sec=coverage
  openSrvHostId?: number | null
  openSrvSec?: string | null // раздел окна сервера бэкапов: ?backupsrv=id&sec=cleanup
  onConsumed?: () => void
  onSrvConsumed?: () => void
}) {
  const { t } = useI18n()
  const { isViewer, isAdmin } = useAuth()
  const [servers, setServers] = useState<Server[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [openId, setOpenId] = useState<number | null>(openHostId)
  // раздел из ссылки - до первого открытия окна этой ноды
  const [linkSec, setLinkSec] = useState<{ id: number | null; sec: string | null }>({ id: openHostId, sec: openSec })
  const [openSrvId, setOpenSrvId] = useState<number | null>(openSrvHostId)
  const [linkSrvSec, setLinkSrvSec] = useState<{ id: number | null; sec: string | null }>({ id: openSrvHostId, sec: openSrvSec })
  const [setupSrv, setSetupSrv] = useState<Server | null>(null)
  const [deploySrv, setDeploySrv] = useState<Server | null>(null)
  // Только id, а не сам объект: модалка обязана видеть свежий сервер после каждой
  // перезагрузки списка. С копией, снятой в момент открытия, галочка внутри не
  // перерисовывалась — клик срабатывал, но выглядел как проигнорированный, и
  // изменение обнаруживалось только при закрытии окна.
  const [covId, setCovId] = useState<number | null>(null)
  // подвал «не требуется» свёрнут по умолчанию; выбор помним между заходами
  const [showOptOut, setShowOptOut] = useState(
    () => localStorage.getItem('kervax.backups.showOptOut') === '1',
  )
  useEffect(() => {
    localStorage.setItem('kervax.backups.showOptOut', showOptOut ? '1' : '0')
  }, [showOptOut])
  useEffect(() => {
    if (openHostId != null) {
      setOpenId(openHostId)
      setLinkSec({ id: openHostId, sec: openSec })
      onConsumed?.()
    }
  }, [openHostId, openSec, onConsumed])
  useEffect(() => {
    if (openSrvHostId != null) {
      setOpenSrvId(openSrvHostId)
      setLinkSrvSec({ id: openSrvHostId, sec: openSrvSec })
      onSrvConsumed?.()
    }
  }, [openSrvHostId, openSrvSec, onSrvConsumed])
  useUrlCard('backup', openId)
  useUrlCard('backupsrv', openSrvId)
  const [query, setQuery] = useState('')
  const { tile, setTile, tileProps } = useTileFilter<BFilter>()
  const [searchRo, setSearchRo] = useState(true) // см. поле поиска ниже
  const [groupBy, setGroupBy] = useState<'none' | 'group'>(
    () => (localStorage.getItem('kervax_backup_groupby') as 'none' | 'group') || 'group',
  )
  const setGrouping = (g: 'none' | 'group') => {
    setGroupBy(g)
    localStorage.setItem('kervax_backup_groupby', g)
  }

  const load = useCallback(() => {
    listServers()
      .then((s) => {
        setServers(s)
        setErr(null)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        setErr(e instanceof Error ? e.message : t('Ошибка'))
      })
  }, [onUnauthorized, t])

  useEffect(() => {
    load()
    const id = window.setInterval(load, 12000)
    return () => window.clearInterval(id)
  }, [load])

  const srvHosts = (servers ?? [])
    .map((s) => ({ s, d: s.last_report?.backup_server }))
    .filter((x): x is { s: Server; d: BackupServerInfo } => !!x.d?.present)
    .sort((a, b) => a.s.name.localeCompare(b.s.name))
  const openSrv = srvHosts.find(({ s }) => s.id === openSrvId)
  const covSrv = (servers ?? []).find((x) => x.id === covId) ?? null

  const allHosts = (servers ?? [])
    .map((s) => ({ s, d: s.last_report?.backup }))
    // клиент = ЖИВОЙ бэкап (см. backupAlive). Мёртвый бэкап (снесли restic+таймер,
    // остался лишь осиротевший метрик) — НЕ клиент, уходит в «Без бэкапа». Сервер бэкапов со
    // своим живым бэкапом на другой сервер - тоже клиент: иначе его бэкап нигде не виден, а
    // ссылка ?backup=id из алерта уводила в окно сервера
    .filter((x): x is BHost => backupAlive(x.d))
  // заглушенное по бэкапам: репозитории (у бэкап-серверов) и пункты покрытия (у клиентов) -
  // плиткой "Заглушено" в ряду плиток, список в панели справа (общая с "Серверами")
  const mutes = muteRows(servers ?? [], t, ['repo', 'audit'])
  const mutesSub = mutesTileSub(mutes, t)
  const nRepo = mutes.filter((r) => r.group === 'repo').length
  const [mutesOpen, setMutesOpen] = useState(false)
  // сервера БЕЗ живого бэкапа (по умолчанию бэкап нужен → алерт). Бэкап-серверы и
  // живые клиенты сюда не попадают; мёртвый/осиротевший бэкап — попадает (можно настроить заново).
  const noPanelBackup = (servers ?? []).filter(
    (s) =>
      s.online &&
      !s.last_report?.backup_server?.present &&
      !backupAlive(s.last_report?.backup),
  )
  // свой файловый бэкап (restic/borg/rsync без панели): нода не «без бэкапа», за ним следим
  // упавшая на полпути настройка из панели (backup_state partial): своим блоком "Проблемы" с
  // причиной и кнопками, а не в "Без бэкапа" - там казалось, что никто и не пробовал
  const setupBad = noPanelBackup.filter((s) => s.backup_state?.state === 'partial').sort((a, b) => a.name.localeCompare(b.name))
  const ownOnly = noPanelBackup.filter((s) => s.backup_state?.state !== 'partial' && hasOwnFileBackup(s)).sort((a, b) => a.name.localeCompare(b.name))
  const noBackupAll = noPanelBackup.filter((s) => s.backup_state?.state !== 'partial' && !hasOwnFileBackup(s))
  // требуют бэкапа (галка не стоит) — наверх, красным; «не требуется» — в отдельный подвал внизу
  const noBackupReq = noBackupAll.filter((s) => !s.backup_not_required).sort((a, b) => a.name.localeCompare(b.name))
  const noBackupOpt = noBackupAll.filter((s) => s.backup_not_required).sort((a, b) => a.name.localeCompare(b.name))
  // сводка плитками: клиенты по статусу, ноды без бэкапа, бэкап-серверы, проверка восстановления
  const failedN = allHosts.filter(({ d }) => backupStatus(d) === 'failed').length
  const staleN = allHosts.filter(({ d }) => backupStatus(d) === 'stale').length
  const ownBadN = allHosts.filter(({ s }) => ownBackupProblems(s).length > 0).length
  const ownOnlyBadN = ownOnly.filter((s) => ownBackupProblems(s).length > 0).length
  const srvStat = srvHosts.map(({ s, d }) => {
    const ctx = repoCtx(s)
    const repos = (d.repos ?? []).filter((r) => !ctx.archived.has(r.name))
    return { repos: repos.length, bad: repos.filter((r) => repoBad(r, ctx)).length, pct: diskPct(d) ?? -1, down: !d.running }
  })
  const repoN = srvStat.reduce((a, x) => a + x.repos, 0)
  const repoBadN = srvStat.reduce((a, x) => a + x.bad, 0)
  const srvDownN = srvStat.filter((x) => x.down).length
  const pctMax = Math.max(-1, ...srvStat.map((x) => x.pct))
  const verified = allHosts.filter(({ s }) => s.backup_verify)
  const verBadN = verified.filter(({ s }) => (s.backup_verify?.level ?? 0) >= 2).length
  const verWarnN = verified.filter(({ s }) => (s.backup_verify?.level ?? 0) === 1).length
  const q = query.trim().toLowerCase()
  // Поиск обязан действовать на ВСЮ страницу. Раньше он сужал только клиентов, а
  // бэкап-серверы и «не требуется» оставались как есть — при активном фильтре это
  // читалось как «ничего не бэкапится», хотя ноды просто не подходили под запрос.
  const match = (s: Server) =>
    !q || [s.name, s.group_name].join(' ').toLowerCase().includes(q) || srvIpHit(s, q)
  // Нажатая плитка оставляет свой блок страницы, тот, что она считает: клиентов, бэкап-серверы,
  // ноды без бэкапа (вместе со своим бэкапом и "не требуется" - их числа в той же плитке) или
  // клиентов без пройденной проверки восстановления. Поиск действует поверх
  const showSec = (sec: BFilter) => !tile || tile === sec || (sec === 'clients' && tile === 'verify')
  const hosts = (showSec('clients') ? allHosts : [])
    .filter(({ s }) => match(s))
    .map((h) => ({ ...h, hint: tile === 'verify' ? verifyHit(h.s, t) : null }))
    .filter((h) => tile !== 'verify' || h.hint)
    .sort((a, b) => a.s.name.localeCompare(b.s.name))
  const srvHostsView = showSec('servers') ? srvHosts.filter(({ s }) => match(s)) : []
  const ownOnlyView = showSec('nobackup') ? ownOnly.filter(match) : []
  const setupBadView = showSec('nobackup') ? setupBad.filter(match) : []
  const noBackupReqView = showSec('nobackup') ? noBackupReq.filter(match) : []
  const noBackupOptView = showSec('nobackup') ? noBackupOpt.filter(match) : []
  const nothing = hosts.length + srvHostsView.length + ownOnlyView.length + noBackupReqView.length + noBackupOptView.length + setupBadView.length === 0
  // нажатая плитка: подпись чипа над списком и что сказать, если под нее ничего не попало
  const filterLabel: Record<BFilter, string> = {
    clients: t('клиенты'), nobackup: t('без бэкапа'), servers: t('бэкап-серверы'),
    verify: t('без пройденной проверки восстановления'),
  }
  const filterEmpty: Record<BFilter, string> = {
    clients: t('Клиентов с бэкапом нет.'), nobackup: t('Бэкап есть у всех нод.'), servers: t('Бэкап-серверов нет.'),
    verify: t('Восстановление проверено у всех клиентов.'),
  }

  const groups: { key: string; label: string; items: typeof hosts }[] =
    groupBy === 'none'
      ? [{ key: 'all', label: '', items: hosts }]
      : (() => {
          const map = new Map<string, typeof hosts>()
          for (const h of hosts) {
            const k = h.s.group_name?.trim() || ''
            if (!map.has(k)) map.set(k, [])
            map.get(k)!.push(h)
          }
          return [...map.keys()]
            .sort((a, b) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)))
            .map((k) => ({ key: k, label: k || t('Без группы'), items: map.get(k)! }))
        })()

  const open = allHosts.find(({ s }) => s.id === openId)
  // нода не клиент бэкапа (без бэкапа / «не требуется») — открываем ей модалку покрытия,
  // иначе клик из «Требует действий» упирался бы в пустоту
  const [focusId, setFocusId] = useState<number | null>(null)
  useEffect(() => {
    if (openId == null || open) return
    const srv = (servers ?? []).find((x) => x.id === openId)
    if (!srv) return
    const row = setupBad.some((x) => x.id === srv.id) || noBackupAll.some((x) => x.id === srv.id)
    if (srv.last_report?.backup_server?.present) setOpenSrvId(openId)
    else if (row && linkSec.sec !== 'coverage') setFocusId(srv.id)
    else if ((srv.backup_audit?.length ?? 0) > 0) setCovId(srv.id)
    setOpenId(null)
  }, [openId, open, servers, setupBad, noBackupAll, linkSec.sec])

  return (
    <div>
      <div className="checks-head">
        <h2 className="docker-title">💾 {t('Бэкапы')}</h2>
        <div className="checks-head-actions">
          {allHosts.length > 1 && (
            <div className="win-switch">
              <button
                className={`win-btn${groupBy === 'group' ? ' win-btn-active' : ''}`}
                onClick={() => setGrouping('group')}
              >
                {t('По группе')}
              </button>
              <button
                className={`win-btn${groupBy === 'none' ? ' win-btn-active' : ''}`}
                onClick={() => setGrouping('none')}
              >
                {t('Без групп')}
              </button>
            </div>
          )}
        </div>
      </div>
      {servers && allHosts.length + srvHosts.length + noBackupAll.length + ownOnly.length + setupBad.length > 0 && (
        <div className="net-tiles hs-tiles">
          <NetTile
            label={t('Клиенты')}
            value={String(allHosts.length)}
            sub={subOf(
              failedN > 0 && t('ошибка: {n}', { n: failedN }),
              staleN > 0 && t('не свежий: {n}', { n: staleN }),
              ownBadN > 0 && t('свой бэкап с проблемой: {n}', { n: ownBadN }),
            ) || (allHosts.length ? t('все свежие') : undefined)}
            tone={failedN || staleN ? 'bad' : ownBadN ? 'warn' : allHosts.length ? 'ok' : undefined}
            {...tileProps('clients')}
          />
          <NetTile
            label={t('Без бэкапа')}
            value={String(noBackupReq.length + setupBad.length)}
            sub={subOf(
              setupBad.length > 0 && t('настройка не прошла: {n}', { n: setupBad.length }),
              ownOnly.length > 0 && t('свой бэкап: {n}', { n: ownOnly.length }),
              ownOnlyBadN > 0 && t('с проблемой: {n}', { n: ownOnlyBadN }),
              noBackupOpt.length > 0 && t('не требуется: {n}', { n: noBackupOpt.length }),
            ) || t('у всех нод есть')}
            tone={noBackupReq.length || setupBad.length || ownOnlyBadN ? 'bad' : 'ok'}
            {...tileProps('nobackup')}
          />
          {srvHosts.length > 0 && (
            <NetTile
              label={t('Бэкап-серверы')}
              value={String(srvHosts.length)}
              sub={subOf(
                t('репозиториев: {n}', { n: repoN }),
                repoBadN > 0 && t('проблемных: {b}', { b: repoBadN }),
                srvDownN > 0 && t('остановлено: {n}', { n: srvDownN }),
                pctMax >= 0 && t('диск до {p}%', { p: pctMax }),
              )}
              tone={repoBadN || srvDownN || pctMax >= 90 ? 'bad' : pctMax >= 80 ? 'warn' : 'ok'}
              {...tileProps('servers')}
            />
          )}
          {verified.length > 0 && (
            <NetTile
              label={t('Восстановление проверено')}
              value={String(verified.length - verBadN - verWarnN)}
              unit={t('из {n}', { n: allHosts.length })}
              sub={subOf(
                verBadN > 0 && t('не прошло: {n}', { n: verBadN }),
                verWarnN > 0 && t('с замечаниями: {n}', { n: verWarnN }),
              ) || undefined}
              tone={verBadN ? 'bad' : verWarnN ? 'warn' : verified.length === allHosts.length ? 'ok' : undefined}
              {...tileProps('verify')}
            />
          )}
          {/* как "Бэкап-серверы": нет заглушенного - нет и плитки, здесь это редкость */}
          {mutes.length > 0 && (
            <NetTile
              label={t('Заглушено')}
              value={String(mutes.length)}
              sub={subOf(
                mutesSub.where,
                nRepo > 0 && t('репозиториев: {n}', { n: nRepo }),
                mutes.length > nRepo && t('пунктов покрытия: {n}', { n: mutes.length - nRepo }),
              )}
              title={mutes.map((r) => `${r.server}: ${r.label}`).join('\n')}
              active={mutesOpen}
              onClick={() => setMutesOpen(true)}
            />
          )}
        </div>
      )}
      {allHosts.length > 1 && (
        <div className="checks-search-row">
          {/* Chrome видел на странице поля пароля (сейф), считал её формой входа и
              подставлял сюда сохранённый логин: поиск молча превращался в фильтр
              «admin», и казалось, что бэкапов нет. autocomplete=off он на таких
              страницах игнорирует, поэтому ещё и readOnly до фокуса — в readOnly
              браузер не пишет вовсе. */}
          <input
            className="checks-search"
            type="search"
            name="backup-search"
            autoComplete="off"
            readOnly={searchRo}
            onFocus={() => setSearchRo(false)}
            placeholder={t('Поиск: нода, IP, группа…')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button className="ghost search-clear" onClick={() => setQuery('')}>
              {t('Сбросить')}
            </button>
          )}
        </div>
      )}
      {tile && <TileFilterChip label={filterLabel[tile]} onClear={() => setTile(null)} />}
      {err && <p className="form-error">{err}</p>}
      {servers && allHosts.length === 0 && srvHosts.length === 0 && noBackupAll.length === 0 && ownOnly.length === 0 && (
        <div className="card muted">
          {t('Ноды с restic-бэкапом не найдены. Агент определяет бэкап сам (метрики + systemd); если бэкап есть, но раздел пуст — обновите агент.')}
        </div>
      )}
      <div className="net-cards hs-groups">
        {srvHostsView.length > 0 && (
          <div className="chart-card net-card net-card-wide">
            <div className="net-head">
              <div className="chart-cap net-cap">
                {t('Бэкап-серверы')}
                <span className="net-n">{srvHostsView.length}</span>
              </div>
            </div>
            <div className="loc-results">
              {srvHostsView.map(({ s, d }) => (
                <BackupServerRow key={s.id} server={s} info={d} onOpen={() => setOpenSrvId(s.id)} />
              ))}
            </div>
          </div>
        )}
        {setupBadView.length > 0 && (
          <div className="chart-card net-card net-card-wide">
            <div className="net-head">
              <div className="chart-cap net-cap">
                {t('Проблемы')}
                <span className="net-n net-n-bad">{setupBadView.length}</span>
              </div>
            </div>
            <div className="loc-results">
              {setupBadView.map((s) => (
                <SetupProblemRow key={s.id} server={s} canAct={!isViewer} focus={focusId === s.id}
                  onRedo={() => setSetupSrv(s)} onChanged={load} />
              ))}
            </div>
          </div>
        )}
        {noBackupReqView.length > 0 && (
          <div className="chart-card net-card net-card-wide">
            <div className="net-head">
              <div className="chart-cap net-cap">
                {t('Без бэкапа')}
                <span className="net-n net-n-bad">{noBackupReqView.length}</span>
              </div>
            </div>
            <div className="loc-results">
              {noBackupReqView.map((s) => (
                <NoBackupRow key={s.id} server={s} canAct={!isViewer} focus={focusId === s.id}
                  onSetup={() => setSetupSrv(s)} onDeploy={() => setDeploySrv(s)} onChanged={load} />
              ))}
            </div>
          </div>
        )}
        {ownOnlyView.length > 0 && (
          <div className="chart-card net-card net-card-wide">
            <div className="net-head">
              <div className="chart-cap net-cap">
                {t('Свой бэкап (настроен без панели)')}
                <span className="net-n">{ownOnlyView.length}</span>
              </div>
            </div>
            <div className="loc-results">
              {ownOnlyView.map((s) => {
                const bad = ownBackupProblems(s).length > 0
                return (
                  <button key={s.id} className="loc-res loc-res-btn hs-host" onClick={() => {
                    // из этого списка открываем сразу раздел своих бэкапов
                    setLinkSec({ id: s.id, sec: 'own' })
                    setCovId(s.id)
                  }}>
                    <span className={`lk-dot ${bad ? 'lk-dot-bad' : 'lk-dot-ok'}`}
                      title={bad ? t('свой бэкап с проблемой') : t('свой бэкап работает')} />
                    <div className="hs-host-main">
                      <div className="hs-host-name">
                        <CountryFlag code={s.country} />
                        {s.name}
                        {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
                        <OwnBackupsChip server={s} />
                      </div>
                      <div className="mono muted small">{serverAddr(s)}</div>
                    </div>
                  </button>
                )
              })}
            </div>
          </div>
        )}
        {groupBy !== 'none' && hosts.length > 0 &&
          srvHostsView.length + noBackupReqView.length + noBackupOptView.length + ownOnlyView.length > 0 && (
          <div className="chart-cap hs-sect">{t('Клиенты (что бэкапится)')}</div>
        )}
        {servers && (tile ? nothing : allHosts.length > 0 && hosts.length === 0) && (
          <div className="chart-card net-card net-card-wide muted">
            <div>
              {q
                ? t('По запросу «{q}» ничего нет — это фильтр, а не отсутствие бэкапов.', { q: query.trim() })
                : tile ? filterEmpty[tile] : t('Ничего не найдено.')}
              {q && (
                <button className="ghost search-clear" onClick={() => setQuery('')}>
                  {t('Сбросить')}
                </button>
              )}
            </div>
          </div>
        )}
        {groups.filter((g) => g.items.length > 0).map((g) => (
          <div key={g.key} className="chart-card net-card net-card-wide">
            <div className="net-head">
              <div className="chart-cap net-cap">
                {groupBy === 'none' ? t('Клиенты') : g.label}
                <span className="net-n">{g.items.length}</span>
              </div>
            </div>
            <div className="loc-results">
              {g.items.map(({ s, d, hint }) => (
                <HostRow
                  key={s.id}
                  server={s}
                  backup={d}
                  showGroup={groupBy !== 'group'}
                  dest={backupDest(s, servers ?? [])}
                  hint={hint}
                  onOpen={() => setOpenId(s.id)}
                />
              ))}
            </div>
          </div>
        ))}
        {/* «не требуется» — подвал: это не проблема, а осознанно снятые ноды. Свёрнуто
            по умолчанию, состояние помним между заходами. */}
        {noBackupOptView.length > 0 && (
          <div className="chart-card net-card net-card-wide hs-optout">
            <div className="net-head">
              <div className="chart-cap net-cap">
                {t('Бэкап не требуется')}
                <span className="net-n">{noBackupOptView.length}</span>
              </div>
              {/* нажата плитка "Без бэкапа": ее "не требуется" видно сразу */}
              {tile !== 'nobackup' && (
                <button className="ghost small" onClick={() => setShowOptOut(!showOptOut)}>
                  {showOptOut ? t('скрыть') : t('показать')}
                </button>
              )}
            </div>
            {(showOptOut || tile === 'nobackup') && (
              <div className="loc-results">
                {noBackupOptView.map((s) => (
                  <NoBackupRow key={s.id} server={s} canAct={!isViewer}
                    onSetup={() => setSetupSrv(s)} onDeploy={() => setDeploySrv(s)}
                    onCoverage={(s.backup_audit?.length ?? 0) > 0 ? () => setCovId(s.id) : undefined}
                    onChanged={load} />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
      {/* Сейф доступов — в подвале, рядом со справочными блоками: это не ежедневная
          работа, а страховка на день восстановления. Виден только админам. */}
      {isAdmin && <VaultPanel servers={servers ?? []} />}
      {mutesOpen && (
        <MutesPanel
          rows={mutes}
          servers={servers ?? []}
          canEdit={!isViewer}
          onOpen={(id) => {
            // у бэкап-сервера (репозитории) свое окно, у клиента (покрытие) - свое
            setMutesOpen(false)
            const s = (servers ?? []).find((x) => x.id === id)
            if (s?.last_report?.backup_server?.present) setOpenSrvId(id)
            else setOpenId(id)
          }}
          onChanged={(u) => setServers((prev) => (prev ?? []).map((x) => (x.id === u.id ? u : x)))}
          onUnauthorized={onUnauthorized}
          onClose={() => setMutesOpen(false)}
        />
      )}
      {open && (
        <BackupModal
          key={open.s.id}
          server={open.s}
          backup={open.d}
          repo={backupRepoStat(open.d.repo_dest, servers ?? [], open.s)}
          dest={backupDest(open.s, servers ?? [])}
          canManage={!isViewer}
          initialSec={linkSec.id === open.s.id ? linkSec.sec : null}
          onChanged={load}
          onClose={() => {
            setOpenId(null)
            setLinkSec({ id: null, sec: null })
          }}
          onReinstall={() => {
            setOpenId(null)
            setLinkSec({ id: null, sec: null })
            setSetupSrv(open.s)
          }}
          onOpenDest={(d) => {
            setOpenId(null)
            setLinkSec({ id: null, sec: null })
            if (d.last_report?.backup_server?.present) setOpenSrvId(d.id)
            else window.location.assign(`?server=${d.id}`)
          }}
        />
      )}
      {openSrv && (
        <BackupServerModal
          key={openSrv.s.id}
          server={openSrv.s}
          info={openSrv.d}
          servers={servers ?? []}
          canAct={!isViewer}
          initialSec={linkSrvSec.id === openSrv.s.id ? linkSrvSec.sec : null}
          onChanged={load}
          onClose={() => {
            setOpenSrvId(null)
            setLinkSrvSec({ id: null, sec: null })
          }}
        />
      )}
      {setupSrv && (
        <BackupSetupModal
          server={setupSrv}
          backupServers={srvHosts.map((h) => h.s)}
          onClose={() => setSetupSrv(null)}
          onDone={() => { setSetupSrv(null); load() }}
        />
      )}
      {covSrv && (
        <NodeCoverageModal server={covSrv} canManage={!isViewer}
          initialSec={linkSec.id === covSrv.id ? linkSec.sec : null}
          onChanged={load}
          onClose={() => {
            setCovId(null)
            setLinkSec({ id: null, sec: null })
          }} />
      )}
      {deploySrv && (
        <DeployServerModal
          server={deploySrv}
          onClose={() => setDeploySrv(null)}
          onDone={() => { setDeploySrv(null); load() }}
        />
      )}
    </div>
  )
}
