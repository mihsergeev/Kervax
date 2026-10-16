import { useEffect, useRef, useState } from 'react'
import { ApiError, alertHistory, type AlertEventDto, type AlertHistory, type AlertHistoryQuery, type ReportNoise } from './api'
import { CardNav, type SecDef } from './cardLayout'
import { useI18n } from './i18n'
import { NetTile } from './NetLinks'
import { NoiseTable } from './NoiseTable'
import { times } from './reportNoise'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
  initialTarget?: string // открыть сразу на сервере или мониторе (из карточки сервера)
  onOpen?: (kind: 'server' | 'check', id: number) => void
}

const PERIODS = [1, 7, 30, 180]

// История отправленных алертов (backend api/alert_history.py), раскладка как у карточки сервера.
// Нужна на два вопроса: приходил ли алерт про эту беду и когда, и что шумит больше всего. На
// сводке - что шумело: что именно и где, как в отчете; у самых шумных объектов свой раздел с
// их сообщениями; "Все сообщения" - лента с поиском и отбором.
export function AlertHistoryModal({ onClose, onUnauthorized, initialTarget, onOpen }: Props) {
  const { t } = useI18n()
  const [days, setDays] = useState(7)
  const [sec, setSec] = useState(initialTarget ? `t:${initialTarget}` : 'over')
  // сводка и меню: весь период без отбора
  const [base, setBase] = useState<AlertHistory | null>(null)
  const [baseDays, setBaseDays] = useState<number | null>(null)
  // лента: отбор
  const [only, setOnly] = useState<'all' | 'fires' | 'recoveries'>('all')
  const [kind, setKind] = useState('')
  const [target, setTarget] = useState('')
  const [q, setQ] = useState('')
  const [qDebounced, setQDebounced] = useState('')
  // раздел ленты или объекта: его ответ и подгруженные раньше сообщения
  const [part, setPart] = useState<AlertHistory | null>(null)
  const [partKey, setPartKey] = useState('')
  const [events, setEvents] = useState<AlertEventDto[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const mainRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const id = window.setTimeout(() => setQDebounced(q.trim()), 400)
    return () => window.clearTimeout(id)
  }, [q])

  useEffect(() => {
    let alive = true
    alertHistory({ days })
      .then((d) => {
        if (!alive) return
        setBase(d)
        setBaseDays(days)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
        else if (alive) setErr(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
  }, [days, onUnauthorized])

  const secTarget = sec.startsWith('t:') ? sec.slice(2) : ''
  const query: AlertHistoryQuery | null =
    sec === 'feed'
      ? { days, only, kind, target, q: qDebounced }
      : secTarget
        ? { days, target: secTarget }
        : null
  const key = query ? JSON.stringify(query) : ''

  useEffect(() => {
    if (!key) return
    let alive = true
    alertHistory(JSON.parse(key) as AlertHistoryQuery)
      .then((d) => {
        if (!alive) return
        setPart(d)
        setPartKey(key)
        setEvents(d.events)
        setErr(null)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
        else if (alive) setErr(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
  }, [key, onUnauthorized])

  const more = async () => {
    const last = events[events.length - 1]
    if (!last || !query) return
    setBusy(true)
    try {
      const d = await alertHistory({ ...query, before_id: last.id })
      setEvents((prev) => [...prev, ...d.events])
      setPart((prev) => (prev ? { ...prev, more: d.more } : d))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const go = (id: string) => {
    setSec(id)
    mainRef.current?.scrollTo({ top: 0 })
  }
  // к сообщениям про это: лента с отбором по объекту и виду
  const toFeed = (tg: string, kd: string) => {
    setTarget(tg)
    setKind(kd)
    setOnly('all')
    setQ('')
    go('feed')
  }

  const s = base?.summary
  const targets = s?.targets ?? []
  // объект из карточки сервера - в меню, даже если он не из самых шумных
  const navTargets =
    initialTarget && !targets.some((x) => x.target === initialTarget)
      ? [...targets, { target: initialTarget, section: '', n: 0, srv_id: null, chk_id: null }]
      : targets
  const SECS: SecDef[] = [
    { id: 'over', label: 'Сводка', group: '' },
    { id: 'feed', label: 'Все сообщения', group: '' },
    ...navTargets.map((x) => ({ id: `t:${x.target}`, label: x.target, group: 'Чаще всего' })),
  ]
  const side: Record<string, string> = { feed: s ? String(s.total) : '' }
  for (const x of navTargets) side[`t:${x.target}`] = x.n ? String(x.n) : ''
  const cur = SECS.find((x) => x.id === sec) ?? SECS[0]
  const curT = navTargets.find((x) => `t:${x.target}` === cur.id)
  // ответ именно на текущий отбор: пока идет новый, старый виден приглушенным
  const fresh = part && partKey === key ? part : null
  // карточка объекта: id из сводки, иначе из строк раздела
  const ids = curT && (curT.srv_id != null || curT.chk_id != null) ? curT : fresh?.summary.items[0]

  const inNav = (tg: string) => navTargets.some((x) => x.target === tg)
  const name = (x: ReportNoise) =>
    inNav(x.target) ? (
      <button className="linklike" onClick={() => go(`t:${x.target}`)}>
        {x.target}
      </button>
    ) : (
      <button className="linklike" onClick={() => toFeed(x.target, '')}>
        {x.target}
      </button>
    )

  const period = (
    <div className="win-switch">
      {PERIODS.map((d) => (
        <button key={d} className={`win-btn${d === days ? ' win-btn-active' : ''}`} onClick={() => setDays(d)}>
          {d === 1 ? t('сутки') : t('{n} дней', { n: d })}
        </button>
      ))}
    </div>
  )

  // сообщения по дням: дата подписью, у строки время, объект и вид
  const feed = (all: AlertEventDto[], withTarget: boolean) => {
    if (!all.length) return <p className="muted small">{t('За этот период алертов не было.')}</p>
    // подписи дней верны, только если новое выше старого - не полагаемся на порядок id
    const list = [...all].sort((a, b) => b.ts.localeCompare(a.ts) || b.id - a.id)
    const day = (ts: string) => new Date(ts).toLocaleDateString([], { weekday: 'short', day: '2-digit', month: '2-digit' })
    return (
      <div className="ah-list">
        {list.map((e, i) => (
          <div key={e.id}>
            {(i === 0 || day(list[i - 1].ts) !== day(e.ts)) && <div className="srv-nav-grp rt-cap">{day(e.ts)}</div>}
            <div className={`ah-row${e.recovery ? ' ah-rec' : ''}`}>
              <div className="ah-meta muted small">
                <span className="mono">{new Date(e.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                {withTarget && e.target && (
                  <button className="ghost ah-link" onClick={() => (inNav(e.target) ? go(`t:${e.target}`) : setTarget(e.target))}>
                    {e.target}
                  </button>
                )}
                {e.kind && <span>{e.label}</span>}
                {e.recovery && <span className="ah-rec-tag">{t('отбой')}</span>}
              </div>
              <div className="ah-text">{e.text}</div>
            </div>
          </div>
        ))}
      </div>
    )
  }
  const moreBtn = part?.more && (
    <div className="ans-acts">
      <button className="ghost" disabled={busy} onClick={more}>
        {busy ? '...' : t('Показать раньше')}
      </button>
    </div>
  )

  const overview = () => {
    if (!s) return null
    const top = targets[0]
    return (
      <>
        <p className="muted small al-lead">
          {t('Каждый доставленный алерт и отбой хранится полгода. Что шумело - что именно и где, как в отчете: имя ведет к объекту, строка - к сообщениям про это.')}
        </p>
        <div className="net-tiles srv-tiles">
          <button className="srv-tile-btn" onClick={() => toFeed('', '')} title={t('к сообщениям')}>
            <NetTile label={t('Срабатываний')} value={String(s.fires)} sub={t('отбоев: {n}', { n: s.recoveries })} />
          </button>
          {top && (
            <button className="srv-tile-btn" onClick={() => go(`t:${top.target}`)} title={t('к разделу')}>
              <NetTile label={t('Чаще всего')} value={top.target} sub={times(top.n, t)} />
            </button>
          )}
        </div>
        {s.items.length === 0 ? (
          <p className="muted small">{t('За этот период алертов не было.')}</p>
        ) : (
          <>
            <div className="srv-nav-grp rt-cap">{t('Что шумело')}</div>
            <NoiseTable items={s.items} name={name} onPick={(x) => toFeed(x.target, x.kind)} />
            {s.more_items > 0 && <p className="muted small">{t('И еще {n} реже - в ленте.', { n: s.more_items })}</p>}
          </>
        )}
      </>
    )
  }

  const feedSec = () => (
    <>
      <div className="modal-toolbar ah-tools">
        <input
          className="checks-search modal-search"
          placeholder={t('Поиск по тексту')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select className="modal-sort" value={only} onChange={(e) => setOnly(e.target.value as typeof only)}>
          <option value="all">{t('все')}</option>
          <option value="fires">{t('только срабатывания')}</option>
          <option value="recoveries">{t('только отбои')}</option>
        </select>
        {(s?.kinds.length ?? 0) > 0 && (
          <select className="modal-sort" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">{t('любой вид')}</option>
            {s!.kinds.map((k) => (
              <option key={k.kind} value={k.kind}>
                {k.label}
              </option>
            ))}
            {kind && !s!.kinds.some((k) => k.kind === kind) && <option value={kind}>{kind}</option>}
          </select>
        )}
        {target && (
          <button className="type-chip ah-chip" onClick={() => setTarget('')} title={t('Снять фильтр')}>
            {target} ✕
          </button>
        )}
      </div>
      {fresh && (
        <p className="muted small">
          {t('Срабатываний: {f}, отбоев: {r}', { f: fresh.summary.fires, r: fresh.summary.recoveries })}
        </p>
      )}
      {part && feed(events, true)}
      {moreBtn}
    </>
  )

  const targetSec = () =>
    fresh && (
      <>
        <p className="muted small al-lead">
          {t('Срабатываний: {f}, отбоев: {r}', { f: fresh.summary.fires, r: fresh.summary.recoveries })}
        </p>
        {fresh.summary.items.length > 0 && (
          <>
            <div className="srv-nav-grp rt-cap">{t('Что было')}</div>
            <NoiseTable items={fresh.summary.items} name={name} flat onPick={(x) => toFeed(x.target, x.kind)} />
          </>
        )}
        <div className="srv-nav-grp rt-cap">{t('Сообщения')}</div>
        {feed(events, false)}
        {moreBtn}
      </>
    )

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('История алертов')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>

        <div className="al-body">
          <CardNav secs={SECS} active={cur.id} info={{}} side={side} onGo={go} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>{curT ? curT.target : t(cur.label)}</h4>
              {period}
              {curT && onOpen && ids && (ids.srv_id != null || ids.chk_id != null) && (
                <button
                  className="ghost small"
                  onClick={() => (ids.srv_id != null ? onOpen('server', ids.srv_id) : onOpen('check', ids.chk_id!))}
                >
                  {t('Карточка')} ›
                </button>
              )}
            </div>
            {err && <p className="form-error">{err}</p>}
            {!base ? (
              !err && <p className="muted small">{t('загрузка…')}</p>
            ) : (
              <div className={`srv-sec${baseDays !== days || (key && partKey !== key) ? ' rp-busy' : ''}`}>
                {cur.id === 'over' && overview()}
                {cur.id === 'feed' && feedSec()}
                {curT && targetSec()}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
