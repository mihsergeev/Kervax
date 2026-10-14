import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  checkHistory,
  checkLocations,
  checkLog,
  checkToForm,
  checkUptime,
  listIncidents,
  acceptDns,
  dropCheckKnown,
  rotatePushToken,
  setCheckKnown,
  setCheckNote,
  runCheck,
  runCheckResult,
  snoozeCheck,
  updateCheck,
  type Check,
  type CheckForm,
  type CheckRun,
  type CheckSample,
  type CheckStatus,
  type Incident,
  type LocationResult,
  type Uptime,
} from './api'
import { CheckFormCard } from './CheckFormCard'
import { CheckWhere, phaseSeries } from './CheckPhases'
import { SnoozeControl } from './SnoozeControl'
import { StackedAreaChart } from './charts/StackedAreaChart'
import { StatusBar, type BarStatus } from './charts/StatusBar'
import { useI18n } from './i18n'
import { expiryText } from './checkUtils'
import { useAuth } from './auth'
import { NoteBar } from './NoteBar'
import { CheckParentBar } from './ParentBar'
import { KnownCheckBar, KnownDialog } from './KnownDialog'
import { MaintBanner } from './MaintModal'

type Props = {
  check: Check
  groups?: string[]
  onClose: () => void
  onSaved?: () => void
  onRun?: () => void // ручная проверка закончилась — обновить список
  onDelete?: () => void
  onUnauthorized: () => void
}

const WINDOWS: { key: string; label: string; hours: number }[] = [
  { key: '24h', label: '24ч', hours: 24 },
  { key: '7d', label: '7д', hours: 168 },
  { key: '30d', label: '30д', hours: 720 },
]

