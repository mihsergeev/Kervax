// Заглушенное одним списком: пятая плитка "Заглушено" в ряду плиток раздела и панель справа.
// Раньше сводка висела чипами над списком: на 13 мьютах занимала несколько строк, на сотне
// закрыла бы страницу. Плитка держит любое число, а весь список с поиском, отбором и снятием -
// в панели. Общая для "Серверов" (пороги и алерты) и "Бэкапов" (репозитории и покрытие).
import { Fragment, useCallback, useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ApiError, type Server } from './api'
import { useI18n } from './i18n'
import { liftMute, muteAutoNote, muteLeft, mutesByServer, muteSoon, restoreMute, type MuteRow, type TFn } from './mutes'

const rowId = (r: MuteRow) => `${r.sid}|${r.group}|${r.until ? 's' : 'p'}|${r.key}`

const fmtUntil = (u: string) =>
  new Date(u).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })

function When({ r, t }: { r: MuteRow; t: TFn }) {
  if (!r.until) return <span className="muted">{(r.group === 'alert' && muteAutoNote(r.key, t)) || t('бессрочно')}</span>
  return (
    <>
      <span>{t('ещё {d}', { d: muteLeft(r.until, t) })}</span>
      <span className="muted mt-until">{t('до {t}', { t: fmtUntil(r.until) })}</span>
    </>
  )
}

const FOCUSABLE = 'button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex="-1"])'

