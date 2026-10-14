import { useEffect, useState } from 'react'
import {
  ApiError,
  checksOverview,
  createMaint,
  deleteMaint,
  listMaint,
  listServers,
  updateMaint,
  type MaintIn,
  type MaintWindow,
} from './api'
import { useAuth } from './auth'
import { useI18n } from './i18n'
import { maintFor, maintReload, maintTime, useMaint } from './maintStore'

type T = (k: string, p?: Record<string, string | number>) => string
type Opt = { id: number; name: string }

const WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']

const pad = (n: number) => String(n).padStart(2, '0')

// datetime-local живет в часах браузера, API - в UTC
function toLocalInput(iso: string): string {
  const d = new Date(iso)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function schedText(w: MaintWindow, t: T): string {
  const s = new Date(w.starts_at)
  const e = new Date(w.ends_at)
  const hm = (d: Date) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const dm = (d: Date) => d.toLocaleDateString([], { day: '2-digit', month: '2-digit' })
  if (!w.repeat) return `${dm(s)} ${hm(s)} - ${s.toDateString() === e.toDateString() ? hm(e) : `${dm(e)} ${hm(e)}`}`
  const days = w.repeat === 'daily' ? t('каждый день') : w.weekdays.map((d) => t(WD[d])).join(', ')
  return `${days} ${hm(s)}-${hm(e)}`
}

function emptyForm(): MaintIn {
  const s = new Date(Date.now() + 3600_000)
  s.setMinutes(0, 0, 0)
  return {
    title: '',
    server_ids: [],
    check_ids: [],
    starts_at: s.toISOString(),
    ends_at: new Date(s.getTime() + 3600_000).toISOString(),
    repeat: '',
    weekdays: [],
    verify: true,
    note: '',
  }
}

// Окна обслуживания: плановые работы, пока алерты по выбранным серверам и мониторам молчат,
// после окна - сверка одним сообщением, все ли вернулось
export function MaintModal({ onClose, onUnauthorized }: { onClose: () => void; onUnauthorized: () => void }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [items, setItems] = useState<MaintWindow[] | null>(null)
  const [servers, setServers] = useState<Opt[]>([])
  const [checks, setChecks] = useState<Opt[]>([])
  const [form, setForm] = useState<MaintIn | null>(null)
  const [editId, setEditId] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const fail = (e: unknown) => {
    if (e instanceof ApiError && e.status === 401) return onUnauthorized()
    setErr(e instanceof Error ? e.message : t('Ошибка'))
  }
  const load = () => {
    listMaint().then(setItems).catch(fail)
  }
  useEffect(() => {
    load()
    listServers()
      .then((ss) => setServers(ss.map((s) => ({ id: s.id, name: s.name })).sort((a, b) => a.name.localeCompare(b.name))))
      .catch(() => {})
    checksOverview()
      .then((o) => setChecks(o.checks.map((c) => ({ id: c.id, name: c.name })).sort((a, b) => a.name.localeCompare(b.name))))
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const save = async () => {
    if (!form) return
    setBusy(true)
    setErr('')
    try {
      if (editId) await updateMaint(editId, form)
      else await createMaint(form)
      setForm(null)
      setEditId(null)
      load()
      maintReload()
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }
  const remove = async (w: MaintWindow) => {
    if (!window.confirm(t('Удалить окно "{name}"?', { name: w.title }))) return
    try {
      await deleteMaint(w.id)
      load()
      maintReload()
    } catch (e) {
      fail(e)
    }
  }
  const names = (ids: number[], opts: Opt[]) =>
    ids.map((i) => opts.find((o) => o.id === i)?.name ?? `#${i}`).join(', ')

  // идущие, потом ближайшие, потом прошедшие разовые
  const rank = (w: MaintWindow) => (w.active ? 0 : w.finished ? 2 : 1)
  const sorted = [...(items ?? [])].sort(
    (a, b) => rank(a) - rank(b) || (a.next_start ?? a.starts_at).localeCompare(b.next_start ?? b.starts_at),
  )

  return (
    <div className="modal-backdrop">
      <div className="card modal maint-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>🚧 {t('Окна обслуживания')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <p className="muted small">
          {t('Плановые работы: пока окно идет, алерты по выбранным серверам и мониторам молчат. То, что осталось сломанным, придет алертом после окна, а через 5 минут после конца - сверка одним сообщением, все ли вернулось в норму.')}
        </p>
        {err && <div className="form-error">{err}</div>}

        {form ? (
          <MaintForm
            form={form}
            setForm={setForm}
            servers={servers}
            checks={checks}
            busy={busy}
            editing={editId !== null}
            onSave={save}
            onCancel={() => {
              setForm(null)
              setEditId(null)
              setErr('')
            }}
          />
        ) : (
          !isViewer && (
            <button
              onClick={() => {
                setForm(emptyForm())
                setEditId(null)
              }}
            >
              + {t('Новое окно')}
            </button>
          )
        )}

        <div className="maint-list">
          {items !== null && sorted.length === 0 && <div className="muted small">{t('Окон пока нет.')}</div>}
          {sorted.map((w) => (
            <div key={w.id} className={`maint-row${w.finished ? ' maint-done' : ''}`}>
              <div className="maint-row-main">
                <div>
                  <b>{w.title}</b>{' '}
                  {w.active ? (
                    <span className="type-chip maint-live">{t('идет до {t}', { t: maintTime(w.cur_end) })}</span>
                  ) : w.finished ? (
                    <span className="type-chip off">{t('прошло')}</span>
                  ) : (
                    w.next_start && <span className="type-chip">{t('следующее {t}', { t: maintTime(w.next_start) })}</span>
                  )}
                </div>
                <div className="muted small">{schedText(w, t)}</div>
                <div className="muted small">
                  {[
                    w.server_ids.length ? `${t('серверы')}: ${names(w.server_ids, servers)}` : '',
                    w.check_ids.length ? `${t('мониторы')}: ${names(w.check_ids, checks)}` : '',
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </div>
                {w.note && <div className="small">{w.note}</div>}
              </div>
              {!isViewer && (
                <div className="maint-row-actions">
                  <button
                    className="ghost small"
                    onClick={() => {
                      setForm({
                        title: w.title,
                        server_ids: w.server_ids,
                        check_ids: w.check_ids,
                        starts_at: w.starts_at,
                        ends_at: w.ends_at,
                        repeat: w.repeat,
                        weekdays: w.weekdays,
                        verify: w.verify,
                        note: w.note,
                      })
                      setEditId(w.id)
                    }}
                  >
                    {t('Изменить')}
                  </button>
                  <button className="ghost small" onClick={() => remove(w)}>
                    {t('Удалить')}
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function MaintForm({
  form,
  setForm,
  servers,
  checks,
  busy,
  editing,
  onSave,
  onCancel,
}: {
  form: MaintIn
  setForm: (f: MaintIn) => void
  servers: Opt[]
  checks: Opt[]
  busy: boolean
  editing: boolean
  onSave: () => void
  onCancel: () => void
}) {
  const { t } = useI18n()
  const set = (p: Partial<MaintIn>) => setForm({ ...form, ...p })
  const setTime = (key: 'starts_at' | 'ends_at', v: string) => {
    if (!v) return
    const iso = new Date(v).toISOString()
    // сдвинули начало - конец едет следом, длительность та же
    if (key === 'starts_at') {
      const dur = Date.parse(form.ends_at) - Date.parse(form.starts_at)
      set({ starts_at: iso, ends_at: new Date(Date.parse(iso) + Math.max(dur, 60_000)).toISOString() })
    } else set({ ends_at: iso })
  }
  const ok = form.title.trim() !== '' && (form.server_ids.length > 0 || form.check_ids.length > 0) && !busy
  return (
    <div className="maint-form">
      <label className="field field-wide">
        <span>{t('Что делаем')}</span>
        <input
          value={form.title}
          maxLength={200}
          placeholder={t('напр. обновление ядра и перезагрузка')}
          onChange={(e) => set({ title: e.target.value })}
        />
      </label>
      <div className="maint-times">
        <label className="field">
          <span>{t('Начало')}</span>
          <input type="datetime-local" value={toLocalInput(form.starts_at)} onChange={(e) => setTime('starts_at', e.target.value)} />
        </label>
        <label className="field">
          <span>{t('Конец')}</span>
          <input type="datetime-local" value={toLocalInput(form.ends_at)} onChange={(e) => setTime('ends_at', e.target.value)} />
        </label>
        <label className="field">
          <span>{t('Повтор')}</span>
          <select value={form.repeat} onChange={(e) => set({ repeat: e.target.value as MaintIn['repeat'] })}>
            <option value="">{t('разово')}</option>
            <option value="daily">{t('каждый день')}</option>
            <option value="weekly">{t('по дням недели')}</option>
          </select>
        </label>
      </div>
      {form.repeat === 'weekly' && (
        <div className="maint-days">
          {WD.map((d, i) => (
            <label key={d} className="check-inline">
              <input
                type="checkbox"
                checked={form.weekdays.includes(i)}
                onChange={(e) =>
                  set({ weekdays: e.target.checked ? [...form.weekdays, i] : form.weekdays.filter((x) => x !== i) })
                }
              />
              {t(d)}
            </label>
          ))}
        </div>
      )}
      {form.repeat && (
        <div className="muted small">
          {t('Повторы - в то же время и той же длины, что и первое окно, по часовому поясу алертов.')}
        </div>
      )}
      <div className="maint-pick-row">
        <Picker title={t('Серверы')} opts={servers} value={form.server_ids} onChange={(v) => set({ server_ids: v })} />
        <Picker title={t('Мониторы')} opts={checks} value={form.check_ids} onChange={(v) => set({ check_ids: v })} />
      </div>
      <label className="check-inline">
        <input type="checkbox" checked={form.verify} onChange={(e) => set({ verify: e.target.checked })} />
        {t('Сверка после окна: прислать, все ли вернулось в норму')}
      </label>
      <label className="field field-wide">
        <span>{t('Заметка')}</span>
        <textarea rows={2} maxLength={1000} value={form.note} onChange={(e) => set({ note: e.target.value })} />
      </label>
      <div className="note-bar-actions">
        <button disabled={!ok} onClick={onSave}>
          {editing ? t('Сохранить') : t('Создать окно')}
        </button>
        <button className="ghost" disabled={busy} onClick={onCancel}>
          {t('Отмена')}
        </button>
      </div>
    </div>
  )
}

// Выбор серверов или мониторов: поиск и галки, выбранные сверху
function Picker({ title, opts, value, onChange }: { title: string; opts: Opt[]; value: number[]; onChange: (v: number[]) => void }) {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const sel = new Set(value)
  const shown = opts
    .filter((o) => !q || o.name.toLowerCase().includes(q.toLowerCase()))
    .sort((a, b) => Number(sel.has(b.id)) - Number(sel.has(a.id)))
  return (
    <div className="maint-pick">
      <div className="maint-pick-head">
        <span>{title}</span>
        <span className="muted small">{t('выбрано: {n}', { n: value.length })}</span>
      </div>
      <input value={q} placeholder={t('поиск')} onChange={(e) => setQ(e.target.value)} />
      <div className="maint-pick-list">
        {shown.map((o) => (
          <label key={o.id} className="check-inline">
            <input
              type="checkbox"
              checked={sel.has(o.id)}
              onChange={(e) => onChange(e.target.checked ? [...value, o.id] : value.filter((x) => x !== o.id))}
            />
            {o.name}
          </label>
        ))}
      </div>
    </div>
  )
}

// Плашка в карточке сервера или монитора: идет обслуживание, алерты молчат
export function MaintBanner({ kind, id }: { kind: 'server' | 'check'; id: number }) {
  const { t } = useI18n()
  const w = maintFor(useMaint(), kind, id)
  if (!w) return null
  return (
    <div className="note-bar maint-bar">
      <span className="note-bar-ic">🚧</span>
      <div className="note-bar-body">
        <div>
          <b>{t('Идет обслуживание')}:</b> {w.title} · {t('до {t}', { t: maintTime(w.cur_end) })}
        </div>
        <div className="muted small">{t('алерты молчат, то, что останется сломанным, придет после окна')}</div>
      </div>
    </div>
  )
}

// Значок в строке списка
export function MaintChip({ kind, id }: { kind: 'server' | 'check'; id: number }) {
  const { t } = useI18n()
  const w = maintFor(useMaint(), kind, id)
  if (!w) return null
  return (
    <span className="note-chip" title={`${t('Идет обслуживание')}: ${w.title} · ${t('до {t}', { t: maintTime(w.cur_end) })}`}>
      🚧
    </span>
  )
}

// Блок на главной: что обслуживается сейчас и что начнется в ближайшие 12 часов
export function MaintHome() {
  const { t } = useI18n()
  const ws = useMaint()
  const live = ws.filter((w) => w.active)
  const soon = ws
    .filter((w) => !w.active && w.next_start && Date.parse(w.next_start) - Date.now() < 12 * 3600_000)
    .sort((a, b) => (a.next_start ?? '').localeCompare(b.next_start ?? ''))
  if (!live.length && !soon.length) return null
  const scope = (w: MaintWindow) =>
    [
      w.server_ids.length ? t('серверов {n}', { n: w.server_ids.length }) : '',
      w.check_ids.length ? t('мониторов {n}', { n: w.check_ids.length }) : '',
    ]
      .filter(Boolean)
      .join(', ')
  return (
    <div className="chart-card net-card home-block">
      <div className="net-head">
        <div className="chart-cap home-cap">
          {live.length ? t('Идет обслуживание') : t('Скоро обслуживание')}
          <span className={`home-n${live.length ? ' home-n-warn' : ''}`}>{live.length || soon.length}</span>
        </div>
      </div>
      <div className="home-attention-list">
        {live.map((w) => (
          <div key={w.id} className="home-attention-row maint-home-row">
            <span className="lk-dot lk-dot-warn" />
            <span className="home-attention-txt">
              <b>{w.title}</b> - {t('до {t}', { t: maintTime(w.cur_end) })}
              <span className="muted"> · {scope(w)}</span>
            </span>
          </div>
        ))}
        {soon.map((w) => (
          <div key={w.id} className="home-attention-row maint-home-row muted">
            <span className="lk-dot" />
            <span className="home-attention-txt">
              {t('в {t}', { t: maintTime(w.next_start) })}: {w.title}
              <span> · {scope(w)}</span>
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
