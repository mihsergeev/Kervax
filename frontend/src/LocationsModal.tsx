import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  createLocation,
  deleteLocation,
  getLocationsOverview,
  listLocations,
  testProxy,
  testSavedLocation,
  updateLocation,
  type Location,
  type LocationsOverview,
  type LocationStat,
  type LocSiteCell,
  type ProxyTestResult,
} from './api'
import { CardNav, type SecDef, type SecInfo } from './cardLayout'
import { CountryFlag } from './CountryFlag'
import { useI18n } from './i18n'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
  onOpenCheck?: (id: number) => void
}

// Локации как карточка сервера: слева "Где недоступно" и сами локации, справа одна. У локации
// видно, как она проверяет сейчас и за сутки, какие сайты недоступны только из нее и не сломан
// ли сам прокси; ниже ее настройки, "Сохранить" внизу окна. Стили - al-* (общие с алертами) и lc-*

type Form = { name: string; url: string; monitors: boolean; enabled: boolean }
const EMPTY: Form = { name: '', url: '', monitors: true, enabled: true }
const formOf = (l: Location): Form => ({ name: l.name, url: l.url, monitors: l.monitors !== false, enabled: l.enabled })
const key = (x: unknown) => JSON.stringify(x)

// http://user:pass@host -> http://***@host: в заголовке пароль ни к чему
function masked(url: string): string {
  const [scheme, rest] = url.includes('://') ? url.split('://', 2) : ['', url]
  const at = rest.lastIndexOf('@')
  return at < 0 ? url : `${scheme}://***@${rest.slice(at + 1)}`
}

function kindOf(url: string): string {
  const u = url.trim().toLowerCase()
  if (!u) return 'напрямую, с самой панели'
  if (u.startsWith('socks')) return 'SOCKS5-прокси'
  return u.startsWith('https') ? 'HTTPS-прокси' : 'HTTP-прокси'
}

const isDown = (c?: LocSiteCell) => c?.status === 'down'
const isOk = (c?: LocSiteCell) => c?.status === 'up' || c?.status === 'degraded'

