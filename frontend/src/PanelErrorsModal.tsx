import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  getPanelErrors,
  resolvePanelErrors,
  type PanelErrorBrief,
  type PanelErrorKind,
  type PanelErrorRow,
  type PanelErrorsOut,
} from './api'
import { CardNav, type SecDef } from './cardLayout'
import { useI18n } from './i18n'
import { NetTile } from './NetLinks'
import { sinceShort } from './serverUtils'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
  onChanged: () => void // счетчик неразобранного у шестеренки
  onOpenServer: (id: number) => void
}

const KIND_LABEL: Record<PanelErrorKind, string> = {
  report: 'Сообщения людей',
  server: 'Бэкенд',
  api: 'Действия',
  command: 'Команды агенту',
  front: 'Интерфейс',
}
const KIND_CHIP: Record<PanelErrorKind, string> = {
  report: 'сообщение',
  server: 'бэкенд',
  api: 'действие',
  command: 'команда',
  front: 'интерфейс',
}
const KIND_LEAD: Record<PanelErrorKind, string> = {
  report: 'Что люди написали через кнопку Сообщить о проблеме, с их ошибками за 15 минут до сообщения.',
  server: 'Необработанные исключения бэкенда (500): маршрут, кто и короткий traceback кода панели.',
  api: 'Изменяющие запросы, на которые панель ответила отказом. Протухший вход (401), неверный пароль и не прошедшие проверку формы (422) сюда не пишутся.',
  command: 'Команды агенту и helper-скриптам, которые запустил человек и которые не прошли: вывод helper-скрипта и параметры команды.',
  front: 'Ошибки в браузере: исключения, необработанные промисы и упавшие при отрисовке экраны.',
}
const ORDER: PanelErrorKind[] = ['report', 'server', 'api', 'command', 'front']
const PERIODS = [1, 7, 30]