function fmtDateTime(ts: string): string {
  return new Date(ts).toLocaleString([], {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function tone(u: number | null): string {
  if (u == null) return ''
  return u >= 99 ? 't-up' : u >= 95 ? 't-degraded' : 't-down'
}

// Состояние ручной проверки: идёт → готово (вердикт) или «проверить не удалось».
type RunState =
  | {
      phase: 'running'
      source: 'panel' | 'agent'
      server: string
      fast: boolean
      pending: number | null // номер запроса к агенту, когда ответ ещё в пути
      since: number
    }
  | { phase: 'done'; result: CheckRun }
  | { phase: 'failed'; message: string }

// Бины окна целиком: пропуски — «нет данных», а не соседние бины, растянутые на всю
// ширину. У свежего монитора было два бина на всю ленту, и один сбой из двадцати
// проверок выглядел как «полсуток лежал» — рядом с лентой в списке, где тот же сбой
// занимал одну клетку из двадцати четырёх.
function fillWindow(
  points: CheckSample[],
  fromMs: number,
  toMs: number,
  stepSec: number,
): { ts: number; p: CheckSample | null }[] {
  const plain = points.map((p) => ({ ts: new Date(p.ts).getTime(), p }))
  if (!stepSec || points.length === 0) return plain
  const stepMs = stepSec * 1000
  const first = Math.floor(fromMs / stepMs) * stepMs
  const last = Math.floor(toMs / stepMs) * stepMs
  const n = (last - first) / stepMs + 1
  if (n <= 0 || n > 2000) return plain
  const byBin = new Map<number, CheckSample>()
  for (const x of plain) byBin.set(Math.floor(x.ts / stepMs) * stepMs, x.p)
  const out: { ts: number; p: CheckSample | null }[] = []
  for (let b = first; b <= last; b += stepMs) out.push({ ts: b, p: byBin.get(b) ?? null })
  return out
}

/** Метка «алерта не было» с объяснением, чего именно не хватило. */
function NoAlertChip({ inc, t }: { inc: Incident; t: (k: string, p?: Record<string, string | number>) => string }) {
  const n = inc.alert_after ?? 0
  const iv = inc.interval_seconds ?? 0
  const mins = n && iv ? Math.round((n * iv) / 60) : 0
  const hint = n
    ? t('Алерт уходит после {n} неудачных проверок подряд — это около {m} мин непрерывного сбоя. Этот инцидент закончился раньше. Порог меняется в настройках монитора.', { n, m: mins })
    : t('По этому инциденту алерт не отправлялся.')
  return <span className="type-chip inc-noalert" title={hint}>{t('без алерта')}</span>
}

export function CheckDetail({ check, groups, onClose, onSaved, onRun, onDelete, onUnauthorized }: Props) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [hours, setHours] = useState(24)
  const [uptime, setUptime] = useState<Uptime | null>(null)
  const [points, setPoints] = useState<CheckSample[] | null>(null)
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [locs, setLocs] = useState<LocationResult[]>([])
  const [logRows, setLogRows] = useState<CheckSample[]>([])
  const [logFailed, setLogFailed] = useState(true) // по умолчанию только сбои
  // Старое разворачивают руками. Открытая карточка показывает выбранное окно —
  // иначе первым же экраном встречает сбой недельной давности, и кажется, что
  // с сайтом что-то не так прямо сейчас.
  const [oldInc, setOldInc] = useState(false)
  const [oldLog, setOldLog] = useState(false)
  // локация, чью тайм-серию показываем на графике (null = основная/прямая)
  const [selectedLoc, setSelectedLoc] = useState<number | null>(null)
  const [selectedIp, setSelectedIp] = useState<string | null>(null) // график по IP
  const [zoomOpen, setZoomOpen] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [step, setStep] = useState(0) // ширина бина истории, сек
  // ручная проверка и её вердикт; refresh — перечитать карточку сразу после неё,
  // не дожидаясь очередного автообновления
  const [run, setRun] = useState<RunState | null>(null)
  const [refresh, setRefresh] = useState(0)
  const [knownOpen, setKnownOpen] = useState(false) // отметить сбой известной проблемой

  // редактирование прямо в модалке
  const [editing, setEditing] = useState(false)
  const [snoozing, setSnoozing] = useState(false)
  const [form, setForm] = useState<CheckForm | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)

  const startEdit = () => {
    setForm(checkToForm(check))
    setSaveErr(null)
    setEditing(true)
  }
  const setField = (patch: Partial<CheckForm>) =>
    setForm((f) => (f ? { ...f, ...patch } : f))
  const canSave =
    !!form && form.name.trim() !== '' && form.target.trim() !== '' && !saving

  const submitEdit = async () => {
    if (!form) return
    setSaving(true)
    setSaveErr(null)
    try {
      await updateCheck(check.id, form)
      setEditing(false)
      onSaved?.()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setSaveErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setSaving(false)
    }
  }

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    },
    [onUnauthorized, t],
  )

  // «Проверить сейчас»: человек жмёт кнопку, когда что-то поменял или сомневается, и
  // ответ должен быть виден сразу и однозначно — работает сайт или нет и почему.
  // Раньше результат молча записывался в журнал: кнопку жали двадцать раз подряд.
  const running = run?.phase === 'running'
  const finishRun = (r: CheckRun) => {
    setRun(r.run_error ? { phase: 'failed', message: r.run_error } : { phase: 'done', result: r })
    setRefresh((x) => x + 1)
    onRun?.()
  }
  // опрос ответа агента живёт в эффекте, а колбэки меняются на каждой отрисовке
  const runCb = useRef({ finishRun, onUnauthorized })
  runCb.current = { finishRun, onUnauthorized }

  const startRun = async () => {
    if (running) return
    const local = check.probe_local
    setRun({
      phase: 'running', source: local ? 'agent' : 'panel',
      server: check.probe_server_name ?? '', fast: false, pending: null, since: Date.now(),
    })
    try {
      const r = await runCheck(check.id)
      if (r.run_pending != null) {
        setRun({
          phase: 'running', source: 'agent', server: r.run_server, fast: r.run_fast,
          pending: r.run_pending, since: Date.now(),
        })
      } else {
        finishRun(r)
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setRun({ phase: 'failed', message: e instanceof Error ? e.message : t('Ошибка') })
    }
  }

  const pendingId = run?.phase === 'running' ? run.pending : null
  useEffect(() => {
    if (pendingId == null) return
    let stop = false
    const started = Date.now()
    let timer = 0
    const poll = async () => {
      if (stop) return
      try {
        const r = await runCheckResult(check.id, pendingId)
        if (stop) return
        if (r.run_pending == null) return runCb.current.finishRun(r)
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return runCb.current.onUnauthorized()
        // сеть моргнула — спрашиваем дальше, срок ожидания считает панель
      }
      if (Date.now() - started > 150_000) {
        setRun({ phase: 'failed', message: t('Ответ проверки так и не пришёл.') })
        return
      }
      timer = window.setTimeout(poll, 1000)
    }
    timer = window.setTimeout(poll, 700)
    return () => {
      stop = true
      window.clearTimeout(timer)
    }
  }, [pendingId, check.id, t])

  useEffect(() => setRun(null), [check.id])
  const closeRun = useCallback(() => setRun(null), [])

  // статусы/аптайм/инциденты/локации — с автообновлением (пока не редактируем)
  useEffect(() => {
    if (editing) return
    const run = () => {
      checkUptime(check.id).then(setUptime).catch(fail)
      listIncidents(50, check.id).then(setIncidents).catch(fail)
      checkLog(check.id, logFailed, 100).then(setLogRows).catch(fail)
      if (check.check_locations)
        checkLocations(check.id)
          .then((rows) => {
            setLocs(rows)
            setSelectedLoc((cur) =>
              cur ?? rows.find((r) => r.direct)?.location_id ?? rows[0]?.location_id ?? null,
            )
          })
          .catch(fail)
    }
    run()
    const id = window.setInterval(run, 12000)
    return () => window.clearInterval(id)
  }, [check.id, check.check_locations, editing, logFailed, fail, refresh])

  useEffect(() => {
    setOldInc(false)
    setOldLog(false)
  }, [hours, check.id])

  // Граница «свежего»: то же окно, что у графика и полосы статусов. Один
  // переключатель на всю карточку — переключил на 7д, увидел и историю сбоев.
  const cutoff = Date.now() - hours * 3600_000
  const winLabel = t(WINDOWS.find((w) => w.hours === hours)?.label ?? '24ч')
  // Незакрытый инцидент — всегда свежий, даже если начался месяц назад: он идёт СЕЙЧАС.
  const incFresh = incidents.filter(
    (i) => !i.ended_at || new Date(i.ended_at).getTime() >= cutoff,
  )
  const incHidden = incidents.length - incFresh.length
  const incShown = oldInc ? incidents : incFresh
  const logFresh = logRows.filter((r) => new Date(r.ts).getTime() >= cutoff)
  const logHidden = logRows.length - logFresh.length
  const logShown = oldLog ? logRows : logFresh

  // выбранная локация: если она прямая — бэкенд отдаст основную серию
  const selLoc = locs.find((l) => l.location_id === selectedLoc)
  const selDirect = selLoc?.direct ?? true
  const selName = selLoc?.name

  useEffect(() => {
    setPoints(null) // сброс только при смене окна/локации/IP — на автообновлении без мигания
    const run = () =>
      checkHistory(
        check.id,
        hours,
        selectedIp ? undefined : selDirect ? undefined : selectedLoc ?? undefined,
        undefined,
        selectedIp ?? undefined,
      )
        .then((h) => {
          setPoints(h.points)
          setStep(h.step_seconds ?? 0)
        })
        .catch(fail)
    run()
    if (editing) return
    const id = window.setInterval(run, 12000)
    return () => window.clearInterval(id)
  }, [check.id, hours, selectedLoc, selDirect, selectedIp, editing, fail, refresh])

  const isCert = check.type === 'cert'
  const chartTs = (points ?? []).map((p) => new Date(p.ts).getTime())
  // основная серия http-монитора - по фазам (DNS, подключение, TLS, ожидание, загрузка)
  const phased = !isCert && !selectedIp && selDirect ? phaseSeries(points, t) : null
  const chartSeries = phased ?? [
    {
      name: isCert ? t('дн.') : t('мс'),
      color: isCert ? '#22c55e' : '#4b74ff',
      values: (points ?? []).map((p) => (isCert ? p.value : p.latency_ms)),
    },
  ]
  const fmtCY = (v: number) => `${Math.round(v)}`
  const fmtCV = (v: number) =>
    isCert ? `${Math.round(v)} ${t('дн.')}` : `${Math.round(v)} ${t('мс')}`
  const chartCap = isCert
    ? t('Осталось дней сертификата')
    : selectedIp
      ? t('Время ответа · IP {ip}', { ip: selectedIp })
      : check.check_locations && selName
        ? t('Время ответа · {loc}', { loc: selName })
        : t('Время ответа')
  const fmtX =
    hours <= 24
      ? (ms: number) =>
          new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : (ms: number) =>
          new Date(ms).toLocaleDateString([], { day: '2-digit', month: '2-digit' })

  const nowMs = Date.now()
  const segments: { status: BarStatus; title: string }[] =
    points == null
      ? []
      : fillWindow(points, nowMs - hours * 3600_000, nowMs, step).map(({ ts, p }) => ({
          status: p ? p.status : 'nodata',
          title: `${fmtDateTime(new Date(ts).toISOString())} · ${
            p ? statusWord(p.status, t) : t('нет данных')
          }`,
        }))

  return (
    <>
    <div className="modal-backdrop">
      <div className="card detail">
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot sdot-${check.last_status}`} />
            <h3>{check.name}</h3>
            <span className="type-chip">
              {{ http: 'HTTP', tcp_port: 'TCP', cert: 'TLS', push: 'PUSH', dns: 'DNS' }[check.type]}
            </span>
          </div>
          <div className="detail-head-actions">
            {!editing && !isViewer && onRun && (
              <button
                className="ghost icon-btn"
                onClick={startRun}
                disabled={running}
                title={t('Проверить сейчас')}
              >
                {running ? <span className="run-spin" /> : '▶'}
              </button>
            )}
            {!editing && !isViewer && (
              <button className="ghost icon-btn" onClick={startEdit} title={t('Изменить')}>
                ✎
              </button>
            )}
            {!editing && !isViewer && onDelete && (
              <button
                className="ghost icon-btn icon-btn-danger"
                onClick={onDelete}
                title={t('Удалить')}
              >
                🗑
              </button>
            )}
            <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
              ✕
            </button>
          </div>
        </div>
        {!editing && <MaintBanner kind="check" id={check.id} />}
        {!editing && (
          <NoteBar
            note={check.note}
            by={check.note_by}
            at={check.note_at}
            canEdit={!isViewer}
            onSave={async (text) => {
              try {
                await setCheckNote(check.id, text)
                onSaved?.()
              } catch (e) {
                if (e instanceof ApiError && e.status === 401) onUnauthorized()
                throw e
              }
            }}
          />
        )}
        {!editing && <CheckParentBar check={check} canEdit={!isViewer} onSaved={() => onSaved?.()} />}
        {!editing &&
          (check.known_issue ? (
            <KnownCheckBar
              known={check.known_issue}
              canEdit={!isViewer}
              onDrop={async () => {
                await dropCheckKnown(check.id)
                onSaved?.()
              }}
            />
          ) : !isViewer && (check.last_status === 'down' || check.last_status === 'degraded' || !!check.flaky_since) ? (
            <button className="ghost small note-add" onClick={() => setKnownOpen(true)}>
              📌 {t('отметить как известную проблему')}
            </button>
          ) : null)}
        {knownOpen && (
          <KnownDialog
            title={check.name}
            onClose={() => setKnownOpen(false)}
            onSave={async (text, days) => {
              await setCheckKnown(check.id, text, days)
              setKnownOpen(false)
              onSaved?.()
            }}
          />
        )}
        {check.type === 'push' ? (
          <PushInfo check={check} canEdit={!isViewer} />
        ) : check.type === 'dns' ? (
          <DnsInfo check={check} canEdit={!isViewer} onAccepted={() => onSaved?.()} />
        ) : (
          <div className="detail-target">
            {check.target}
            {check.port ? `:${check.port}` : ''}
          </div>
        )}

        {!editing && !isViewer && (
          <SnoozeControl
            snoozeUntil={check.snooze_until}
            busy={snoozing}
            onSnooze={async (hours) => {
              setSnoozing(true)
              try {
                await snoozeCheck(check.id, hours)
                onSaved?.()
              } catch (e) {
                fail(e)
              } finally {
                setSnoozing(false)
              }
            }}
          />
        )}

        {editing && form ? (
          <>
            {saveErr && <p className="form-error">{saveErr}</p>}
            <CheckFormCard
              bare
              form={form}
              set={setField}
              editing
              busy={saving}
              canSubmit={canSave}
              onSubmit={submitEdit}
              onCancel={() => setEditing(false)}
              groups={groups}
            />
          </>
        ) : (
          <>
        {err && <p className="form-error">{err}</p>}

        <div className="uptime-tiles">
          <UptimeTile label={t('Аптайм 24ч')} value={uptime?.day} />
          <UptimeTile label={t('Аптайм 7д')} value={uptime?.week} />
          <UptimeTile label={t('Аптайм 30д')} value={uptime?.month} />
        </div>

        {(check.check_ssl || check.check_domain) && (
          <div className="expiry-pills">
            {check.check_ssl && (
              <ExpiryPill
                icon="🔐"
                label={t('SSL-сертификат')}
                days={check.ssl_days}
                warn={check.ssl_warn_days}
                message={check.ssl_message}
              />
            )}
            {check.check_domain && (
              <ExpiryPill
                icon="🌐"
                label={t('Домен')}
                days={check.domain_days}
                warn={check.domain_warn_days}
                message={check.domain_message}
              />
            )}
          </div>
        )}

        {check.check_locations && (
          <div className="detail-locs">
            <div className="chart-cap">{t('Локации')}</div>
            <div className="loc-results">
              {locs.map((r) => (
                <button
                  key={r.location_id}
                  className={
                    'loc-res loc-res-btn' +
                    (r.location_id === selectedLoc ? ' loc-res-active' : '') +
                    (!r.enabled ? ' loc-res-off' : '')
                  }
                  onClick={() => {
                    setSelectedLoc(r.location_id)
                    setSelectedIp(null) // локация активна → график по локации
                  }}
                  title={t('проверено: {t}', { t: fmtDateTime(r.checked_at) })}
                >
                  <span className={`sdot sdot-${r.status}`} />
                  <div className="loc-res-name">
                    {r.name}
                    {r.direct && <span className="type-chip">{t('напрямую')}</span>}
                  </div>
                  <div className="loc-res-metric">
                    {r.latency_ms != null ? `${r.latency_ms} ${t('мс')}` : '—'}
                  </div>
                  <div className="loc-res-msg muted small" title={r.message}>
                    {r.message || '—'}
                  </div>
                </button>
              ))}
              {locs.length === 0 && (
                <div className="muted small">
                  {t('Ещё нет данных из локаций — проверка идёт в фоне.')}
                </div>
              )}
            </div>
          </div>
        )}

        {(check.extra_paths ?? []).length > 0 && (
          <div className="detail-locs">
            <div className="chart-cap">{t('Дополнительные пути')}</div>
            <div className="loc-results">
              {(check.last_path_results ?? []).map((r) => (
                <div key={r.path} className="loc-res">
                  <span className={`sdot sdot-${r.status === 'pending' || r.status === 'skipped' ? 'unknown' : r.status}`} />
                  <div className="loc-res-name mono">{r.path}</div>
                  <div className="loc-res-metric">
                    {r.latency_ms != null ? `${r.latency_ms} ${t('мс')}` : '—'}
                  </div>
                  <div className="loc-res-msg muted small" title={r.message}>
                    {r.message || '—'}
                  </div>
                </div>
              ))}
              {(check.last_path_results ?? []).length === 0 && (
                <div className="muted small">
                  {t('Пути ещё не проверялись: результат появится после ближайшей проверки.')}
                </div>
              )}
            </div>
          </div>
        )}

        {check.check_all_ips && (
          <div className="detail-locs">
            <div className="chart-cap">{t('IP-адреса домена')}</div>
            <div className="loc-results">
              {(check.last_ip_results ?? []).map((r) => (
                <button
                  key={r.ip}
                  className={
                    'loc-res loc-res-btn' + (r.ip === selectedIp ? ' loc-res-active' : '')
                  }
                  onClick={() =>
                    setSelectedIp((cur) => (cur === r.ip ? null : r.ip))
                  }
                  title={t('Показать время ответа этого IP')}
                >
                  <span className={`sdot sdot-${r.status}`} />
                  <div className="loc-res-name mono">{r.ip}</div>
                  <div className="loc-res-metric">
                    {r.latency_ms != null ? `${r.latency_ms} ${t('мс')}` : '—'}
                  </div>
                  <div className="loc-res-msg muted small" title={r.message}>
                    {r.message || '—'}
                  </div>
                </button>
              ))}
              {(check.last_ip_results ?? []).length === 0 && (
                <div className="muted small">
                  {t('Ещё нет разбивки по IP — проверка идёт в фоне (или у домена один адрес).')}
                </div>
              )}
            </div>
          </div>
        )}

        <div className="detail-toolbar">
          <div className="win-switch">
            {WINDOWS.map((w) => (
              <button
                key={w.key}
                className={`win-btn${hours === w.hours ? ' win-btn-active' : ''}`}
                onClick={() => setHours(w.hours)}
              >
                {t(w.label)}
              </button>
            ))}
          </div>
        </div>

        <StatusBar segments={segments} />

        <CheckWhere check={check} />

        <div className="chart-block chart-card">
          <button className="chart-cap chart-cap-btn" onClick={() => setZoomOpen(true)}>
            <span>{chartCap}</span>
            <span className="chart-expand" title={t('Открыть на весь экран')}>⤢</span>
          </button>
          {points == null ? (
            <div className="chart-empty">{t('загрузка…')}</div>
          ) : chartTs.length ? (
            <StackedAreaChart
              ts={chartTs}
              series={chartSeries}
              fmtY={fmtCY}
              fmtV={fmtCV}
              fmtTime={fmtX}
              height={150}
              onExpand={() => setZoomOpen(true)}
            />
          ) : (
            <div className="chart-empty">—</div>
          )}
        </div>

        <div className="detail-inc">
          <div className="chart-cap">{t('Инциденты')}</div>
          {incidents.length === 0 ? (
            <div className="muted small">{t('Инцидентов не было.')}</div>
          ) : incShown.length === 0 ? (
            <div className="muted small">
              {t('За {w} инцидентов не было.', { w: winLabel })}{' '}
              {t('последний — {t}', { t: fmtDateTime(incidents[0].started_at) })}
            </div>
          ) : (
            <div className="inc-list">
              {incShown.map((inc) => (
                <div key={inc.id} className="inc-item">
                  <span className={`sdot sdot-${inc.status}`} />
                  <div className="inc-when">
                    <div>{fmtDateTime(inc.started_at)}</div>
                    <div className="muted small">
                      {inc.ended_at
                        ? t('до {t}', { t: fmtDateTime(inc.ended_at) })
                        : t('идёт сейчас')}
                    </div>
                  </div>
                  <div className="inc-msg muted small" title={inc.last_message}>
                    {inc.last_message || '—'}
                  </div>
                  {/* «Сайт лежал, а алерта не было» — самый частый вопрос к панели.
                      Ответ должен стоять прямо у инцидента: короткий сбой не набирает
                      порог «N проверок подряд», и это видно без похода в базу. */}
                  {inc.notified === false && <NoAlertChip inc={inc} t={t} />}
                </div>
              ))}
            </div>
          )}
          {incHidden > 0 && (
            <button className="ghost small stale-more" onClick={() => setOldInc(!oldInc)}>
              {oldInc
                ? t('скрыть прежние')
                : t('показать прежние ({n})', { n: incHidden })}
            </button>
          )}
        </div>

        <div className="detail-log">
          <div className="log-head">
            <div className="chart-cap">{t('Журнал проверок')}</div>
            <div className="win-switch log-switch">
              <button
                className={`win-btn${logFailed ? ' win-btn-active' : ''}`}
                onClick={() => setLogFailed(true)}
              >
                {t('Только сбои')}
              </button>
              <button
                className={`win-btn${!logFailed ? ' win-btn-active' : ''}`}
                onClick={() => setLogFailed(false)}
              >
                {t('Все')}
              </button>
            </div>
          </div>
          {logRows.length === 0 ? (
            <div className="muted small">
              {logFailed ? t('Сбоев не было.') : t('Пока пусто.')}
            </div>
          ) : logShown.length === 0 ? (
            <div className="muted small">
              {logFailed
                ? t('За {w} сбоев не было.', { w: winLabel })
                : t('За {w} проверок нет.', { w: winLabel })}{' '}
              {logFailed &&
                t('последний — {t}', { t: fmtDateTime(logRows[0].ts) })}
            </div>
          ) : (
            <div className="log-list">
              {logShown.map((s, i) => (
                <div key={i} className="log-item">
                  <span className={`sdot sdot-${s.status}`} />
                  <span className="log-time muted small">{fmtDateTime(s.ts)}</span>
                  <span className="log-metric">
                    {s.latency_ms != null ? `${s.latency_ms} ${t('мс')}` : '—'}
                  </span>
                  <span className="log-msg mono small" title={s.message}>
                    {s.message || '—'}
                  </span>
                </div>
              ))}
            </div>
          )}
          {logHidden > 0 && (
            <button className="ghost small stale-more" onClick={() => setOldLog(!oldLog)}>
              {oldLog
                ? t('скрыть прежние')
                : t('показать прежние ({n})', { n: logHidden })}
            </button>
          )}
        </div>
          </>
        )}
      </div>
    </div>
    {run && <RunVerdict run={run} onClose={closeRun} />}
    {zoomOpen && (
      <CheckChartModal
        check={check}
        isCert={isCert}
        locationId={selDirect ? undefined : selectedLoc ?? undefined}
        locName={selName}
        onClose={() => setZoomOpen(false)}
      />
    )}
    </>
  )
}

function CheckChartModal({
  check,
  isCert,
  locationId,
  locName,
  onClose,
}: {
  check: Check
  isCert: boolean
  locationId?: number
  locName?: string
  onClose: () => void
}) {
  const { t } = useI18n()
  const [hours, setHours] = useState(24)
  const [zoom, setZoom] = useState<{ from: number; to: number } | null>(null)
  const [points, setPoints] = useState<CheckSample[] | null>(null)

  useEffect(() => {
    setPoints(null)
    const run = () =>
      checkHistory(check.id, hours, locationId, zoom ?? undefined)
        .then((h) => setPoints(h.points))
        .catch(() => {})
    run()
    if (zoom) return
    const id = window.setInterval(run, 15000)
    return () => window.clearInterval(id)
  }, [check.id, hours, locationId, zoom])

  const ts = (points ?? []).map((p) => new Date(p.ts).getTime())
  const phased = !isCert && !locationId ? phaseSeries(points, t) : null
  const series = phased ?? [
    {
      name: isCert ? t('дн.') : t('мс'),
      color: isCert ? '#22c55e' : '#4b74ff',
      values: (points ?? []).map((p) => (isCert ? p.value : p.latency_ms)),
    },
  ]
  const spanH = zoom ? (zoom.to - zoom.from) / 3600 : hours
  const fmtT =
    spanH <= 24
      ? (ms: number) =>
          new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : (ms: number) =>
          new Date(ms).toLocaleDateString([], { day: '2-digit', month: '2-digit' })
  const title = isCert
    ? t('Осталось дней сертификата')
    : locName
      ? t('Время ответа · {loc}', { loc: locName })
      : t('Время ответа')

  return (
    <div className="modal-backdrop chart-full-backdrop">
      <div className="card chart-full">
        <div className="detail-head">
          <div className="detail-title">
            <span className={`sdot sdot-${check.last_status}`} />
            <h3>{check.name}</h3>
            <span className="chart-full-metric">{title}</span>
          </div>
          <button className="ghost icon-btn" onClick={onClose} title={t('Закрыть')}>
            ✕
          </button>
        </div>
        <div className="srv-meta">
          <span className="srv-meta-item mono">
            {check.target}
            {check.port ? `:${check.port}` : ''}
          </span>
        </div>
        <div className="detail-toolbar chart-full-bar">
          <div className="win-switch">
            {WINDOWS.map((w) => (
              <button
                key={w.key}
                className={`win-btn${!zoom && hours === w.hours ? ' win-btn-active' : ''}`}
                onClick={() => {
                  setZoom(null)
                  setHours(w.hours)
                }}
              >
                {t(w.label)}
              </button>
            ))}
          </div>
          {zoom ? (
            <button className="ghost" onClick={() => setZoom(null)}>
              {t('Сбросить зум')} ✕
            </button>
          ) : (
            <span className="muted small">{t('Выделите участок мышью, чтобы приблизить')}</span>
          )}
        </div>
        {points == null ? (
          <div className="chart-empty">{t('загрузка…')}</div>
        ) : ts.length ? (
          <>
          {!isCert && (
            <div className="chart-full-status">
              <StatusBar
                segments={(points ?? []).map((p) => ({
                  status: p.status,
                  title: `${fmtDateTime(p.ts)} · ${statusWord(p.status, t)}`,
                }))}
              />
            </div>
          )}
          <StackedAreaChart
            ts={ts}
            series={series}
            fmtY={(v) => `${Math.round(v)}`}
            fmtV={(v) =>
              isCert ? `${Math.round(v)} ${t('дн.')}` : `${Math.round(v)} ${t('мс')}`
            }
            fmtTime={fmtT}
            height={420}
            onZoom={(f, to) => setZoom({ from: f / 1000, to: to / 1000 })}
          />
          </>
        ) : (
          <div className="chart-empty">{t('Нет данных за период')}</div>
        )}
      </div>
    </div>
  )
}

function statusWord(
  st: CheckStatus,
  t: (k: string, p?: Record<string, string | number>) => string,
): string {
  return st === 'up'
    ? t('работает')
    : st === 'degraded'
      ? t('деградация')
      : st === 'down'
        ? t('недоступен')
        : t('нет данных')
}

/** Всплывающий ответ на «Проверить сейчас»: крупно — работает или нет, ниже — почему
 *  и откуда проверяли. Успех сам уходит через несколько секунд; сбой и «не удалось
 *  проверить» висят, пока их не закроют: их надо прочитать. */
function RunVerdict({ run, onClose }: { run: RunState; onClose: () => void }) {
  const { t } = useI18n()
  const [, setTick] = useState(0)
  useEffect(() => {
    if (run.phase !== 'running') return
    const id = window.setInterval(() => setTick((x) => x + 1), 1000)
    return () => window.clearInterval(id)
  }, [run.phase])
  const autoHide = run.phase === 'done' && run.result.run_status === 'up'
  useEffect(() => {
    if (!autoHide) return
    const id = window.setTimeout(onClose, 8000)
    return () => window.clearTimeout(id)
  }, [autoHide, onClose, run])

  let tone: string
  let icon: React.ReactNode
  let title: string
  let text = ''
  let meta = ''
  if (run.phase === 'running') {
    tone = 'running'
    icon = <span className="run-spin run-spin-lg" />
    title = t('Проверяем…')
    const secs = Math.max(0, Math.round((Date.now() - run.since) / 1000))
    if (run.source === 'agent') {
      text = run.server
        ? t('Сайт проверяет агент изнутри сервера {srv}', { srv: run.server })
        : t('Сайт проверяет агент изнутри сервера')
      if (run.pending != null && !run.fast) meta = t('обычно это до 15 секунд')
    } else {
      text = t('Запрос к сайту идёт прямо сейчас')
    }
    if (secs >= 2) meta = meta ? `${meta} · ${secs} ${t('с')}` : `${secs} ${t('с')}`
  } else if (run.phase === 'failed') {
    tone = 'failed'
    icon = '!'
    title = t('Проверить не удалось')
    text = run.message
  } else {
    const r = run.result
    const st = r.run_status || r.last_status
    tone = st
    icon = st === 'up' ? '✓' : st === 'degraded' ? '!' : '✕'
    title =
      st === 'up' ? t('Работает') : st === 'degraded' ? t('Работает с замечаниями') : t('Не работает')
    text = r.run_message
    const where =
      r.run_source === 'agent'
        ? t('проверено изнутри сервера {srv}', { srv: r.run_server })
        : t('проверено из панели')
    const when = r.run_at
      ? new Date(r.run_at).toLocaleTimeString([], {
          hour: '2-digit', minute: '2-digit', second: '2-digit',
        })
      : ''
    meta = when ? `${where} · ${when}` : where
  }
  return (
    <div className={`run-verdict run-verdict-${tone}`} role="status" aria-live="polite">
      <span className="run-verdict-ic">{icon}</span>
      <div className="run-verdict-body">
        <div className="run-verdict-title">{title}</div>
        {text && <div className="run-verdict-text">{text}</div>}
        {meta && <div className="run-verdict-meta">{meta}</div>}
      </div>
      <button className="ghost icon-btn run-verdict-x" onClick={onClose} title={t('Закрыть')}>
        ✕
      </button>
    </div>
  )
}

function ExpiryPill({
  icon,
  label,
  days,
  warn,
  message,
}: {
  icon: string
  label: string
  days: number | null
  warn: number[]
  message: string
}) {
  const { t } = useI18n()
  const warnMax = warn.length ? Math.max(...warn) : 0
  const warnMin = warn.length ? Math.min(...warn) : 0
  const tn =
    days == null ? '' : days <= warnMin ? 't-down' : days <= warnMax ? 't-degraded' : 't-up'
  const text = days == null ? message || t('нет данных') : expiryText(days, t)
  return (
    <div className="expiry-pill" title={message}>
      <span className="expiry-ico">{icon}</span>
      <div className="expiry-body">
        <div className="expiry-lbl">{label}</div>
        <div className={`expiry-days ${tn}`}>{text}</div>
      </div>
    </div>
  )
}

function UptimeTile({ label, value }: { label: string; value: number | null | undefined }) {
  return (
    <div className="uptime-tile">
      <div className={`uptime-val ${tone(value ?? null)}`}>
        {value == null ? '—' : `${value}%`}
      </div>
      <div className="uptime-lbl">{label}</div>
    </div>
  )
}

// Адрес push-монитора и как его дергать: из крона после успешного прогона или всегда, с кодом выхода.
function PushInfo({ check, canEdit }: { check: Check; canEdit: boolean }) {
  const { t } = useI18n()
  const [token, setToken] = useState(check.push_token || '')
  const [busy, setBusy] = useState(false)
  const url = `${window.location.origin}/api/agent/push/${token}`
  const ok = `/path/to/backup.sh && curl -fsS -m 10 --retry 3 ${url} >/dev/null`
  const always = `/path/to/backup.sh; curl -fsS -m 10 --retry 3 "${url}?status=$?" >/dev/null`
  const rotate = async () => {
    if (!window.confirm(t('Выдать новый адрес? Старый перестанет приниматься сразу, скрипт надо будет поправить.'))) return
    setBusy(true)
    try {
      const c = await rotatePushToken(check.id)
      setToken(c.push_token || '')
    } finally {
      setBusy(false)
    }
  }
  const copy = (s: string) => navigator.clipboard?.writeText(s)
  const last = (
    <div className="muted small">
      {check.push_at
        ? t('Последний сигнал: {when}', { when: new Date(check.push_at).toLocaleString() })
        : t('Сигнала еще не было')}
      {check.push_fail ? ` · ${t('скрипт сообщил об ошибке: {msg}', { msg: check.push_fail })}` : ''}
    </div>
  )
  // токен - секрет, роль "только просмотр" его не получает
  if (!token) {
    return (
      <div className="push-info">
        {last}
        <div className="muted small">{t('Адрес для скрипта видят админ и редактор')}</div>
      </div>
    )
  }
  return (
    <div className="push-info">
      <div className="muted small">{t('Адрес для скрипта (секрет, не публикуйте):')}</div>
      <div className="agent-advice-cmd">
        <pre>{url}</pre>
        <button className="ghost" onClick={() => copy(url)}>{t('Копировать')}</button>
      </div>
      <div className="muted small">{t('Сигнал только после успешного прогона:')}</div>
      <div className="agent-advice-cmd">
        <pre>{ok}</pre>
        <button className="ghost" onClick={() => copy(ok)}>{t('Копировать')}</button>
      </div>
      <div className="muted small">{t('Сигнал всегда, с кодом выхода (ненулевой = ошибка, придет сразу):')}</div>
      <div className="agent-advice-cmd">
        <pre>{always}</pre>
        <button className="ghost" onClick={() => copy(always)}>{t('Копировать')}</button>
      </div>
      {last}
      {canEdit && (
        <button className="ghost" disabled={busy} onClick={rotate}>
          {t('Выдать новый адрес')}
        </button>
      )}
    </div>
  )
}

// DNS-монитор: что спрашиваем, что ждем или что запомнили, что ответили сейчас. Ответ сменился
// по плану (переехали) - "принять" делает его правильным, монитор зеленеет сразу.
function DnsInfo({ check, canEdit, onAccepted }: { check: Check; canEdit: boolean; onAccepted: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const expect = (check.dns_expect ?? '').split('\n').map((x) => x.trim()).filter(Boolean)
  const base = check.dns_baseline ?? []
  const last = check.dns_last ?? []
  const same = (a: string[], b: string[]) => [...a].sort().join('|') === [...b].sort().join('|')
  const changed = expect.length === 0 && base.length > 0 && last.length > 0 && !same(base, last)
  const accept = async () => {
    setBusy(true)
    setErr('')
    try {
      await acceptDns(check.id)
      onAccepted()
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="dns-info">
      <div className="detail-target">
        {check.target} <span className="type-chip">{check.dns_type || 'A'}</span>
        {check.dns_server && <span className="muted small"> @{check.dns_server}</span>}
      </div>
      <div className="muted small">
        {expect.length > 0
          ? `${check.dns_match === 'contains' ? t('ждем, что ответ содержит') : t('ждем ответ')}: ${expect.join(', ')}`
          : base.length > 0
            ? `${t('запомнили')}: ${base.join(', ')}`
            : t('ответа еще не было: первый запомним и будем следить за изменениями')}
      </div>
      {last.length > 0 && (
        <div className="small">
          {t('сейчас')}: <span className="mono">{last.join(', ')}</span>
        </div>
      )}
      {changed && canEdit && (
        <button className="ghost small" disabled={busy} onClick={accept}>
          {t('Принять новый ответ как правильный')}
        </button>
      )}
      {err && <div className="muted small">{err}</div>}
    </div>
  )
}