export function LocationsModal({ onClose, onUnauthorized, onOpenCheck }: Props) {
  const { t } = useI18n()
  const [locs, setLocs] = useState<Location[] | null>(null)
  const [ov, setOv] = useState<LocationsOverview | null>(null)
  const [sec, setSec] = useState('where')
  const [f, setF] = useState<Form>(EMPTY)
  const [test, setTest] = useState<ProxyTestResult | null>(null)
  const [testing, setTesting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const mainRef = useRef<HTMLDivElement>(null)
  const exitAsked = useRef(false)

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    },
    [onUnauthorized, t],
  )

  const reload = useCallback(async () => {
    const [l, o] = await Promise.all([listLocations(), getLocationsOverview()])
    setLocs(l)
    setOv(o)
    return { l, o }
  }, [])

  const cur = locs?.find((l) => `l${l.id}` === sec) ?? null
  const base = cur ? formOf(cur) : EMPTY
  const dirty = sec !== 'where' && key(f) !== key(base)

  // открыть раздел: форма - из локации, проверка прокси - заново
  const open = (id: string, all: Location[] | null = locs, force = false) => {
    if (!force && dirty && !window.confirm(t('Уйти без сохранения? Изменения пропадут.'))) return
    const l = all?.find((x) => `l${x.id}` === id)
    setSec(id)
    setF(l ? formOf(l) : EMPTY)
    setTest(null)
    setErr(null)
    setMsg(null)
    mainRef.current?.scrollTo(0, 0)
  }

  useEffect(() => {
    reload()
      .then(({ l, o }) => {
        const probes = l.some((x) => x.url.trim() && x.monitors && x.enabled)
        const first = o.sites.length && probes ? 'where' : l.length ? `l${l[0].id}` : 'add'
        setSec(first)
        const fl = l.find((x) => `l${x.id}` === first)
        setF(fl ? formOf(fl) : EMPTY)
        // где локации выходят в интернет, панель узнает один раз и запоминает
        if (exitAsked.current) return
        exitAsked.current = true
        ;(async () => {
          for (const x of l.filter((y) => y.enabled && !y.exit_at)) {
            try {
              const r = await testSavedLocation(x.id)
              setLocs((all) =>
                all ? all.map((y) => (y.id === x.id ? { ...y, exit_ip: r.exit_ip, exit_country: r.exit_country, exit_at: new Date().toISOString() } : y)) : all,
              )
            } catch {
              // не узнали - не беда, это подсказка
            }
          }
        })()
      })
      .catch(fail)
  }, [reload, fail])

  const set = (p: Partial<Form>) => {
    setF((x) => ({ ...x, ...p }))
    setMsg(null)
    if ('url' in p) setTest(null) // адрес изменился - прошлая проверка неактуальна
  }

  const trimmed = f.url.trim()
  const urlChanged = !cur || trimmed !== cur.url.trim()
  // сохраняем прокси, только если он доступен; неизмененный адрес уже был сохранен рабочим
  const verified = trimmed === '' || !urlChanged || (test != null && test.ok)
  const canSave = !busy && f.name.trim() !== '' && verified && (sec === 'add' || dirty)

  async function testNow() {
    setTesting(true)
    setErr(null)
    try {
      if (cur && !urlChanged) {
        const r = await testSavedLocation(cur.id)
        setTest(r)
        setLocs((all) => all && all.map((y) => (y.id === cur.id ? { ...y, exit_ip: r.exit_ip, exit_country: r.exit_country, exit_at: new Date().toISOString() } : y)))
      } else setTest(await testProxy(trimmed))
    } catch (e) {
      fail(e)
    } finally {
      setTesting(false)
    }
  }

  async function save() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    const exit = urlChanged && test?.ok && trimmed ? { exit_ip: test.exit_ip, exit_country: test.exit_country } : {}
    try {
      if (cur) {
        await updateLocation(cur.id, { ...f, ...exit })
        const { l } = await reload()
        open(`l${cur.id}`, l, true)
        setMsg(t('Сохранено.'))
      } else {
        const made = await createLocation({ name: f.name, url: f.url, ...exit })
        const { l } = await reload()
        open(`l${made.id}`, l, true)
        setMsg(t('Локация добавлена.'))
      }
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    if (!cur || !window.confirm(t('Удалить локацию «{name}»?', { name: cur.name }))) return
    try {
      await deleteLocation(cur.id)
      const { l } = await reload()
      open(l.length ? 'where' : 'add', l, true)
    } catch (e) {
      fail(e)
    }
  }

  const close = () => {
    if (dirty && !window.confirm(t('Закрыть без сохранения? Изменения пропадут.'))) return
    onClose()
  }

  const stat = (id: number): LocationStat | undefined => ov?.locations.find((s) => s.id === id)
  const broken = new Set((ov?.locations ?? []).filter((s) => s.broken).map((s) => s.id))
  // столбцы таблицы и "только отсюда": локации-прокси, через которые проверяются сайты
  const probes = (locs ?? []).filter((l) => l.url.trim() && l.monitors && l.enabled)
  const direct = (locs ?? []).find((l) => !l.url.trim())

  const secs: SecDef[] = [
    { id: 'where', label: 'Где недоступно', group: '' },
    ...(locs ?? []).map((l) => ({ id: `l${l.id}`, label: l.name, group: 'Локации' })),
    { id: 'add', label: '+ Добавить локацию', group: 'Локации' },
  ]
  const info: Record<string, SecInfo> = {}
  if (ov) {
    const n = ov.sites.length
    info.where = { level: n ? 1 : 0, n, line: '' }
    for (const l of locs ?? []) {
      const s = stat(l.id)
      if (!s || !l.enabled) continue
      info[`l${l.id}`] = s.broken
        ? { level: 2, n: s.checks, line: '' }
        : { level: s.down ? (l.url.trim() ? 1 : 2) : 0, n: s.down, line: '' }
    }
  }

  const title =
    sec === 'where' ? t('Где недоступно') : sec === 'add' ? t('Новая локация') : cur ? cur.name : ''

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Локации')}</h3>
          <span className="muted small lc-sub">{t('Откуда панель проверяет сайты и порты нод')}</span>
          <button className="ghost" onClick={close}>
            {t('Закрыть')}
          </button>
        </div>

        <div className="al-body">
          <CardNav secs={secs} active={sec} info={info} onGo={(id) => open(id)} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>
                {title}
                {cur && <CountryFlag code={cur.exit_country} />}
              </h4>
            </div>
            {!locs || !ov ? (
              err ? <p className="form-error">{err}</p> : <p className="muted small">{t('загрузка…')}</p>
            ) : sec === 'where' ? (
              <WhereDown
                ov={ov}
                probes={probes}
                direct={direct}
                broken={broken}
                onGo={(id) => open(`l${id}`)}
                onOpenCheck={onOpenCheck}
              />
            ) : (
              <div className="srv-sec">
                {cur && (
                  <LocState
                    loc={cur}
                    stat={stat(cur.id)}
                    ov={ov}
                    probes={probes}
                    broken={broken}
                    ports={ov.ports_location_id === cur.id}
                    testing={testing}
                    onTest={testNow}
                    onOpenCheck={onOpenCheck}
                  />
                )}
                {sec === 'add' && (
                  <p className="muted small al-lead">
                    {t('Нужны только название и адрес. Прокси проверяется сразу, сохранить можно только рабочий.')}
                  </p>
                )}
                <div className="al-box">
                  {cur && <div className="al-box-head"><b>{t('Настройки')}</b></div>}
                  <div className="form-grid">
                    <label className="field">
                      <span>{t('Название')}</span>
                      <input value={f.name} onChange={(e) => set({ name: e.target.value })} placeholder={t('напр. Германия')} />
                    </label>
                    <label className="field">
                      <span>{t('Адрес прокси')}</span>
                      <div className="pass-row">
                        <input
                          className="mono"
                          value={f.url}
                          onChange={(e) => set({ url: e.target.value })}
                          onBlur={() => trimmed && urlChanged && !test && !testing && testNow()}
                          placeholder={t('socks5://… (пусто = напрямую)')}
                        />
                        <button className="ghost" onClick={testNow} disabled={testing || busy || (!trimmed && !cur)}>
                          {testing ? t('Проверяю…') : t('Проверить')}
                        </button>
                      </div>
                    </label>
                  </div>
                  {test ? (
                    <p className={`small lc-exit ${test.ok ? 'form-ok' : 'form-error'}`}>
                      {test.ok ? '✓ ' : '✗ '}
                      {test.message}
                      {test.ok && test.exit_ip && (
                        <>
                          {' · '}
                          {t('выход в интернет:')} <CountryFlag code={test.exit_country} /> <span className="mono">{test.exit_ip}</span>
                        </>
                      )}
                    </p>
                  ) : cur && !urlChanged && cur.exit_ip ? (
                    <p className="small muted lc-exit">
                      {t('Выход в интернет:')} <CountryFlag code={cur.exit_country} /> <span className="mono">{cur.exit_ip}</span>
                    </p>
                  ) : null}
                  {trimmed !== '' && urlChanged && !test?.ok && !testing && (
                    <p className="muted small">{t('Проверьте прокси — сохранить можно только доступный.')}</p>
                  )}
                  <p className="muted small">
                    {trimmed
                      ? t('HTTP, HTTPS или SOCKS5, логин и пароль можно в адресе: socks5://user:pass@host:1080.')
                      : t('Пустой адрес - проверка с самой панели, это основная проверка всех мониторов.')}
                  </p>
                  {cur && (
                    <>
                      <label className="check-toggle">
                        <input type="checkbox" checked={f.monitors} onChange={(e) => set({ monitors: e.target.checked })} />
                        <span>
                          <b>{t('Проверять через нее сайты')}</b>
                          <span className="muted small">
                            {t('Выключено - только порты нод снаружи: для сайтов, открытых лишь своей сети, не будет ложных "недоступен".')}
                          </span>
                        </span>
                      </label>
                      <label className="check-toggle">
                        <input type="checkbox" checked={f.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
                        <span>
                          <b>{t('Включена')}</b>
                        </span>
                      </label>
                      <div>
                        <button className="ghost small lc-del" onClick={remove}>
                          {t('Удалить локацию')}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {sec !== 'where' && (
          <div className="al-foot">
            {err ? (
              <span className="form-error small">{err}</span>
            ) : msg ? (
              <span className="form-ok small">{msg}</span>
            ) : dirty && sec !== 'add' ? (
              <span className="al-dirty small">
                <span className="lk-dot lk-dot-warn" />
                {t('Есть несохраненные изменения')}
              </span>
            ) : null}
            <span className="al-foot-btns">
              {dirty && cur && (
                <button className="ghost" onClick={() => open(sec, locs, true)} disabled={busy}>
                  {t('Отмена')}
                </button>
              )}
              <button onClick={save} disabled={!canSave}>
                {busy ? t('…') : sec === 'add' ? t('Добавить') : t('Сохранить')}
              </button>
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

// Состояние локации: что она проверяет, как за сутки и какие сайты недоступны отсюда
function LocState({
  loc,
  stat,
  ov,
  probes,
  broken,
  ports,
  testing,
  onTest,
  onOpenCheck,
}: {
  loc: Location
  stat?: LocationStat
  ov: LocationsOverview
  probes: Location[]
  broken: Set<number>
  ports: boolean
  testing: boolean
  onTest: () => void
  onOpenCheck?: (id: number) => void
}) {
  const { t } = useI18n()
  const isDirect = !loc.url.trim()
  const portsOnly = !isDirect && !loc.monitors
  // недоступные отсюда; "только отсюда" - а с панели и из остальных локаций отвечает
  const here = ov.sites.filter((s) => (isDirect ? isDown(s.main) : isDown(s.cells[loc.id])))
  const others = (s: LocationsOverview['sites'][number]) => [
    ...(isDirect ? [] : [s.main]),
    ...probes.filter((p) => p.id !== loc.id && !broken.has(p.id)).map((p) => s.cells[p.id]).filter(Boolean),
  ]
  const onlyHere = (s: LocationsOverview['sites'][number]) => {
    const o = others(s)
    return o.length > 0 && o.every(isOk)
  }
  const anyOnly = here.some(onlyHere)
  return (
    <>
      <p className="muted small al-lead">
        {t(kindOf(loc.url))}
        {loc.url.trim() && (
          <>
            {' · '}
            <span className="mono">{masked(loc.url)}</span>
          </>
        )}
      </p>
      <div className="lc-chips">
        {isDirect && <span className="type-chip">{t('основная проверка')}</span>}
        {!isDirect && loc.monitors && <span className="type-chip">{t('сайты')}</span>}
        {portsOnly && <span className="type-chip">{t('без сайтов')}</span>}
        {ports && <span className="type-chip lc-chip-acc">{t('порты нод снаружи')}</span>}
        {!loc.enabled && <span className="type-chip off">{t('выключена')}</span>}
      </div>
      {!loc.enabled ? (
        <div className="al-note al-note-warn">
          <span>{t('Локация выключена: через нее ничего не проверяется.')}</span>
        </div>
      ) : portsOnly ? (
        <div className="al-note lc-note-info">
          <span>
            {t('Эта локация только для внешней проверки портов нод: через нее панель смотрит, какие порты видны из интернета. Сайты через нее не проверяются, поэтому сайты, открытые лишь своей сети, не дают ложных "недоступен".')}
          </span>
        </div>
      ) : stat?.broken ? (
        <div className="al-note al-note-bad">
          <span>
            {t('Сломан сам прокси: отсюда недоступны все сайты ({n}), с самой панели часть из них отвечает, а сам прокси не отвечает на проверку. Вместо алертов по каждому сайту приходит один алерт про прокси. Ошибка: {err}', { n: stat.checks, err: stat.error })}
          </span>
          <button className="ghost small" onClick={onTest} disabled={testing}>
            {testing ? t('Проверяю…') : t('Проверить прокси')}
          </button>
        </div>
      ) : stat ? (
        <div className="lc-tiles">
          <div className="lc-tile">
            <b>{stat.checks}</b>
            <span>{isDirect ? t('мониторов') : t('сайтов проверяется')}</span>
          </div>
          <div className={`lc-tile${stat.up_pct != null && stat.up_pct >= 99.5 ? ' lc-tile-ok' : ''}`}>
            <b>{stat.up_pct != null ? `${stat.up_pct}%` : '—'}</b>
            <span>{t('доступно за сутки')}</span>
          </div>
          <div className="lc-tile">
            <b>{stat.avg_ms != null ? `${stat.avg_ms} ${t('мс')}` : '—'}</b>
            <span>{t('средний ответ')}</span>
          </div>
          <div className={`lc-tile${stat.down ? ' lc-tile-bad' : ''}`}>
            <b>{stat.down}</b>
            <span>{t('недоступны сейчас')}</span>
          </div>
        </div>
      ) : null}
      {loc.enabled && !portsOnly && !stat?.broken && here.length > 0 && (
        <>
          <div className="ov-cap">{t('Недоступны отсюда')}</div>
          <div className="lc-rows">
            {here.map((s) => {
              const c = isDirect ? s.main : s.cells[loc.id]
              const only = onlyHere(s)
              return (
                <div key={s.check_id} className="lc-row">
                  <span className="lk-dot lk-dot-bad" />
                  <SiteName id={s.check_id} name={s.name} group={s.group} onOpenCheck={onOpenCheck} />
                  <span className="lc-msg muted small" title={c?.message}>{c?.message}</span>
                  {others(s).length > 0 && (
                    <span className={`type-chip${only ? ' lc-chip-warn' : ''}`}>{only ? t('только отсюда') : t('и из других')}</span>
                  )}
                </div>
              )
            })}
          </div>
          {anyOnly && (
            <p className="muted small">
              {t('"Только отсюда" - из остальных мест сайт отвечает: похоже на блокировку или фильтр по стране, а не на падение.')}
            </p>
          )}
        </>
      )}
    </>
  )
}

function SiteName({ id, name, group, onOpenCheck }: { id: number; name: string; group: string; onOpenCheck?: (id: number) => void }) {
  return (
    <span className="lc-site">
      {onOpenCheck ? (
        <button className="linklike lc-link" onClick={() => onOpenCheck(id)}>
          {name}
        </button>
      ) : (
        <b>{name}</b>
      )}
      {group && <span className="muted small"> {group}</span>}
    </span>
  )
}

// Где недоступно: сайт по строкам, откуда проверяли - по столбцам. Недоступен из одной
// локации, а из других отвечает - видно сразу, что это не падение сайта
function WhereDown({
  ov,
  probes,
  direct,
  broken,
  onGo,
  onOpenCheck,
}: {
  ov: LocationsOverview
  probes: Location[]
  direct?: Location
  broken: Set<number>
  onGo: (id: number) => void
  onOpenCheck?: (id: number) => void
}) {
  const { t } = useI18n()
  const dead = probes.filter((p) => broken.has(p.id))
  const cols = probes
  // сначала недоступные лишь из части мест: это и есть то, ради чего локации
  const partial = (s: LocationsOverview['sites'][number]) => {
    const all = [s.main, ...cols.filter((p) => !broken.has(p.id)).map((p) => s.cells[p.id]).filter(Boolean)]
    return all.some(isOk) && all.some(isDown)
  }
  const rows = [...ov.sites].sort((a, b) => Number(partial(b)) - Number(partial(a)) || a.name.localeCompare(b.name))
  const cell = (c: LocSiteCell | undefined, off: boolean) => {
    if (!c) return <span className="lc-cell lc-cell-na" title={t('отсюда не проверяется')}>—</span>
    const cls = off ? 'lc-cell-na' : c.status === 'down' ? 'lc-cell-bad' : c.status === 'degraded' ? 'lc-cell-deg' : c.status === 'up' ? 'lc-cell-ok' : 'lc-cell-na'
    const label = c.status === 'down' ? t('недоступен') : c.status === 'degraded' ? t('медленно') : c.status === 'up' ? t('ок') : '?'
    return <span className={`lc-cell ${cls}`} title={c.message || undefined}>{label}</span>
  }
  return (
    <div className="srv-sec">
      <p className="muted small al-lead">
        {t('Сайты, которые сейчас недоступны хоть откуда-то: с самой панели или через локации. Недоступен из одной, а из других отвечает - похоже на блокировку по стране или фильтр, а не на падение сайта.')}
      </p>
      {dead.map((p) => (
        <div key={p.id} className="al-note al-note-bad">
          <span>{t('Прокси локации «{name}» не отвечает: ее столбец в таблице не в счет.', { name: p.name })}</span>
          <button className="ghost small" onClick={() => onGo(p.id)}>
            {t('Открыть')} ›
          </button>
        </div>
      ))}
      {!cols.length && (
        <div className="al-note lc-note-info">
          <span>{t('Через прокси пока ничего не проверяется: добавьте локацию, тогда будет видно, откуда сайт недоступен.')}</span>
        </div>
      )}
      {!rows.length ? (
        <p className="form-ok small">{t('✓ Все сайты отвечают отовсюду.')}</p>
      ) : (
        <div className="lc-mx-wrap">
          <table className="lc-mx">
            <thead>
              <tr>
                <th>{t('Сайт')}</th>
                <th>
                  {direct?.name || t('Сама панель')} <CountryFlag code={direct?.exit_country} />
                </th>
                {cols.map((p) => (
                  <th key={p.id}>
                    <button className="linklike lc-link" onClick={() => onGo(p.id)}>
                      {p.name}
                    </button>{' '}
                    <CountryFlag code={p.exit_country} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.check_id}>
                  <td>
                    <SiteName id={s.check_id} name={s.name} group={s.group} onOpenCheck={onOpenCheck} />
                  </td>
                  <td>{cell(s.main, false)}</td>
                  {cols.map((p) => (
                    <td key={p.id}>{cell(s.cells[p.id], broken.has(p.id))}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {ov.sites.length >= 300 && <p className="muted small">{t('Показаны первые 300 сайтов.')}</p>}
    </div>
  )
}