// "Ошибки панели" (только админ): на что люди натыкаются в интерфейсе, без скриншотов. Раскладка
// как у остальных окон шестеренки: меню слева, один раздел; разделы - по видам записей и
// разобранное. Отметка "разобрано" ставится сразу, без сохранения.
export function PanelErrorsModal({ onClose, onUnauthorized, onChanged, onOpenServer }: Props) {
  const { t } = useI18n()
  const [sec, setSec] = useState('over')
  const [days, setDays] = useState(7)
  const [q, setQ] = useState('')
  const [qd, setQd] = useState('')
  const [data, setData] = useState<PanelErrorsOut | null>(null)
  const [dataKey, setDataKey] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [tick, setTick] = useState(0)
  const mainRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const id = window.setTimeout(() => setQd(q.trim()), 350)
    return () => window.clearTimeout(id)
  }, [q])

  const state = sec === 'resolved' ? 'resolved' : 'open'
  const kind = ORDER.includes(sec as PanelErrorKind) ? sec : ''
  const key = JSON.stringify({ state, kind, q: qd, days, tick })

  useEffect(() => {
    let alive = true
    getPanelErrors({ state, kind, q: qd, days })
      .then((d) => {
        if (!alive) return
        setData(d)
        setDataKey(key)
        setErr(null)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
        else if (alive) setErr(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
  }, [key, state, kind, qd, days, onUnauthorized])

  const mark = useCallback(
    async (ids: number[], resolved: boolean) => {
      if (!ids.length) return
      setBusy(true)
      try {
        await resolvePanelErrors(ids, resolved)
        setTick((x) => x + 1)
        onChanged()
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        setErr(e instanceof Error ? e.message : String(e))
      } finally {
        setBusy(false)
      }
    },
    [onChanged, onUnauthorized],
  )

  const go = (id: string) => {
    setSec(id)
    mainRef.current?.scrollTo({ top: 0 })
  }

  const c = data?.counts
  const SECS: SecDef[] = [
    { id: 'over', label: 'Сводка', group: '' },
    ...ORDER.map((k) => ({ id: k, label: KIND_LABEL[k], group: 'Неразобранное' })),
    { id: 'resolved', label: 'Разобранное', group: 'Архив' },
  ]
  const side: Record<string, string> = {}
  for (const k of ORDER) side[k] = c?.by_kind[k] ? String(c.by_kind[k]) : ''
  if (c?.resolved) side.resolved = String(c.resolved)
  const cur = SECS.find((x) => x.id === sec) ?? SECS[0]
  const items = data?.items ?? []
  const fresh = dataKey === key

  const overview = () => {
    const latest = items.slice(0, 8)
    return (
      <>
        <p className="muted small al-lead">
          {t('Панель сама записывает, на что люди натыкаются: падения бэкенда, неудачные действия, команды агенту, которые не прошли, и ошибки интерфейса. Одинаковые складываются в одну строку со счетчиком. Хранится {d} дней, не больше {n} записей. В Telegram это не уходит.', {
            d: data?.keep_days ?? 30,
            n: data?.max_rows ?? 5000,
          })}
        </p>
        <div className="net-tiles srv-tiles">
          <button className="srv-tile-btn" onClick={() => go('report')} title={t('к разделу')}>
            <NetTile label={t('Сообщений людей')} value={String(c?.by_kind.report ?? 0)} sub={t('неразобранных')} />
          </button>
          <NetTile label={t('Неразобрано')} value={String(c?.open ?? 0)} sub={t('записей всех видов')} />
          <NetTile label={t('За сутки')} value={String(c?.day ?? 0)} sub={t('ошибок появилось или повторилось')} />
          <button className="srv-tile-btn" onClick={() => go('resolved')} title={t('к разделу')}>
            <NetTile label={t('Разобрано')} value={String(c?.resolved ?? 0)} sub={t('за время хранения')} />
          </button>
        </div>
        <div className="srv-nav-grp rt-cap">{t('Свежее')}</div>
        {latest.length === 0 ? (
          <p className="muted small">{t('Неразобранных ошибок нет.')}</p>
        ) : (
          <div className="pe-list">
            {latest.map((x) => (
              <ErrRow key={x.id} x={x} busy={busy} onMark={mark} onOpenServer={onOpenServer} showKind />
            ))}
          </div>
        )}
      </>
    )
  }

  const list = () => (
    <>
      <p className="muted small al-lead">
        {cur.id === 'resolved' ? t('Отмеченное как разобранное. Если ошибка повторится, она вернется в неразобранное с пометкой.') : t(KIND_LEAD[cur.id as PanelErrorKind])}
      </p>
      {items.length === 0 ? (
        <p className="muted small">{qd ? t('Ничего не нашлось.') : t('Пусто.')}</p>
      ) : (
        <div className="pe-list">
          {items.map((x) => (
            <ErrRow key={x.id} x={x} busy={busy} onMark={mark} onOpenServer={onOpenServer} showKind={cur.id === 'resolved'} />
          ))}
        </div>
      )}
      {data?.more && <p className="muted small">{t('Показаны последние {n}: сузьте период или поиск.', { n: items.length })}</p>}
    </>
  )

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Ошибки панели')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="al-body">
          <CardNav secs={SECS} active={cur.id} info={{}} side={side} onGo={go} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
              <div className="win-switch">
                {PERIODS.map((d) => (
                  <button key={d} className={`win-btn${d === days ? ' win-btn-active' : ''}`} onClick={() => setDays(d)}>
                    {d === 1 ? t('сутки') : t('{n} дней', { n: d })}
                  </button>
                ))}
              </div>
            </div>
            {cur.id !== 'over' && (
              <div className="modal-toolbar ah-tools">
                <input
                  className="checks-search modal-search"
                  placeholder={t('Поиск: текст, кто, сервер, адрес')}
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                />
                {cur.id !== 'resolved' && items.length > 1 && (
                  <button className="ghost small" disabled={busy} onClick={() => mark(items.map((x) => x.id), true)}>
                    {t('Все показанные разобраны')}
                  </button>
                )}
              </div>
            )}
            {err && <p className="form-error">{err}</p>}
            {!data ? (
              !err && <p className="muted small">{t('загрузка…')}</p>
            ) : (
              <div className={`srv-sec${fresh ? '' : ' rp-busy'}`}>{cur.id === 'over' ? overview() : list()}</div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function ErrRow({
  x,
  busy,
  onMark,
  onOpenServer,
  showKind,
}: {
  x: PanelErrorRow
  busy: boolean
  onMark: (ids: number[], resolved: boolean) => void
  onOpenServer: (id: number) => void
  showKind?: boolean
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const ago = sinceShort(x.last_at, t)
  const attached = x.extra?.errors ?? []
  const clientLog = x.extra?.client_log ?? []
  const params = x.extra?.params
  const hasMore = !!x.detail || !!params || attached.length > 0 || clientLog.length > 0
  return (
    <div className={`pe-row pe-${x.kind}${x.resolved ? ' pe-done' : ''}`}>
      <div className="pe-head">
        {showKind && <span className={`type-chip pe-chip pe-chip-${x.kind}`}>{t(KIND_CHIP[x.kind])}</span>}
        {x.kind !== 'report' && <span className="mono pe-act">{x.action}{x.status ? ` ${x.status}` : ''}</span>}
        {x.kind === 'report' && <span className="pe-who-main">{x.username || '?'}</span>}
        {x.count > 1 && <span className="pe-count" title={t('сколько раз')}>x{x.count}</span>}
        {x.reopened && !x.resolved && <span className="pe-again">{t('снова после разбора')}</span>}
        <span className="muted small pe-when" title={new Date(x.last_at).toLocaleString()}>
          {ago ? t('{t} назад', { t: ago }) : t('только что')}
        </span>
      </div>
      <div className="pe-msg">{x.message}</div>
      <div className="pe-meta muted small">
        {x.kind !== 'report' && x.username && <span>{t('кто')}: {x.username}</span>}
        {x.server_id != null && (
          <span>
            {t('сервер')}:{' '}
            <button className="linklike" onClick={() => onOpenServer(x.server_id!)}>
              {x.server_name || `#${x.server_id}`}
            </button>
          </span>
        )}
        {x.page && (
          <span>
            {t('где')}:{' '}
            <a className="mono" href={x.page} target="_blank" rel="noreferrer">
              {x.page}
            </a>
          </span>
        )}
        {x.count > 1 && <span>{t('впервые')}: {new Date(x.first_at).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</span>}
        {x.version && <span>{t('версия')} {x.version}</span>}
        {x.resolved && x.resolved_by && <span>{t('разобрал {who}', { who: x.resolved_by })}</span>}
      </div>
      {x.kind === 'report' && attached.length > 0 && (
        <div className="pe-att">
          <div className="muted small">{t('Ошибки этого человека за 15 минут до сообщения')}:</div>
          {attached.map((e: PanelErrorBrief) => (
            <div key={e.id} className="pe-att-row small">
              <span className={`type-chip pe-chip pe-chip-${e.kind}`}>{t(KIND_CHIP[e.kind])}</span>
              <span className="mono">{e.action}{e.status ? ` ${e.status}` : ''}</span>
              <span>{e.message}</span>
              {e.count > 1 && <span className="pe-count">x{e.count}</span>}
            </div>
          ))}
        </div>
      )}
      {open && (
        <div className="pe-more">
          {x.kind === 'report' && clientLog.length > 0 && (
            <>
              <div className="muted small">{t('Неудачные запросы вкладки')}:</div>
              <pre className="pe-pre">
                {clientLog.map((c) => `${c.ts.slice(11, 19)} ${c.method} ${c.path} ${c.status || '-'} ${c.detail}`).join('\n')}
              </pre>
            </>
          )}
          {params && (
            <>
              <div className="muted small">{t('Параметры команды')}:</div>
              <pre className="pe-pre">{JSON.stringify(params, null, 1)}</pre>
            </>
          )}
          {x.detail && <pre className="pe-pre">{x.detail}</pre>}
        </div>
      )}
      <div className="pe-acts-row">
        {hasMore && (
          <button className="ghost small" onClick={() => setOpen((v) => !v)}>
            {open ? t('Скрыть подробности') : t('Подробности')}
          </button>
        )}
        <button className="ghost small" disabled={busy} onClick={() => onMark([x.id], !x.resolved)}>
          {x.resolved ? t('Вернуть в неразобранное') : t('Разобрано')}
        </button>
      </div>
    </div>
  )
}
