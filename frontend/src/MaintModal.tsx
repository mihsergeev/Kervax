import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  checksOverview,
  createMaint,
  deleteMaint,
  listMaint,
  listServers,
  updateMaint,
  type CheckStatus,
  type MaintIn,
  type MaintWindow,
} from './api'
import { useAuth } from './auth'
import { CardNav, type SecDef } from './cardLayout'
import { useI18n } from './i18n'
import { maintFor, maintReload, maintTime, useMaint } from './maintStore'
import { NetTile } from './NetLinks'

// Окна обслуживания как карточка сервера: сводка (что идет, что скоро), у каждого окна свой
// раздел (что затронет и как оно сейчас, закончить или продлить, настройки), новое окно -
// только обязательное, прошедшие отдельно.

type T = (k: string, p?: Record<string, string | number>) => string
type Srv = { id: number; name: string; online: boolean; enabled: boolean }
type Chk = { id: number; name: string; status: CheckStatus; enabled: boolean }
type Props = {
  onClose: () => void
  onUnauthorized: () => void
  onOpen: (kind: 'server' | 'check', id: number) => void
}

const WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
const DUR: [number, string][] = [
  [30, '30 мин'],
  [60, '1 ч'],
  [120, '2 ч'],
  [240, '4 ч'],
  [480, '8 ч'],
]

const pad = (n: number) => String(n).padStart(2, '0')

