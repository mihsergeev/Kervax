import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  backupCommand,
  backupCommandStatus,
  backupCredentials,
  backupRepoMute,
  backupSetup,
  backupSetupStatus,
  CURRENT_BACKUP_HELPER,
  deployBackupServer,
  enableBackupTls,
  listServers,
  type BackupCommand,
  type BackupCreds,
  type BackupInfo,
  type BackupServerInfo,
  type BackupSetupJob,
  type RepoStat,
  type Server,
} from './api'
import { updateServer } from './api'
import { backupWindowNote } from './serverUtils'
import { OsIcon, osShort } from './osIcon'
import { CoverageAudit } from './CoverageAudit'
import { bulkCleanupScript, safeRepoName } from './bulkCleanupScript'
import { useAuth } from './auth'
import { useI18n, currentLang, tr } from './i18n'
import { MuteChip, MutesBanner, collectMutes } from './mutes'
import { VaultPanel } from './VaultPanel'
import { CountryFlag } from './CountryFlag'
import { byteUnits, fmtDur } from './units'

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

// «живой» бэкап: есть systemd-таймер ИЛИ restic на месте и хоть раз бэкапил. Только
// осиротевший метрик (restic снесён, таймера нет) — мёртвый бэкап → нода в «Без бэкапа».
function backupAlive(b?: BackupInfo | null): boolean {
  if (!b || !b.present) return false
  if (b.configured) return true
  if (b.restic_found && b.metric_present) return true
  return false
}

