import { useEffect, useRef, useState } from 'react'
import { ApiError, getRetentionOverview, putRetention, type RetentionGroup, type RetentionOverview } from './api'
import { CardNav, type SecDef } from './cardLayout'
import { useI18n } from './i18n'
import { NetTile } from './NetLinks'
import { fmtBytes } from './units'

// Окно "Хранение данных" как карточка сервера: сводка с размером базы и полосой по видам данных,
// по разделу на каждый срок из настроек (сроки кнопками, сколько занимает, сколько прибавляется в
// сутки, сколько станет) и постоянные сроки - то, что чистится само по своим правилам.

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

type Gid = 'srv' | 'chk'
type Want = Record<Gid, number>

const GROUP: Record<Gid, { label: string; what: string }> = {
  srv: {
    label: 'Метрики серверов',
    what: 'CPU, память, сеть и диски с агентов, связь между серверами, ошибки 5xx, OOM. Старше срока удаляется само.',
  },
  chk: {
    label: 'История проверок',
    what: 'Время ответа и статусы мониторов, проверки через локации, адреса сайтов, закрытые инциденты. Старше срока удаляется само.',
  },
}
const PART: Record<string, string> = {
  server_metrics: 'Метрики с агентов',
  link_samples: 'Связь между серверами',
  web_error_samples: 'Ошибки 5xx по сайтам',
  oom_events: 'OOM',
  check_samples: 'Проверки мониторов',
  location_samples: 'Проверки через локации',
  check_ip_samples: 'Адреса сайтов',
  check_incidents: 'Инциденты',
}
const FIXED: Record<string, string> = {
  alerts: 'История алертов',
  timeline: 'Хронология серверов',
  traffic: 'Трафик по дням',
  usage: 'Кто качает трафик и пишет на диск, по часам',
  commands: 'Команды Docker, Kubernetes и бэкапа',
  web_lines: 'Строки ошибок 5xx: адреса клиентов, запросы',
  domain_probes: 'Проверки домена при добавлении сайта',
  probes: 'Ручные проверки',
  panel_requests: 'Левые запросы к панели',
  panel_errors: 'Ошибки панели и сообщения о проблемах',
}
const PRESETS: [number, string][] = [
  [7, '7д'],
  [14, '14д'],
  [30, '30д'],
  [90, '90д'],
  [180, 'полгода'],
  [365, 'год'],
]

const SECS: SecDef[] = [
  { id: 'over', label: 'Сводка', group: '' },
  { id: 'srv', label: GROUP.srv.label, group: 'Срок хранения' },
  { id: 'chk', label: GROUP.chk.label, group: 'Срок хранения' },
  { id: 'fix', label: 'Постоянные сроки', group: 'Без настройки' },
]
const LEAD: Record<string, string> = {
  over: 'Сколько занимает база панели и из чего она состоит. Больше всего места у двух видов данных, срок которых настраивается.',
  fix: 'Эти данные чистятся по своим срокам: они маленькие или нужны ровно столько.',
}

const daysOf = (ov: RetentionOverview): Want => ({
  srv: ov.groups.find((g) => g.id === 'srv')?.days ?? 30,
  chk: ov.groups.find((g) => g.id === 'chk')?.days ?? 30,
})

