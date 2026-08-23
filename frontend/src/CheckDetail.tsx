import { useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  checkHistory,
  checkLocations,
  checkLog,
  checkToForm,
  checkUptime,
  listIncidents,
  snoozeCheck,
  updateCheck,
  type Check,
  type CheckForm,
  type CheckSample,
  type Incident,
  type LocationResult,
  type Uptime,
} from './api'
import { CheckFormCard } from './CheckFormCard'
import { SnoozeControl } from './SnoozeControl'
import { StackedAreaChart } from './charts/StackedAreaChart'
import { StatusBar } from './charts/StatusBar'
import { useI18n } from './i18n'
import { expiryText } from './checkUtils'
import { useAuth } from './auth'

type Props = {
  check: Check
  groups?: string[]
  onClose: () => void
  onSaved?: () => void
  onRun?: () => void
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
  // локация, чью тайм-серию показываем на графике (null = основная/прямая)
  const [selectedLoc, setSelectedLoc] = useState<number | null>(null)
  const [selectedIp, setSelectedIp] = useState<string | null>(null) // график по IP
  const [zoomOpen, setZoomOpen] = useState(false)
  const [err, setErr] = useState<string | null>(null)

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
  }, [check.id, check.check_locations, editing, logFailed, fail])

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
        .then((h) => setPoints(h.points))
        .catch(fail)
    run()
    if (editing) return
    const id = window.setInterval(run, 12000)
    return () => window.clearInterval(id)
  }, [check.id, hours, selectedLoc, selDirect, selectedIp, editing, fail])

  const isCert = check.type === 'cert'
  const chartTs = (points ?? []).map((p) => new Date(p.ts).getTime())
  const chartSeries = [
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

  const segments = (points ?? []).map((p) => ({
    status: p.status,
    title: `${fmtDateTime(p.ts)} · ${p.status}`,
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
              {{ http: 'HTTP', tcp_port: 'TCP', cert: 'TLS' }[check.type]}
            </span>
          </div>
          <div className="detail-head-actions">
            {!editing && !isViewer && onRun && (
              <button className="ghost icon-btn" onClick={onRun} title={t('Проверить сейчас')}>
                ▶
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
        <div className="detail-target">
          {check.target}
          {check.port ? `:${check.port}` : ''}
        </div>

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
          ) : (
            <div className="inc-list">
              {incidents.map((inc) => (
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
          ) : (
            <div className="log-list">
              {logRows.map((s, i) => (
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
        </div>
          </>
        )}
      </div>
    </div>
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
  const series = [
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
                  title: `${fmtDateTime(p.ts)} · ${p.status}`,
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