// datetime-local живет в часах браузера, API - в UTC
function toLocalInput(iso: string): string {
  const d = new Date(iso)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function schedText(w: MaintIn, t: T): string {
  const s = new Date(w.starts_at)
  const e = new Date(w.ends_at)
  const hm = (d: Date) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const dm = (d: Date) => d.toLocaleDateString([], { day: '2-digit', month: '2-digit' })
  if (!w.repeat) return `${dm(s)} ${hm(s)} - ${s.toDateString() === e.toDateString() ? hm(e) : `${dm(e)} ${hm(e)}`}`
  const days = w.repeat === 'daily' ? t('каждый день') : w.weekdays.map((d) => t(WD[d])).join(', ')
  return `${days} ${hm(s)}-${hm(e)}`
}

// через сколько начнется: "через 3 ч"
function untilShort(iso: string, t: T): string {
  const sec = (Date.parse(iso) - Date.now()) / 1000
  if (!(sec > 60)) return t('в ближайшие минуты')
  if (sec < 3600) return t('через {n} мин', { n: Math.round(sec / 60) })
  if (sec < 48 * 3600) return t('через {n} ч', { n: Math.round(sec / 3600) })
  return t('через {n} дн', { n: Math.round(sec / 86400) })
}

// то же коротко, для меню и плитки: "12 ч"
function inShort(iso: string, t: T): string {
  const sec = (Date.parse(iso) - Date.now()) / 1000
  if (!(sec > 60)) return t('скоро')
  if (sec < 3600) return t('{n} мин', { n: Math.round(sec / 60) })
  if (sec < 48 * 3600) return t('{n} ч', { n: Math.round(sec / 3600) })
  return t('{n} дн', { n: Math.round(sec / 86400) })
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

const formOf = (w: MaintWindow): MaintIn => ({
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
const sameIds = (a: number[], b: number[]) => a.length === b.length && a.every((x) => b.includes(x))
const sameForm = (a: MaintIn, b: MaintIn) =>
  a.title === b.title &&
  sameIds(a.server_ids, b.server_ids) &&
  sameIds(a.check_ids, b.check_ids) &&
  Date.parse(a.starts_at) === Date.parse(b.starts_at) &&
  Date.parse(a.ends_at) === Date.parse(b.ends_at) &&
  a.repeat === b.repeat &&
  sameIds(a.weekdays, b.weekdays) &&
  a.verify === b.verify &&
  a.note === b.note

// идущие, потом ближайшие, потом прошедшие разовые
const rank = (w: MaintWindow) => (w.active ? 0 : w.finished ? 2 : 1)
const byWhen = (a: MaintWindow, b: MaintWindow) =>
  rank(a) - rank(b) || (a.next_start ?? a.starts_at).localeCompare(b.next_start ?? b.starts_at)

export function MaintModal({ onClose, onUnauthorized, onOpen }: Props) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [items, setItems] = useState<MaintWindow[] | null>(null)
  const [servers, setServers] = useState<Srv[]>([])
  const [checks, setChecks] = useState<Chk[]>([])
  const [sec, setSec] = useState('over')
  const [draft, setDraft] = useState<{ id: number; form: MaintIn } | null>(null)
  const [nForm, setNForm] = useState<MaintIn>(emptyForm)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const mainRef = useRef<HTMLDivElement>(null)

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    },
    [onUnauthorized, t],
  )
  const load = useCallback(() => listMaint().then(setItems).catch(fail), [fail])
  useEffect(() => {
    load()
    listServers()
      .then((ss) =>
        setServers(
          ss
            .map((s) => ({ id: s.id, name: s.name, online: s.online, enabled: s.enabled }))
            .sort((a, b) => a.name.localeCompare(b.name)),
        ),
      )
      .catch(() => {})
    checksOverview()
      .then((o) =>
        setChecks(
          o.checks
            .map((c) => ({ id: c.id, name: c.name, status: c.last_status, enabled: c.enabled }))
            .sort((a, b) => a.name.localeCompare(b.name)),
        ),
      )
      .catch(() => {})
  }, [load])

  const sorted = [...(items ?? [])].sort(byWhen)
  const curW = sec.startsWith('w') ? sorted.find((w) => `w${w.id}` === sec) : undefined
  const form = curW ? (draft && draft.id === curW.id ? draft.form : formOf(curW)) : null
  const dirty = !!curW && !!form && !sameForm(form, formOf(curW))

  const go = (id: string) => {
    if (dirty && !window.confirm(t('Уйти без сохранения? Изменения пропадут.'))) return
    setDraft(null)
    setErr(null)
    setMsg(null)
    setSec(id)
    mainRef.current?.scrollTo({ top: 0 })
  }
  const close = () => {
    if (dirty && !window.confirm(t('Закрыть без сохранения? Изменения пропадут.'))) return
    onClose()
  }
  const setForm = (f: MaintIn) => {
    if (!curW) return
    setMsg(null)
    setDraft({ id: curW.id, form: f })
  }

  async function act(fn: () => Promise<string | void>) {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const m = await fn()
      if (m) setMsg(m)
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }
  const save = () =>
    act(async () => {
      if (!curW || !form) return
      await updateMaint(curW.id, form)
      await load()
      maintReload()
      setDraft(null)
      return t('Сохранено.')
    })
  const create = () =>
    act(async () => {
      const w = await createMaint(nForm)
      setNForm(emptyForm())
      await load()
      maintReload()
      setSec(`w${w.id}`)
      return t('Окно создано. Сверку после окна и заметку можно поменять ниже.')
    })
  // разовое идущее окно: закончить сейчас или продлить на час
  const shift = (w: MaintWindow, end: Date, done: string) =>
    act(async () => {
      await updateMaint(w.id, { ...formOf(w), ends_at: end.toISOString() })
      await load()
      maintReload()
      setDraft(null)
      return done
    })
  const remove = (w: MaintWindow) => {
    if (!window.confirm(t('Удалить окно "{name}"?', { name: w.title }))) return
    act(async () => {
      await deleteMaint(w.id)
      await load()
      maintReload()
      setDraft(null)
      setSec('over')
    })
  }

  const srvOf = (id: number) => servers.find((s) => s.id === id)
  const chkOf = (id: number) => checks.find((c) => c.id === id)
  // что из окна сейчас не в норме: то же, что сверит панель после окна
  const badNow = (w: MaintIn) => [
    ...w.server_ids
      .map(srvOf)
      .filter((s): s is Srv => !!s && s.enabled && !s.online)
      .map((s) => t('{name} - не на связи', { name: s.name })),
    ...w.check_ids
      .map(chkOf)
      .filter((c): c is Chk => !!c && c.enabled && (c.status === 'down' || c.status === 'degraded'))
      .map((c) => (c.status === 'down' ? t('{name} - недоступен', { name: c.name }) : t('{name} - тормозит', { name: c.name }))),
  ]
  const statusChip = (w: MaintWindow) =>
    w.active ? (
      <span className="type-chip maint-live">{t('идет до {t}', { t: maintTime(w.cur_end) })}</span>
    ) : w.finished ? (
      <span className="type-chip off">{t('прошло')}</span>
    ) : w.next_start ? (
      <span className="type-chip">{t('следующее {t}', { t: maintTime(w.next_start) })}</span>
    ) : null
  const scope = (w: MaintIn) =>
    [
      w.server_ids.length ? t('серверов {n}', { n: w.server_ids.length }) : '',
      w.check_ids.length ? t('мониторов {n}', { n: w.check_ids.length }) : '',
    ]
      .filter(Boolean)
      .join(' · ')

  const live = sorted.filter((w) => w.active)
  const upcoming = sorted.filter((w) => !w.active && !w.finished)
  const past = sorted.filter((w) => w.finished)
  const nextW = upcoming.find((w) => w.next_start)
  const SECS: SecDef[] = [
    { id: 'over', label: 'Сводка', group: '' },
    ...[...live, ...upcoming].map((w) => ({ id: `w${w.id}`, label: w.title, group: 'Окна' })),
    ...past.map((w) => ({ id: `w${w.id}`, label: w.title, group: 'Прошедшие' })),
    ...(isViewer ? [] : [{ id: 'new', label: 'Новое окно', group: 'Добавить' }]),
  ]
  const side: Record<string, string> = {}
  for (const w of sorted)
    side[`w${w.id}`] = w.active ? t('идет') : w.finished ? '' : w.next_start ? inShort(w.next_start, t) : ''
  const cur = SECS.find((s) => s.id === sec) ?? SECS[0]

  const affected = (w: MaintIn) => {
    const rows = [
      ...w.server_ids.map((id) => {
        const s = srvOf(id)
        return {
          key: `s${id}`,
          kind: 'server' as const,
          id,
          name: s?.name ?? `#${id}`,
          what: t('сервер'),
          dot: !s || !s.enabled ? '' : s.online ? ' lk-dot-ok' : ' lk-dot-bad',
          state: !s ? '' : !s.enabled ? t('выключен') : s.online ? t('на связи') : t('не на связи'),
        }
      }),
      ...w.check_ids.map((id) => {
        const c = chkOf(id)
        const st = c?.status
        return {
          key: `c${id}`,
          kind: 'check' as const,
          id,
          name: c?.name ?? `#${id}`,
          what: t('монитор'),
          dot: !c || !c.enabled ? '' : st === 'up' ? ' lk-dot-ok' : st === 'down' ? ' lk-dot-bad' : st === 'degraded' ? ' lk-dot-warn' : '',
          state: !c
            ? ''
            : !c.enabled
              ? t('выключен')
              : st === 'up'
                ? t('работает')
                : st === 'down'
                  ? t('недоступен')
                  : st === 'degraded'
                    ? t('тормозит')
                    : t('нет данных'),
        }
      }),
    ]
    return (
      <table className="sec-tbl">
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td>
                <span className="ans-node">
                  <span className={`lk-dot${r.dot}`} />
                  <button className="linklike" onClick={() => onOpen(r.kind, r.id)}>
                    {r.name}
                  </button>
                </span>
              </td>
              <td className="muted small hide-m">{r.what}</td>
              <td className="small">{r.state}</td>
            </tr>
          ))}
        </tbody>
      </table>
    )
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Окна обслуживания')}</h3>
          {items && (
            <span className={`small ${live.length ? 't-degraded' : 'muted'}`}>
              {live.length
                ? t('идет: {n}', { n: live.length })
                : nextW?.next_start
                  ? t('ближайшее {when}', { when: untilShort(nextW.next_start, t) })
                  : t('окон нет')}
            </span>
          )}
          <button className="ghost" onClick={close}>
            {t('Закрыть')}
          </button>
        </div>

        <div className="al-body">
          <CardNav secs={SECS} active={cur.id} info={{}} side={side} onGo={go} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>
                {curW ? curW.title : t(cur.label)} {curW && statusChip(curW)}
              </h4>
            </div>
            {!items ? (
              err ? <p className="form-error">{err}</p> : <p className="muted small">{t('загрузка…')}</p>
            ) : (
              <div className="srv-sec">
                {cur.id === 'over' && (
                  <>
                    <p className="muted small al-lead">
                      {t('Плановые работы: пока окно идет, алерты по выбранным серверам и мониторам молчат. То, что осталось сломанным, придет алертом после окна, а через 5 минут после конца - сверка одним сообщением, все ли вернулось в норму.')}
                    </p>
                    <div className="net-tiles srv-tiles">
                      <button className="srv-tile-btn" onClick={() => live[0] && go(`w${live[0].id}`)} title={t('к разделу')}>
                        <NetTile
                          label={t('Идет сейчас')}
                          value={String(live.length)}
                          sub={live.length ? live.map((w) => w.title).join(', ') : t('ничего')}
                          tone={live.length ? 'warn' : undefined}
                        />
                      </button>
                      <button className="srv-tile-btn" onClick={() => nextW && go(`w${nextW.id}`)} title={t('к разделу')}>
                        <NetTile
                          label={t('Ближайшее')}
                          value={nextW?.next_start ? inShort(nextW.next_start, t) : '-'}
                          sub={nextW ? nextW.title : t('не запланировано')}
                        />
                      </button>
                      <NetTile
                        label={t('Повторяющихся')}
                        value={String(sorted.filter((w) => w.repeat).length)}
                        sub={t('каждый день или по дням недели')}
                      />
                    </div>
                    {sorted.length === 0 ? (
                      <>
                        <p className="muted small">{t('Окон пока нет.')}</p>
                        {!isViewer && (
                          <div className="ans-acts">
                            <button onClick={() => go('new')}>{t('Новое окно')}</button>
                          </div>
                        )}
                      </>
                    ) : (
                      <table className="sec-tbl">
                        <thead>
                          <tr>
                            <th>{t('Окно')}</th>
                            <th>{t('Когда')}</th>
                            <th className="hide-m">{t('Затронет')}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {sorted.map((w) => (
                            <tr key={w.id} className={w.finished ? 'ans-off' : ''}>
                              <td>
                                <button className="linklike" onClick={() => go(`w${w.id}`)}>
                                  {w.title}
                                </button>{' '}
                                {w.active && <span className="type-chip maint-live">{t('идет')}</span>}
                              </td>
                              <td className="small">{schedText(w, t)}</td>
                              <td className="muted small hide-m">{scope(w)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </>
                )}

                {curW && form && (
                  <>
                    <p className="muted small al-lead">
                      {curW.active
                        ? t('Окно идет: алерты по выбранным серверам и мониторам молчат.')
                        : curW.finished
                          ? t('Окно прошло. Прошедшие разовые окна удаляются через месяц.')
                          : t('Пока окно идет, алерты по выбранным серверам и мониторам молчат.')}
                    </p>
                    <div className="ans-kv small">
                      <span className="muted">{t('Когда')}</span>
                      <span>{schedText(curW, t)}</span>
                      <span className="muted">{t('Затронет')}</span>
                      <span>{scope(curW)}</span>
                      <span className="muted">{t('Сверка после окна')}</span>
                      <span>{curW.verify ? t('да, через 5 минут после конца') : <span className="muted">{t('нет')}</span>}</span>
                      {curW.created_by && (
                        <>
                          <span className="muted">{t('Завел')}</span>
                          <span>{curW.created_by}</span>
                        </>
                      )}
                      {curW.note && (
                        <>
                          <span className="muted">{t('Заметка')}</span>
                          <span>{curW.note}</span>
                        </>
                      )}
                    </div>
                    {curW.active && !curW.repeat && !isViewer && curW.cur_end && (
                      <div className="ans-acts">
                        <button className="ghost" disabled={busy || dirty} onClick={() => shift(curW, new Date(), t('Окно закончено, алерты снова идут.'))}>
                          {t('Закончить сейчас')}
                        </button>
                        <button
                          className="ghost"
                          disabled={busy || dirty}
                          onClick={() => shift(curW, new Date(Date.parse(curW.cur_end!) + 3600_000), t('Окно продлено на час.'))}
                        >
                          {t('Продлить на час')}
                        </button>
                      </div>
                    )}

                    <div className="srv-nav-grp rt-cap">{curW.active ? t('Что затронуто и как оно сейчас') : t('Что затронет и как оно сейчас')}</div>
                    {(() => {
                      const bad = badNow(curW)
                      return bad.length ? (
                        <p className="small t-degraded">{t('Сейчас не в норме: {list}.', { list: bad.join(', ') })}</p>
                      ) : (
                        <p className="small muted">{t('Сейчас все в норме.')}</p>
                      )
                    })()}
                    {affected(curW)}

                    {!isViewer && (
                      <>
                        <div className="srv-nav-grp rt-cap">{t('Настройки окна')}</div>
                        <MaintFields form={form} setForm={setForm} servers={servers} checks={checks} full />
                        <div className="srv-nav-grp rt-cap">{t('Удаление')}</div>
                        <div className="ans-acts">
                          <button className="ghost danger-btn" disabled={busy} onClick={() => remove(curW)}>
                            {t('Удалить окно')}
                          </button>
                        </div>
                      </>
                    )}
                  </>
                )}

                {cur.id === 'new' && (
                  <>
                    <p className="muted small al-lead">
                      {t('Что делаем, когда и что затронет. Сверка после окна включена, ее и заметку можно поменять в разделе окна после создания.')}
                    </p>
                    <MaintFields form={nForm} setForm={setNForm} servers={servers} checks={checks} />
                    <div className="ans-acts">
                      <button
                        onClick={create}
                        disabled={busy || !nForm.title.trim() || (nForm.server_ids.length === 0 && nForm.check_ids.length === 0)}
                      >
                        {busy ? t('…') : t('Создать окно')}
                      </button>
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="al-foot">
          {err && items ? (
            <span className="form-error small">{err}</span>
          ) : msg ? (
            <span className="form-ok small">{msg}</span>
          ) : dirty ? (
            <span className="al-dirty small">
              <span className="lk-dot lk-dot-warn" />
              {t('Есть несохраненные изменения')} · {form && schedText(form, t)}
            </span>
          ) : items ? (
            <span className="muted small">
              {t('Окон: {n}', { n: sorted.length - past.length })}
              {past.length ? ` · ${t('прошедших: {n}', { n: past.length })}` : ''}
            </span>
          ) : null}
          <span className="al-foot-btns">
            {dirty && (
              <button className="ghost" onClick={() => setDraft(null)} disabled={busy}>
                {t('Отмена')}
              </button>
            )}
            {curW && !isViewer && (
              <button onClick={save} disabled={!dirty || busy || !form?.title.trim()}>
                {busy ? t('…') : t('Сохранить')}
              </button>
            )}
          </span>
        </div>
      </div>
    </div>
  )
}

// Поля окна: что делаем, когда, повтор, серверы и мониторы; full - еще сверка и заметка
function MaintFields({
  form,
  setForm,
  servers,
  checks,
  full,
}: {
  form: MaintIn
  setForm: (f: MaintIn) => void
  servers: Srv[]
  checks: Chk[]
  full?: boolean
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
  const durMin = Math.round((Date.parse(form.ends_at) - Date.parse(form.starts_at)) / 60_000)
  const startNow = () => {
    const s = new Date()
    s.setSeconds(0, 0)
    set({ starts_at: s.toISOString(), ends_at: new Date(s.getTime() + Math.max(durMin, 1) * 60_000).toISOString() })
  }
  return (
    <div className="mw-fields">
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
      <div className="rt-row">
        {!form.repeat && (
          <button className="ghost small" onClick={startNow}>
            {t('Начать сейчас')}
          </button>
        )}
        <div className="win-switch">
          {DUR.map(([m, l]) => (
            <button
              key={m}
              className={`win-btn${durMin === m ? ' win-btn-active' : ''}`}
              onClick={() => set({ ends_at: new Date(Date.parse(form.starts_at) + m * 60_000).toISOString() })}
            >
              {t(l)}
            </button>
          ))}
        </div>
      </div>
      {form.repeat === 'weekly' && (
        <div className="win-switch">
          {WD.map((d, i) => (
            <button
              key={d}
              className={`win-btn${form.weekdays.includes(i) ? ' win-btn-active' : ''}`}
              onClick={() =>
                set({ weekdays: form.weekdays.includes(i) ? form.weekdays.filter((x) => x !== i) : [...form.weekdays, i] })
              }
            >
              {t(d)}
            </button>
          ))}
        </div>
      )}
      {form.repeat && (
        <div className="muted small">{t('Повторы - в то же время и той же длины, что и первое окно, по часовому поясу алертов.')}</div>
      )}
      <div className="maint-pick-row">
        <Picker title={t('Серверы')} opts={servers} value={form.server_ids} onChange={(v) => set({ server_ids: v })} />
        <Picker title={t('Мониторы')} opts={checks} value={form.check_ids} onChange={(v) => set({ check_ids: v })} />
      </div>
      {full && (
        <>
          <label className="check-inline">
            <input type="checkbox" checked={form.verify} onChange={(e) => set({ verify: e.target.checked })} />
            {t('Сверка после окна: прислать, все ли вернулось в норму')}
          </label>
          <label className="field field-wide">
            <span>{t('Заметка')}</span>
            <textarea rows={2} maxLength={1000} value={form.note} onChange={(e) => set({ note: e.target.value })} />
          </label>
        </>
      )}
    </div>
  )
}

// Выбор серверов или мониторов: поиск и галки, выбранные сверху
function Picker({
  title,
  opts,
  value,
  onChange,
}: {
  title: string
  opts: { id: number; name: string }[]
  value: number[]
  onChange: (v: number[]) => void
}) {
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
        <div className="chart-cap net-cap">
          {live.length ? t('Идет обслуживание') : t('Скоро обслуживание')}
          <span className={`net-n${live.length ? ' net-n-warn' : ''}`}>{live.length || soon.length}</span>
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