export function RetentionModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [ov, setOv] = useState<RetentionOverview | null>(null)
  const [want, setWant] = useState<Want>({ srv: 30, chk: 30 })
  const [sec, setSec] = useState('over')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const mainRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    getRetentionOverview()
      .then((r) => {
        setOv(r)
        setWant(daysOf(r))
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
        else setErr(e instanceof Error ? e.message : t('Ошибка'))
      })
  }, [onUnauthorized, t])

  const saved = ov ? daysOf(ov) : want
  const dirty = !!ov && (want.srv !== saved.srv || want.chk !== saved.chk)
  const group = (id: Gid) => ov?.groups.find((g) => g.id === id)
  const fixedBytes = (ov?.fixed ?? []).reduce((a, f) => a + (f.bytes ?? 0), 0) + (ov?.other_bytes ?? 0)
  const proj = (id: Gid) => {
    const g = group(id)
    return g ? g.per_day * want[id] : 0
  }
  const future = proj('srv') + proj('chk') + fixedBytes
  const nowTotal = (group('srv')?.bytes ?? 0) + (group('chk')?.bytes ?? 0) + fixedBytes
  const futureTone = future > nowTotal ? 't-degraded' : 't-up'

  const go = (id: string) => {
    setSec(id)
    mainRef.current?.scrollTo({ top: 0 })
  }
  const close = () => {
    if (dirty && !window.confirm(t('Закрыть без сохранения? Изменения пропадут.'))) return
    onClose()
  }
  const setDays = (id: Gid, d: number) => {
    setMsg(null)
    setWant((w) => ({ ...w, [id]: Math.max(1, Math.min(3650, Math.round(d) || 1)) }))
  }
  async function save() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      await putRetention({ server_days: want.srv, sample_days: want.chk })
      const r = await getRetentionOverview()
      setOv(r)
      setWant(daysOf(r))
      setMsg(t('Сохранено.'))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  const fmt = (n: number | null | undefined) => (n == null ? '-' : fmtBytes(n, '0'))
  const every = ov ? (ov.prune_every % 3600 === 0 && ov.prune_every >= 3600
    ? (ov.prune_every === 3600 ? t('раз в час') : t('раз в {n} ч', { n: ov.prune_every / 3600 }))
    : t('раз в {n} мин', { n: Math.max(1, Math.round(ov.prune_every / 60)) })) : ''
  const side: Record<string, string> = ov
    ? { over: fmt(ov.db_bytes), srv: t('{n} дн.', { n: want.srv }), chk: t('{n} дн.', { n: want.chk }) }
    : {}

  // полоса размера по видам данных: сейчас или после сохранения, обе в одном масштабе - так
  // видно, насколько база вырастет или похудеет
  const bar = (after: boolean) => {
    const segs: [string, string, number][] = [
      ['srv', t(GROUP.srv.label), after ? proj('srv') : group('srv')?.bytes ?? 0],
      ['chk', t(GROUP.chk.label), after ? proj('chk') : group('chk')?.bytes ?? 0],
      ['other', t('Остальное'), fixedBytes],
    ]
    const tot = (dirty ? Math.max(nowTotal, future) : nowTotal) || 1
    return (
      <>
        <div className="rt-bar">
          {segs.map(([k, , v]) => (
            <span key={k} className={`rt-c-${k}`} style={{ width: `${((100 * v) / tot).toFixed(1)}%` }} />
          ))}
        </div>
        <div className="rt-legend small">
          {segs.map(([k, l, v]) => (
            <span key={k}>
              <i className={`rt-sw rt-c-${k}`} />
              {l} {fmt(v)}
            </span>
          ))}
        </div>
      </>
    )
  }

  const groupPage = (id: Gid, g: RetentionGroup) => {
    const w = want[id]
    const then = g.per_day * w
    // за сколько дней накоплено: у свежей установки меньше срока
    const span = Math.max(1, Math.round(g.span_days))
    const oldest = g.oldest
      ? new Date(g.oldest).toLocaleDateString([], { day: '2-digit', month: '2-digit', year: 'numeric' })
      : ''
    return (
      <div className="srv-sec">
        <div className="rt-row">
          <div className="win-switch">
            {PRESETS.map(([d, l]) => (
              <button key={d} className={`win-btn${w === d ? ' win-btn-active' : ''}`} onClick={() => setDays(id, d)}>
                {t(l)}
              </button>
            ))}
          </div>
          <label className="field rt-days">
            <span>{t('свой срок, дней')}</span>
            <input type="number" min={1} max={3650} value={w} onChange={(e) => setDays(id, Number(e.target.value))} />
          </label>
        </div>
        <p className="small rt-proj">
          {w === g.days ? (
            <span className="muted">
              {t('Сейчас {now}, прибавляется около {day} в сутки.', { now: fmt(g.bytes), day: fmt(g.per_day) })}
              {oldest && ` ${t('Самая старая запись - {d}.', { d: oldest })}`}
            </span>
          ) : then > g.bytes ? (
            <>
              {t('Сейчас {now} за {d} дн. При {w} дн. будет около', { now: fmt(g.bytes), d: span, w })}{' '}
              <b className="t-degraded">{fmt(then)}</b>
              {g.per_day > 0 &&
                `, ${t('база дорастет за {n} дн.', { n: Math.max(1, Math.ceil(w - g.span_days)) })}`}
            </>
          ) : (
            <>
              {t('Сейчас {now} за {d} дн. При {w} дн. данных станет около', { now: fmt(g.bytes), d: span, w })}{' '}
              <b className="t-up">{fmt(then)}</b>.{' '}
              {t('Лишнее удалится при ближайшей чистке, а место останется за базой под новые записи. Графики за период длиннее срока будут пустыми.')}
            </>
          )}
        </p>
        <div className="srv-nav-grp rt-cap">{t('Из чего состоит')}</div>
        <table className="sec-tbl">
          <tbody>
            {g.parts.map((p) => (
              <tr key={p.key}>
                <td>{t(PART[p.key] ?? p.key)}</td>
                <td className="num muted">{p.rows != null ? t('{n} записей', { n: p.rows.toLocaleString() }) : ''}</td>
                <td className="num">{fmt(p.bytes)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  const cur = SECS.find((s) => s.id === sec) ?? SECS[0]
  const g = sec === 'srv' || sec === 'chk' ? group(sec) : undefined
  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Хранение данных')}</h3>
          {ov && (
            <span className="muted small">
              {t('База {n}', { n: fmt(ov.db_bytes) })}
              {ov.estimated ? ` · ${t('размеры таблиц примерные')}` : ''}
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
              <h4>{t(cur.label)}</h4>
            </div>
            {!ov ? (
              err ? <p className="form-error">{err}</p> : <p className="muted small">{t('загрузка…')}</p>
            ) : (
              <>
                <p className="muted small al-lead">{t(g ? GROUP[sec as Gid].what : LEAD[cur.id])}</p>
                {cur.id === 'over' && (
                  <div className="srv-sec">
                    <div>
                      <span className="rt-big">{fmt(ov.db_bytes)}</span>{' '}
                      <span className="muted small">
                        {t('сейчас')}
                        {dirty && (
                          <>
                            {' · '}
                            {t('данных будет около')} <b className={futureTone}>{fmt(future)}</b>
                          </>
                        )}
                      </span>
                    </div>
                    {bar(false)}
                    {dirty && (
                      <>
                        <div className="srv-nav-grp rt-cap">{t('После сохранения')}</div>
                        {bar(true)}
                      </>
                    )}
                    <div className="net-tiles srv-tiles">
                      {(['srv', 'chk'] as Gid[]).map((id) => {
                        const x = group(id)
                        return (
                          <button key={id} className="srv-tile-btn" onClick={() => go(id)} title={t('к разделу')}>
                            <NetTile
                              label={t(GROUP[id].label)}
                              value={t('{n} дн.', { n: want[id] })}
                              sub={x ? t('{now}, +{day} в сутки', { now: fmt(x.bytes), day: fmt(x.per_day) }) : ''}
                            />
                          </button>
                        )
                      })}
                    </div>
                  </div>
                )}
                {g && groupPage(sec as Gid, g)}
                {cur.id === 'fix' && (
                  <div className="srv-sec">
                    <table className="sec-tbl">
                      <thead>
                        <tr>
                          <th>{t('Что')}</th>
                          <th>{t('Срок')}</th>
                          <th className="num">{t('Занимает')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {ov.fixed.map((f) => (
                          <tr key={f.key}>
                            <td>{t(FIXED[f.key] ?? f.key)}</td>
                            <td>{t('{n} дн.', { n: f.days })}</td>
                            <td className="num">{f.bytes == null ? <span className="muted">{t('в ошибках 5xx')}</span> : fmt(f.bytes)}</td>
                          </tr>
                        ))}
                        <tr>
                          <td>{t('Настройки, серверы, мониторы, журнал действий')}</td>
                          <td>{t('пока есть')}</td>
                          <td className="num">{fmt(ov.other_bytes)}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            )}
          </div>
        </div>

        <div className="al-foot">
          {err && ov ? (
            <span className="form-error small">{err}</span>
          ) : msg ? (
            <span className="form-ok small">{msg}</span>
          ) : dirty ? (
            <span className="al-dirty small">
              <span className="lk-dot lk-dot-warn" />
              {t('Есть несохраненные изменения')} · {t('данных будет около {n}', { n: fmt(future) })}
            </span>
          ) : ov ? (
            <span className="muted small">{t('Старое удаляется {every}', { every })}</span>
          ) : null}
          <span className="al-foot-btns">
            {dirty && (
              <button className="ghost" onClick={() => ov && setWant(daysOf(ov))} disabled={busy}>
                {t('Отмена')}
              </button>
            )}
            <button onClick={save} disabled={!dirty || busy}>
              {busy ? t('…') : t('Сохранить')}
            </button>
          </span>
        </div>
      </div>
    </div>
  )
}