// Панель справа поверх страницы. rows - что заглушено на этой странице; servers - их свежее
// состояние (снятие бессрочного сохраняет alert_mutes сервера целиком). Снятое не пропадает из
// списка сразу: строка остается зачеркнутой с кнопкой "вернуть" - случайный клик по крестику
// в длинном списке исправляется на месте
export function MutesPanel({
  rows,
  servers,
  canEdit,
  onOpen,
  onChanged,
  onUnauthorized,
  onClose,
}: {
  rows: MuteRow[]
  servers: Server[]
  canEdit: boolean
  onOpen: (sid: number) => void
  onChanged: (s: Server) => void
  onUnauthorized: () => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const titleId = useId()
  const [q, setQ] = useState('')
  const [only, setOnly] = useState<'all' | 'perm' | 'timed'>('all')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // снятые за время, пока панель открыта: их можно вернуть
  const [lifted, setLifted] = useState<MuteRow[]>([])
  const box = useRef<HTMLElement>(null)
  const search = useRef<HTMLInputElement>(null)
  // фокус уходит в панель (поле поиска) и по закрытию возвращается туда, откуда ее открыли
  const back = useRef<HTMLElement | null>(null)
  useEffect(() => {
    back.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    // пустой список - без поля поиска, тогда на крестик
    const first = search.current ?? box.current?.querySelector<HTMLElement>('button')
    first?.focus()
  }, [])
  const close = useCallback(() => {
    onClose()
    back.current?.focus()
  }, [onClose])
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !e.defaultPrevented) close()
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [close])
  // Tab не уходит из панели на страницу под ней
  const trap = (e: ReactKeyboardEvent) => {
    if (e.key !== 'Tab' || !box.current) return
    const els = [...box.current.querySelectorAll<HTMLElement>(FOCUSABLE)]
    if (!els.length) return
    const first = els[0]
    const last = els[els.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }

  const run = async (r: MuteRow, fn: (s: Server, r: MuteRow) => Promise<Server>, after: () => void) => {
    const s = servers.find((x) => x.id === r.sid)
    if (!s) return
    setBusy(true)
    setErr(null)
    try {
      onChanged(await fn(s, r))
      after()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }
  const lift = (r: MuteRow) => run(r, liftMute, () => setLifted((xs) => [...xs, r]))
  const restore = (r: MuteRow) =>
    run(r, restoreMute, () => setLifted((xs) => xs.filter((x) => rowId(x) !== rowId(r))))

  const live = new Set(rows.map(rowId))
  const gone = new Set(lifted.map(rowId))
  const all = [...rows, ...lifted.filter((r) => !live.has(rowId(r)))]
  const qq = q.trim().toLowerCase()
  const perm = rows.filter((r) => !r.until).length
  const timed = rows.length - perm
  const soon = rows.filter((r) => muteSoon(r)).length
  const found = all
    .filter((r) => !qq || r.server.toLowerCase().includes(qq) || r.label.toLowerCase().includes(qq))
    .filter((r) => (only === 'all' ? true : only === 'perm' ? !r.until : !!r.until))
  const groups = mutesByServer(found)
  const nSrv = new Set(rows.map((r) => r.sid)).size
  return (
    <div className="mt-drawer-back" onClick={(e) => e.target === e.currentTarget && close()}>
      <aside
        ref={box}
        className="card mt-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={trap}
      >
        <div className="mt-head">
          <h3 id={titleId} className="mt-title">
            {t('Заглушено')}
            <span className="net-n">{rows.length}</span>
          </h3>
          <button className="ghost icon-btn mt-close" onClick={close} title={t('Закрыть')} aria-label={t('Закрыть')}>
            ✕
          </button>
        </div>
        {rows.length + lifted.length > 0 && (
          <div className="mt-totals small muted">
            <span>{t('на серверах: {n}', { n: nSrv })}</span>
            {perm > 0 && <span>{t('бессрочно {n}', { n: perm })}</span>}
            {soon > 0 && <span>{t('истекут за сутки {n}', { n: soon })}</span>}
            {!canEdit && <span>{t('снять может оператор или админ')}</span>}
          </div>
        )}
        {err && <div className="small t-down">{err}</div>}
        {rows.length + lifted.length > 0 && (
          <div className="mt-tools">
            <input
              ref={search}
              className="checks-search mt-search"
              type="search"
              autoComplete="off"
              aria-label={t('Поиск')}
              placeholder={t('Сервер или что заглушено')}
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            {/* отбор есть, только когда есть из чего выбирать: у бэкапов все бессрочные */}
            {perm > 0 && timed > 0 && (
              <div className="win-switch mt-switch" role="group" aria-label={t('Срок')}>
                {(
                  [
                    ['all', t('Все')],
                    ['perm', t('Бессрочно {n}', { n: perm })],
                    ['timed', t('Со сроком {n}', { n: timed })],
                  ] as const
                ).map(([k, l]) => (
                  <button
                    key={k}
                    className={`win-btn${only === k ? ' win-btn-active' : ''}`}
                    aria-pressed={only === k}
                    onClick={() => setOnly(k)}
                  >
                    {l}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        <div className="mt-drawer-body">
          {rows.length + lifted.length === 0 ? (
            <div className="srv-empty">{t('Ничего не заглушено.')}</div>
          ) : groups.length === 0 ? (
            <div className="srv-empty">{t('Ничего не найдено.')}</div>
          ) : (
            <table className="sec-tbl mt-tbl">
              <tbody>
                {groups.map((g) => (
                  <Fragment key={g.sid}>
                    <tr className="mt-grp">
                      <td colSpan={3}>
                        <button className="mt-srv" onClick={() => onOpen(g.sid)} title={t('открыть карточку')}>
                          {g.server}
                        </button>
                        <span className="net-n">{g.items.length}</span>
                      </td>
                    </tr>
                    {g.items.map((r) => {
                      const off = gone.has(rowId(r)) && !live.has(rowId(r))
                      return (
                        <tr key={rowId(r)} className={off ? 'mt-off' : undefined}>
                          <td className="mt-what">
                            <button className="mt-what-btn" onClick={() => onOpen(r.sid)} title={t('открыть карточку')}>
                              {r.label}
                            </button>
                          </td>
                          <td className="mt-when-c">
                            {off ? <span className="muted">{t('заглушка снята')}</span> : <When r={r} t={t} />}
                          </td>
                          <td className="mt-x-c">
                            {canEdit &&
                              (off ? (
                                <button className="ghost mt-undo" disabled={busy} onClick={() => restore(r)}>
                                  {t('вернуть')}
                                </button>
                              ) : (
                                <button
                                  className="ghost mt-x"
                                  disabled={busy}
                                  title={t('Снять заглушку')}
                                  aria-label={`${t('Снять заглушку')}: ${r.server}, ${r.label}`}
                                  onClick={() => lift(r)}
                                >
                                  ✕
                                </button>
                              ))}
                          </td>
                        </tr>
                      )
                    })}
                  </Fragment>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </aside>
    </div>
  )
}