// имя бэкап-сервера назначения из repo_dest (rest://host:port/name → host → имя ноды-сервера)
function backupDestName(
  repoDest: string | undefined,
  servers: Server[],
  client?: Server,
): string {
  if (repoDest) {
    const host = repoDest.match(/rest:[a-z]+:\/\/([^:/]+)/)?.[1] || ''
    if (host) {
      const srv = servers.find(
        (s) => (s.external_ip === host || s.agent_ip === host || s.hostname === host) &&
          s.last_report?.backup_server?.present,
      )
      return srv?.name || host
    }
  }
  // Фолбэк для нод со старым helper'ом: они repo_dest не отдают вовсе. Ищем со стороны
  // бэкап-сервера — репозиторий называется по имени клиента, так что совпадение надёжное.
  if (client) {
    for (const s of servers) {
      const repos = s.last_report?.backup_server?.repos
      if (!repos) continue
      if (repos.some((r) => r.name === client.hostname || r.name === client.name)) return s.name
    }
  }
  return ''
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
      setMsg({ ok: res.status === 'done', text: res.result || (res.status === 'done' ? t('готово') : t('ошибка')) })
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
        if (r.status !== 'done') throw new Error(r.result || t('ошибка'))
      }
      if (pathsDirty) {
        const r = await runAndWait(s.id, { action: 'set_paths', mode, paths: curList })
        if (r.status !== 'done') throw new Error(r.result || t('ошибка'))
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
      <div className="backup-manage-head">{t('Управление')}</div>

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
function BackupModal({
  server: s,
  backup: b,
  repo,
  canManage,
  onChanged,
  onClose,
}: {
  server: Server
  backup: BackupInfo
  repo?: RepoStat
  canManage: boolean
  onChanged: () => void
  onClose: () => void
}) {
  const { isAdmin } = useAuth()
  const { t } = useI18n()
  const [rbusy, setRbusy] = useState(false)
  const [rmsg, setRmsg] = useState<string | null>(null)
  // оптимистично прячем кнопку сразу после успеха: версия в отчёте обновится лишь со
  // следующим тиком агента (~15с), а «кнопка не пропала» читается как «не сработало»
  const [rdone, setRdone] = useState(false)
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
      setRmsg(ok ? (res.result || t('готово')) : (res.result || t('не удалось')))
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
  return createPortal(
    <div className="modal-backdrop">
      <div className="card modal docker-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />{' '}
            <OsIcon os={s.os} /> <CountryFlag code={s.country} /> {s.name}
          </h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="docker-host-head">
          {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          <span className={`type-chip ${statusTone(st)}`}>{stLabel[st]}</span>
          {b.restic_version && <span className="type-chip mono">{shortRestic(b.restic_version)}</span>}
          {s.os && <span className="type-chip">{osShort(s.os)}</span>}
        </div>
        <div className="kv-list">
          {row(t('Последний бэкап'), b.last_backup_ts
            ? `${fmtAgo(b.last_backup_ts)} · ${new Date(b.last_backup_ts * 1000).toLocaleString()}`
            : '—')}
          {row(t('Результат'), b.metric_present
            ? (b.success === 1 ? t('успех') : t('ошибка'))
            : b.ts_source === 'systemd'
              ? `${b.service_result === 'success' ? t('успех') : b.service_result || t('нет данных')} ${t('(по systemd)')}`
              : t('нет метрики'))}
          {row(t('Длительность'),
            // full > restic заметно → показываем обе: дамп занимает время ДО restic,
            // и по одной restic-длительности не понять, когда бэкап реально закончился
            (b.full_duration_sec || 0) > (b.duration_sec || 0) + 2
              ? `${fmtDur(b.full_duration_sec)} ${t('(дампы + restic)')} · restic ${fmtDur(b.duration_sec)}`
              : fmtDur(b.duration_sec))}
          {(b.started_ts || 0) > 0 && b.last_backup_ts && row(t('Окно бэкапа'),
            `${new Date(b.started_ts! * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} → ${new Date(b.last_backup_ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`)}
          {backupWindowNote(s) && (
            <div className="backup-window-note">⏰ {t('Не уложился в окно')}: {backupWindowNote(s)}</div>
          )}
          {row(t('Пропущен (лок занят)'), b.skipped ? t('да') : '—')}
          {row(t('Таймер'), `${b.timer_enabled ? t('включён') : t('выключен')} · ${b.timer_active ? t('активен') : t('неактивен')}`)}
          {b.service_result && row(t('Сервис'), b.service_result)}
          {b.repo_dest && row(t('Куда бэкапится'), b.repo_dest)}
          {/* объёмы: дампы БД (что кладём на диск перед restic) + физический размер репо
              на бэкап-сервере (вся история, дедуп+сжатие). Логический размер снапшота требует
              пароля репо (агенту недоступен) — не показываем, репо-размер понятнее. */}
          {(() => {
            const dumpBytes = (b.dumps ?? []).reduce((a, d) => a + (d.size_bytes || 0), 0)
            const dumpFiles = (b.dumps ?? []).reduce((a, d) => a + (d.files || 0), 0)
            return dumpBytes > 0
              ? row(t('Дампы БД'), `${fmtBytes(dumpBytes)} · ${t('файлов: {n}', { n: dumpFiles })}`)
              : null
          })()}
          {repo && (repo.size_bytes || 0) > 0 && row(t('В репозитории'),
            `${fmtBytes(repo.size_bytes)} · ${t('снапшотов: {n}', { n: repo.snapshots })}`)}
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
        {b.notes && b.notes.length > 0 && (
          <div className="backup-notes">
            {b.notes.map((n, i) => (
              <div key={i} className="form-error small">⚠️ {n}</div>
            ))}
          </div>
        )}
        <CoverageAudit server={s} canManage={canManage} onChanged={onChanged} />
        {canManage && b.manageable && (b.helper_version || 0) < CURRENT_BACKUP_HELPER && (
          <div className="form-error small" style={{ marginTop: '0.5rem' }}>
            ⚠️ {t('helper на ноде устарел — переустановите, чтобы подхватить новые фичи/фиксы:')}
          </div>
        )}
        {canManage && b.manageable && (b.helper_version || 0) < CURRENT_BACKUP_HELPER && <EnableManage />}
        {/* пароль репозитория = ключ дешифровки бэкапа: только админу (см. бэкенд) */}
        {isAdmin && b.manageable && <RestoreInfo server={s} />}
        {canManage && b.manageable ? (
          <ManageSection server={s} backup={b} onChanged={onChanged} />
        ) : (
          <div className="muted small">
            {t('Статус читается без секретов. Управление доступно после установки helper на ноде (кнопка ниже).')}
          </div>
        )}
        {!b.manageable && (
          <EnableManage />
        )}
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
  onOpen,
}: {
  server: Server
  backup: BackupInfo
  showGroup?: boolean
  dest?: string
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
    <button className="check-row srv-row docker-host-row" onClick={onOpen}>
      <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
      <div className="check-main">
        <div className="check-name">
          <OsIcon os={s.os} />
          <CountryFlag code={s.country} />
          {s.name}
          {showGroup && s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          {b.restic_version && <span className="type-chip mono">{shortRestic(b.restic_version)}</span>}
          {!b.configured && <span className="type-chip off">{t('не настроен')}</span>}
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
      </div>
      {/* куда бэкапится — справа, отдельной колонкой: это «адрес назначения», а не
          свойство самой ноды, и глазами удобнее сверять по одной вертикали */}
      {dest && (
        <div className="backup-dest mono muted small" title={t('куда бэкапится')}>
          🗄 {dest}
        </div>
      )}
      <div className="docker-host-count mono">
        <span className={statusTone(st)}>{stLabel[st]}</span>
      </div>
    </button>
  )
}

const STALE_REPO_SECONDS = 3 * 86400
// Лок держится всё время бэкапа и освежается раз в 5 мин — сам по себе он значит
// «бэкап идёт», а не «сломалось». Проблема — лок, который перестали освежать.
const LOCK_STUCK_SECONDS = 30 * 60
function lockStuck(r: RepoStat): boolean {
  if (!r.locked) return false
  if (!r.lock_ts) return true // helper < v5 времени лока не отдаёт — считаем висячим
  return Date.now() / 1000 - r.lock_ts > LOCK_STUCK_SECONDS
}
type RepoState = 'muted' | 'invalid' | 'locked' | 'running' | 'stale' | 'ok'
function repoState(r: RepoStat, muted: Set<string>): RepoState {
  if (muted.has(r.name)) return 'muted'
  if (!r.valid) return 'invalid'
  if (lockStuck(r)) return 'locked'
  if (r.locked) return 'running'
  if (r.last_activity && Date.now() / 1000 - r.last_activity > STALE_REPO_SECONDS) return 'stale'
  return 'ok'
}
// проблемный = не ок и не заглушён (для счётчиков/акцентов)
function repoBad(r: RepoStat, muted: Set<string>): boolean {
  const st = repoState(r, muted)
  return st !== 'ok' && st !== 'muted' && st !== 'running'
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
function RotationInfo({ r, t }: {
  r: RepoStat
  t: (s: string, v?: Record<string, string | number>) => string
}) {
  const oldest = r.oldest_snapshot ?? 0
  const ts = r.rotation_ts ?? 0
  if (!oldest && !ts) return null // старый helper — метрик ротации ещё нет
  const limit = rotationLimitDays(r)
  const ageDays = oldest ? Math.floor((Date.now() / 1000 - oldest) / 86400) : 0
  const stale = !!(oldest && limit && ageDays > limit)
  const removed = r.rotation_removed ?? -1
  return (
    <>
      {ts > 0 && (
        <span title={r.rotation_ok === 0 ? t('последний прогон завершился ошибкой') : ''}>
          {t('чистка')}: {fmtAgo(ts)}
          {removed >= 0 && ` (${t('снято {n}', { n: removed })})`}
        </span>
      )}
      {oldest > 0 && (
        <span
          className={stale ? 't-down' : ''}
          title={limit
            ? t('политика допускает снапшоты возрастом до {n} дн.', { n: limit })
            : t('политика хранения не задана — судить не о чем')}
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
  const [copied, setCopied] = useState(false)
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
              ⚠️ {t('Будет стёрто безвозвратно: {n} снапшотов, {sz}, последний {ago}. Восстановить будет нечем — других копий этих данных у панели нет.', {
                n: r.snapshots, sz: fmtBytes(r.size_bytes), ago: fmtAgo(r.last_activity),
              })}
              <div className="muted small">
                {t('Удаляйте, только если сервер выведен из эксплуатации. Если бэкап ещё может понадобиться — приглушите репозиторий (🔕): он перестанет считаться проблемой, а данные останутся.')}
              </div>
            </div>
          ) : (
            <div className="muted small">
              {t('Репозиторий пустой и не инициализирован — удалять нечего, кроме мусора.')}
            </div>
          )}
          <div className="muted small">
            {t('Панель бэкапы не удаляет (это защита от взлома), выполните от root на бэкап-сервере:')}
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
          <div className="muted small">
            {t('Последняя строка — чтобы панель сразу забыла репу (иначе до следующей минуты). Если репу заводил Ansible, у неё может быть ещё строка в root-crontab — проверьте `sudo crontab -l`.')}
          </div>
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
  const [copied, setCopied] = useState(false)
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
    `  echo "${tr('пароль из env не подходит (репо от другого деплоя) — снимаю лок файлом')}"`,
    `  ls -l "$REPO"/locks/ 2>/dev/null`,
    `  rm -f "$REPO"/locks/* && echo "${tr('лок-файлы удалены')}"`,
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
          <div className="form-error small">
            ⚠️ {t('Сначала убедитесь, что по этому репозиторию НЕ идёт бэкап или prune прямо сейчас — снимать живой лок нельзя. Обычно лок остаётся после аварийно прерванной операции.')}
          </div>
          <div className="agent-advice-cmd">
            <pre>{cmd}</pre>
            <button className="ghost" onClick={() => {
              navigator.clipboard?.writeText(cmd)
              setCopied(true); window.setTimeout(() => setCopied(false), 1500)
            }}>{copied ? t('Скопировано') : t('Копировать')}</button>
          </div>
          <div className="muted small">
            {t('Выполнять на бэкап-сервере от root. Пароль читается из prune-env внутри команды и в списке процессов не виден.')}
          </div>
          {/* Статус снимается не мгновенно: helper пересчитывает статистику раз в минуту,
              потом отчёт агента и опрос UI. Без этой подсказки снятый лок выглядел как
              «панель не заметила», и человек шёл проверять второй раз. */}
          <div className="repo-unlock-after">
            <button className="ghost" disabled={checking} onClick={() => {
              setChecking(true)
              onChanged()
              window.setTimeout(() => setChecking(false), 2000)
            }}>{checking ? t('Проверяю…') : t('Снял — проверить')}</button>
            <span className="muted small">
              {t('Статус обновится и сам, в течение минуты: сервер пересчитывает статистику по расписанию.')}
            </span>
          </div>
        </>
      )}
    </div>
  )
}

function RepoRow({ server: s, r, muted, canAct, onChanged }: {
  server: Server; r: RepoStat; muted: Set<string>; canAct: boolean; onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const st = repoState(r, muted)
  const isMuted = st === 'muted'
  // именно repoBad, а не своя копия условия: иначе строка и счётчики проблем расходятся
  // (так «идёт бэкап» — нормальное состояние — какое-то время красилось красным)
  const tone = isMuted ? 't-muted' : repoBad(r, muted) ? 't-down' : 't-up'
  const ageSec = r.last_activity ? Date.now() / 1000 - r.last_activity : 0
  const freshTone = !r.last_activity ? 'muted' : ageSec > STALE_REPO_SECONDS ? 't-down' : ageSec > 2 * 86400 ? 't-degraded' : 't-up'
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
  return (
    <div className={`loc-res docker-row repo-row repo-row-col ${tone}`}>
      <div className="repo-row-main">
      <div className="docker-c-main">
        <div className="docker-c-name mono">
          {r.name}
          {isMuted && <span className="type-chip">{t('приглушён')}</span>}
          {st === 'invalid' && <span className="type-chip off">{t('невалиден')}</span>}
          {st === 'locked' && <span className="type-chip off">{t('залочен')}</span>}
          {st === 'running' && <span className="type-chip">{t('идёт бэкап')}</span>}
          {st === 'stale' && <span className="type-chip off">{t('устарел')}</span>}
        </div>
        <div className="docker-c-img mono muted small repo-meta">
          <span>{t('снапшотов: {n}', { n: r.snapshots })}</span>
          <span>{fmtBytes(r.size_bytes)}</span>
          <span className={freshTone === 'muted' ? '' : freshTone}>{t('обновлён')}: {fmtAgo(r.last_activity)}</span>
          {hasKeep && (
            <span title={t('политика хранения: последних / дневных / недельных / месячных')}>
              {t('хранит')}: {keep.map((v) => v ?? 0).join('/')}
            </span>
          )}
          <RotationInfo r={r} t={t} />
        </div>
      </div>
      {/* Приглушать нечего, пока репозиторий здоров: кнопка на каждой зелёной строке —
          это десятки бесполезных элементов, среди которых теряются нужные. Показываем
          только там, где есть что глушить, и у уже приглушённых (чтобы вернуть). */}
      {canAct && st !== 'ok' && st !== 'running' && (
        <button className="ghost icon-btn repo-mute-btn" disabled={busy} onClick={toggleMute}
          title={isMuted ? t('Снять приглушение') : t('Приглушить (разовый/неактуальный)')}>
          {busy ? '…' : isMuted ? '🔔' : '🔕'}
        </button>
      )}
      </div>
      {/* «как удалить» даём только у проблемных: пустой мусор и заброшенные репо — это те,
          что реально выводят из эксплуатации. У живого репозитория с ежедневным бэкапом
          такая кнопка была бы приглашением к беде. */}
      {canAct && (junk || st === 'stale') && <RepoCleanup r={r} />}
      {canAct && st === 'locked' && <RepoUnlock r={r} onChanged={onChanged} />}
      {canAct && brokenWithData && (
        <div className="muted small repo-cleanup">
          ⚠️ {t('Нет config, но снапшоты есть ({n} шт., {sz}) — данные могут быть восстановимы. Вслепую не удаляйте, разберитесь на сервере.', { n: r.snapshots, sz: fmtBytes(r.size_bytes) })}
        </div>
      )}
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
      setMsg(ok ? (res.result || t('готово')) : (res.result || t('не удалось')))
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

/** Заполнение тома с репозиториями — первое, что нужно видеть на бэкап-сервере:
 * кончившееся место останавливает бэкапы всего парка разом. Меряем df ПО КАТАЛОГУ
 * ДАННЫХ (root-helper), а не общий диск ноды: репозитории обычно на отдельном томе.
 * Данные появляются с helper 0.18 — на старых просто не рисуем полоску. */
function StorageBar({ info, t }: { info: BackupServerInfo; t: (k: string, p?: Record<string, string | number>) => string }) {
  const total = info.disk_total || 0
  if (!total) return null
  const used = info.disk_used || 0
  const free = info.disk_free ?? Math.max(total - used, 0)
  const pct = Math.min(100, Math.round((used / total) * 100))
  // пороги как у дисков серверов: 90% — уже проблема, 80% — пора смотреть
  const tone = pct >= 90 ? 't-down' : pct >= 80 ? 't-degraded' : 't-up'
  return (
    <div className="bs-storage" title={info.data_dir || ''}>
      <div className="bs-storage-top">
        <span className="muted small">
          {t('Место на диске')}
          {info.data_dir ? <span className="mono"> · {info.data_dir}</span> : null}
        </span>
        <span className={`mono small ${tone}`}>
          {t('свободно {f} из {tt}', { f: fmtBytes(free), tt: fmtBytes(total) })} · {pct}%
        </span>
      </div>
      <div className="srv-bar">
        <span className={`srv-bar-fill ${tone}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function BackupServerModal({ server: s, info, canAct, onChanged, onClose }: {
  server: Server; info: BackupServerInfo; canAct: boolean; onChanged: () => void; onClose: () => void
}) {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<'problems' | 'name' | 'size' | 'age' | 'snapshots'>('problems')
  const muted = new Set(s.backup_repo_mutes ?? [])
  const all = info.repos ?? []
  const bad = all.filter((r) => repoBad(r, muted)).length
  const ql = q.trim().toLowerCase()
  const rank = (r: RepoStat) => (repoBad(r, muted) ? 0 : muted.has(r.name) ? 2 : 1)
  const rcmp: Record<string, (a: RepoStat, b: RepoStat) => number> = {
    problems: (a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name),
    name: (a, b) => a.name.localeCompare(b.name),
    size: (a, b) => (b.size_bytes ?? 0) - (a.size_bytes ?? 0) || a.name.localeCompare(b.name),
    age: (a, b) => (a.last_activity || Infinity) - (b.last_activity || Infinity) || a.name.localeCompare(b.name),
    snapshots: (a, b) => b.snapshots - a.snapshots || a.name.localeCompare(b.name),
  }
  const repos = all
    .filter((r) => !ql || r.name.toLowerCase().includes(ql))
    .slice()
    .sort(rcmp[sort])
  return createPortal(
    <div className="modal-backdrop">
      <div className="card modal docker-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>
            <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />{' '}
            <OsIcon os={s.os} /> <CountryFlag code={s.country} /> {s.name}
          </h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <StorageBar info={info} t={t} />
        <div className="docker-host-head">
          <span className="type-chip">🗄 rest-server {info.version || '—'}</span>
          {s.os && <span className="type-chip">{osShort(s.os)}</span>}
          <span className={`type-chip ${info.running ? 't-up' : 't-down'}`}>
            {info.running ? t('запущен') : t('остановлен')}
          </span>
          {info.tls_front ? (
            <span className="type-chip t-up" title={t('клиенты ходят по HTTPS (self-signed TLS-фронт на порту {p})', { p: info.tls_port || 64101 })}>
              🔒 HTTPS
            </span>
          ) : (
            <span className="type-chip off" title={t('трафик по HTTP без TLS. Данные шифрует restic на клиенте, но логин/пароль и метаданные идут открыто. Можно включить HTTPS (self-signed) кнопкой ниже.')}>
              HTTP
            </span>
          )}
          <span className="muted small">
            {bad > 0
              ? t('репозиториев: {n} · проблемных: {b}', { n: all.length, b: bad })
              : t('репозиториев: {n} · все ок', { n: all.length })}
          </span>
          {canAct && <RestServerUpdate server={s} info={info} onChanged={onChanged} />}
          {canAct && !info.tls_front && <EnableTls server={s} onChanged={onChanged} />}
        </div>
        {all.length === 0 ? (
          <EnableServerStats />
        ) : (
          <>
            {all.length > 5 && (
              <div className="modal-toolbar">
                <input className="checks-search modal-search" placeholder={t('Фильтр: имя репозитория…')}
                  value={q} onChange={(e) => setQ(e.target.value)} />
                <select className="modal-sort" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
                  <option value="problems">{t('сорт: проблемные')}</option>
                  <option value="name">{t('сорт: имя')}</option>
                  <option value="size">{t('сорт: размер')}</option>
                  <option value="age">{t('сорт: старые')}</option>
                  <option value="snapshots">{t('сорт: снапшоты')}</option>
                </select>
              </div>
            )}
            <div className="loc-results docker-clist docker-clist-scroll">
              {repos.length === 0 ? (
                <div className="muted small">{t('Ничего не найдено.')}</div>
              ) : (
                repos.map((r) => (
                  <RepoRow key={r.name} server={s} r={r} muted={muted} canAct={canAct} onChanged={onChanged} />
                ))
              )}
            </div>
          </>
        )}
        {canAct && all.length > 0 && <BulkCleanup server={s} all={all} muted={muted} />}
        <CoverageAudit server={s} canManage={canAct} onChanged={onChanged} />
        <div className="muted small">
          {t('Читается на сервере без паролей: config (валидность), снапшоты, размер, свежесть, лок, политика хранения. Устаревшие (давно нет бэкапа) — красным и алертят; приглушите разовые/неактуальные (🔕).')}
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
  const repos = info.repos ?? []
  const muted = new Set(s.backup_repo_mutes ?? [])
  const bad = repos.filter((r) => repoBad(r, muted)).length
  // и покрытие тоже: бэкап-сервер в списке клиентов не рендерится, так что его
  // заглушённые пункты покрытия иначе оказались бы только в сводке
  const repoMutes = collectMutes(s, t, ['repo', 'audit'])
  return (
    <button className="check-row srv-row docker-host-row" onClick={onOpen}>
      <span className={`sdot ${s.online ? 'sdot-up' : 'sdot-down'}`} />
      <div className="check-main">
        <div className="check-name">
          🗄 <OsIcon os={s.os} /> <CountryFlag code={s.country} /> {s.name}
          <span className="type-chip">rest-server {info.version || '—'}</span>
          {repoMutes.length > 0 && <MuteChip items={repoMutes} t={t} />}
          {!info.running && <span className="type-chip off">{t('остановлен')}</span>}
          {!!info.version && verLt(info.version, RESTSRV_TARGET) && (
            <span className="type-chip upd" title={t('доступно обновление до {v}', { v: RESTSRV_TARGET })}>
              ⬆ {RESTSRV_TARGET}
            </span>
          )}
        </div>
        <div className="check-target mono muted small">
          {t('репозиториев: {n}', { n: repos.length })}
          {bad > 0 ? ` · ${t('проблемных: {b}', { b: bad })}` : ''}
        </div>
      </div>
      <div className="docker-host-count mono">
        <span className={bad > 0 ? 't-down' : 't-up'}>
          {bad > 0 ? `${bad} ⚠` : `${repos.length}`}
        </span>
      </div>
    </button>
  )
}

// строка сервера без настроенного бэкапа: кнопка «Настроить бэкап» + чекбокс «не требуется»
// Нода без бэкапа (или с галкой «не требуется») своей модалки не имеет, но находки
// аудита у неё есть — показываем их отдельной лёгкой модалкой, чтобы пункт с главной
// вёл во что-то осмысленное, а не в пустоту.
function NodeCoverageModal({ server: s, canManage, onChanged, onClose }: {
  server: Server; canManage: boolean; onChanged: () => void; onClose: () => void
}) {
  const { t } = useI18n()
  return createPortal(
    <div className="modal-backdrop">
      <div className="card modal backup-manage-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3><CountryFlag code={s.country} /> {s.name}</h3>
          <button className="ghost" onClick={onClose}>{t('Закрыть')}</button>
        </div>
        <div className="muted small">
          {s.backup_not_required
            ? t('Бэкап на этой ноде помечен как не требующийся, но данные на ней есть:')
            : t('Файловый бэкап на ноде не настроен. Что на ней обнаружено:')}
        </div>
        <CoverageAudit server={s} canManage={canManage} onChanged={onChanged} />
      </div>
    </div>,
    document.body,
  )
}

function NoBackupRow({ server: s, canAct, onSetup, onDeploy, onCoverage, onChanged }: {
  server: Server; canAct: boolean; onSetup: () => void; onDeploy: () => void
  onCoverage?: () => void; onChanged: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const optedOut = s.backup_not_required
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
    <div className={`loc-res docker-row ${optedOut ? 't-muted' : 't-down'}`}>
      <div className="docker-c-main">
        <div className="docker-c-name">
          <CountryFlag code={s.country} />
          {s.name}
          {s.group_name && <span className="type-chip group-chip">{s.group_name}</span>}
          {optedOut && <span className="type-chip">{t('не требуется')}</span>}
        </div>
        <div className="check-target mono muted small">{serverAddr(s)}</div>
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
  const [bsId, setBsId] = useState<number>(backupServers[0]?.id ?? 0)
  const [mode, setMode] = useState<'exclude' | 'include'>('exclude')
  const [paths, setPaths] = useState(DEFAULT_EXCLUDES.join('\n'))
  const [schedule, setSchedule] = useState('23:00')
  const [tls, setTls] = useState(true)
  const [ret, setRet] = useState({ last: 3, daily: 7, weekly: 4, monthly: 6 })
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
    setBusy(true)
    try {
      const j = await backupSetup(s.id, {
        backup_server_id: bsId, mode, paths: list, schedule, tls,
        keep_last: ret.last, keep_daily: ret.daily, keep_weekly: ret.weekly, keep_monthly: ret.monthly,
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
              <button disabled={busy} onClick={run}>{busy ? '…' : t('▶ Настроить и запустить')}</button>
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

export function BackupsPage({
  onUnauthorized,
  openHostId = null,
  openSrvHostId = null,
  onConsumed,
  onSrvConsumed,
}: {
  onUnauthorized: () => void
  openHostId?: number | null
  openSrvHostId?: number | null
  onConsumed?: () => void
  onSrvConsumed?: () => void
}) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [servers, setServers] = useState<Server[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [openId, setOpenId] = useState<number | null>(openHostId)
  const [openSrvId, setOpenSrvId] = useState<number | null>(openSrvHostId)
  const [setupSrv, setSetupSrv] = useState<Server | null>(null)
  const [deploySrv, setDeploySrv] = useState<Server | null>(null)
  const [covSrv, setCovSrv] = useState<Server | null>(null)
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
      onConsumed?.()
    }
  }, [openHostId, onConsumed])
  useEffect(() => {
    if (openSrvHostId != null) {
      setOpenSrvId(openSrvHostId)
      onSrvConsumed?.()
    }
  }, [openSrvHostId, onSrvConsumed])
  const [query, setQuery] = useState('')
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

  const allHosts = (servers ?? [])
    .map((s) => ({ s, d: s.last_report?.backup }))
    // клиент = ЖИВОЙ бэкап (см. backupAlive). Мёртвый бэкап (снесли restic+таймер,
    // остался лишь осиротевший метрик) — НЕ клиент, уходит в «Без бэкапа».
    .filter((x): x is BHost =>
      backupAlive(x.d) && !x.s.last_report?.backup_server?.present,
    )
  // заглушённое по бэкапам: репозитории (у бэкап-серверов) и пункты покрытия (у клиентов)
  const backupMuted = (servers ?? [])
    .map((s) => ({ s, items: collectMutes(s, t, ['repo', 'audit']) }))
    .filter((x) => x.items.length > 0)
  // сервера БЕЗ живого бэкапа (по умолчанию бэкап нужен → алерт). Бэкап-серверы и
  // живые клиенты сюда не попадают; мёртвый/осиротевший бэкап — попадает (можно настроить заново).
  const noBackupAll = (servers ?? []).filter(
    (s) =>
      s.online &&
      !s.last_report?.backup_server?.present &&
      !backupAlive(s.last_report?.backup),
  )
  // требуют бэкапа (галка не стоит) — наверх, красным; «не требуется» — в отдельный подвал внизу
  const noBackupReq = noBackupAll.filter((s) => !s.backup_not_required).sort((a, b) => a.name.localeCompare(b.name))
  const noBackupOpt = noBackupAll.filter((s) => s.backup_not_required).sort((a, b) => a.name.localeCompare(b.name))
  const problems = allHosts.filter(({ d }) => {
    const st = backupStatus(d)
    return st === 'failed' || st === 'stale'
  }).length
  const q = query.trim().toLowerCase()
  // Поиск обязан действовать на ВСЮ страницу. Раньше он сужал только клиентов, а
  // бэкап-серверы и «не требуется» оставались как есть — при активном фильтре это
  // читалось как «ничего не бэкапится», хотя ноды просто не подходили под запрос.
  const match = (s: Server) =>
    !q || [s.name, s.group_name].join(' ').toLowerCase().includes(q)
  const hosts = allHosts
    .filter(({ s }) => match(s))
    .sort((a, b) => a.s.name.localeCompare(b.s.name))
  const srvHostsView = srvHosts.filter(({ s }) => match(s))
  const noBackupReqView = noBackupReq.filter(match)
  const noBackupOptView = noBackupOpt.filter(match)

  const groups: { key: string; label: string; items: BHost[] }[] =
    groupBy === 'none'
      ? [{ key: 'all', label: '', items: hosts }]
      : (() => {
          const map = new Map<string, BHost[]>()
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
  useEffect(() => {
    if (openId == null || open) return
    const srv = (servers ?? []).find((x) => x.id === openId)
    if (!srv) return
    if (srv.last_report?.backup_server?.present) setOpenSrvId(openId)
    else if ((srv.backup_audit?.length ?? 0) > 0) setCovSrv(srv)
    setOpenId(null)
  }, [openId, open, servers])

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
          {allHosts.length > 0 && (
            <span className="muted small">
              {problems > 0
                ? t('{n} нод · {p} проблем', { n: allHosts.length, p: problems })
                : t('{n} нод · все свежие', { n: allHosts.length })}
            </span>
          )}
        </div>
      </div>
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
            placeholder={t('Поиск: нода, группа…')}
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
      {err && <p className="form-error">{err}</p>}
      {srvHostsView.length > 0 && (
        <div className="mon-group">
          <div className="mon-group-head docker-group-head">
            <span className="mon-group-name">🗄 {t('Бэкап-серверы')}</span>
            <span className="mon-group-n">{srvHostsView.length}</span>
          </div>
          <div className="check-list">
            {srvHostsView.map(({ s, d }) => (
              <BackupServerRow key={s.id} server={s} info={d} onOpen={() => setOpenSrvId(s.id)} />
            ))}
          </div>
        </div>
      )}
      {noBackupReqView.length > 0 && (
        <div className="mon-group">
          <div className="mon-group-head docker-group-head">
            <span className="mon-group-name">⚠️ {t('Без бэкапа')}</span>
            <span className="mon-group-n">{noBackupReqView.length}</span>
          </div>
          <div className="check-list">
            {noBackupReqView.map((s) => (
              <NoBackupRow key={s.id} server={s} canAct={!isViewer}
                onSetup={() => setSetupSrv(s)} onDeploy={() => setDeploySrv(s)} onChanged={load} />
            ))}
          </div>
        </div>
      )}
      {srvHostsView.length + noBackupReqView.length + noBackupOptView.length > 0 && hosts.length > 0 && (
        <div className="mon-group-head docker-group-head backup-clients-head">
          <span className="mon-group-name">{t('Клиенты (что бэкапится)')}</span>
        </div>
      )}
      {servers && allHosts.length === 0 && srvHosts.length === 0 && noBackupAll.length === 0 && (
        <div className="card muted">
          {t('Ноды с restic-бэкапом не найдены. Агент определяет бэкап сам (метрики + systemd); если бэкап есть, но раздел пуст — обновите агент.')}
        </div>
      )}
      {servers && allHosts.length > 0 && hosts.length === 0 && (
        <div className="card muted">
          {q
            ? t('По запросу «{q}» ничего нет — это фильтр, а не отсутствие бэкапов.', { q: query.trim() })
            : t('Ничего не найдено.')}
          {q && (
            <button className="ghost search-clear" onClick={() => setQuery('')}>
              {t('Сбросить')}
            </button>
          )}
        </div>
      )}
      {groups.map((g) => (
        <div key={g.key} className="mon-group">
          {groupBy !== 'none' && (
            <div className="mon-group-head docker-group-head">
              <span className="mon-group-name">{g.label}</span>
              <span className="mon-group-n">{g.items.length}</span>
            </div>
          )}
          <div className="check-list">
            {g.items.map(({ s, d }) => (
              <HostRow
                key={s.id}
                server={s}
                backup={d}
                showGroup={groupBy !== 'group'}
                dest={backupDestName(d.repo_dest, servers ?? [], s)}
                onOpen={() => setOpenId(s.id)}
              />
            ))}
          </div>
        </div>
      ))}
      {/* «не требуется» — подвал: это не проблема, а осознанно снятые ноды. Отделено
          чертой, свёрнуто по умолчанию, состояние помним между заходами. */}
      {noBackupOptView.length > 0 && (
        <div className="mon-group nobackup-optout backup-optout-footer">
          <div className="mon-group-head docker-group-head">
            <span className="mon-group-name muted">{t('Бэкап не требуется')}</span>
            <span className="mon-group-n">{noBackupOptView.length}</span>
            <button className="ghost optout-toggle" onClick={() => setShowOptOut(!showOptOut)}>
              {showOptOut ? t('скрыть') : t('показать')}
            </button>
          </div>
          {showOptOut && (
            <div className="check-list">
              {noBackupOptView.map((s) => (
                <NoBackupRow key={s.id} server={s} canAct={!isViewer}
                  onSetup={() => setSetupSrv(s)} onDeploy={() => setDeploySrv(s)}
                  onCoverage={(s.backup_audit?.length ?? 0) > 0 ? () => setCovSrv(s) : undefined}
                  onChanged={load} />
              ))}
            </div>
          )}
        </div>
      )}
      {/* Сейф доступов — в подвале, рядом со справочными блоками: это не ежедневная
          работа, а страховка на день восстановления. Виден только админам. */}
      {!isViewer && <VaultPanel servers={servers ?? []} />}
      {/* Заглушённое ПО БЭКАПАМ — в подвале: это осознанно снятые напоминания
          (репозитории и пункты покрытия), справка, а не работа. Наверху они отжимали
          вниз то, ради чего страницу и открывают. Клик открывает нужную модалку:
          у бэкап-сервера свою, у клиента — свою. */}
      <MutesBanner
        entries={backupMuted.map(({ s, items }) => ({ id: s.id, name: s.name, items }))}
        t={t}
        onOpen={(id) => {
          const s = (servers ?? []).find((x) => x.id === id)
          if (s?.last_report?.backup_server?.present) setOpenSrvId(id)
          else setOpenId(id)
        }}
      />
      {open && (
        <BackupModal
          server={open.s}
          backup={open.d}
          repo={backupRepoStat(open.d.repo_dest, servers ?? [], open.s)}
          canManage={!isViewer}
          onChanged={load}
          onClose={() => setOpenId(null)}
        />
      )}
      {openSrv && (
        <BackupServerModal
          server={openSrv.s}
          info={openSrv.d}
          canAct={!isViewer}
          onChanged={load}
          onClose={() => setOpenSrvId(null)}
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
          onChanged={load} onClose={() => setCovSrv(null)} />
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
