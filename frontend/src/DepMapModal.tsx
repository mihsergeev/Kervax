import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ApiError, getDepMap, type DepMap, type DepMapCheck, type DepMapServer } from './api'
import { CardNav, type SecDef, type SecInfo } from './cardLayout'
import { useI18n } from './i18n'
import { NetTile } from './NetLinks'
import { sinceShort } from './serverUtils'
import type { Section } from './App'

// Карта зависимостей (backend api/depmap.py): что на каком сервере держится. Раскладка как у
// карточки сервера: сводка, у каждого сервера, на котором что-то держится, свой раздел с его
// мониторами, отдельно мониторы без сервера и серверы без связей

type Props = {
  onClose: () => void
  onUnauthorized: () => void
  // sub - раздел карточки: deps - там задается, от кого объект зависит
  onOpen: (section: Section, id: number, sub?: string) => void
}

// с какого размера списка в разделе нужен фильтр
const FILTER_FROM = 12
// сколько совпадений поиска показывать на сводке
const FOUND_MAX = 40
// почему монитор на этом сервере - в таком порядке подписи в разделе сервера
const WHY = ['local', 'site', 'ip', 'manual']
const WHY_CAP: Record<string, string> = {
  local: 'Проверяются изнутри сервера',
  site: 'Домен на этом сервере',
  ip: 'Цель - адрес сервера',
  manual: 'Привязаны вручную',
}

type Tree = {
  byId: Map<number, DepMapServer>
  kids: Map<number, number[]>
  mons: Map<number, DepMapCheck[]>
  // серверы, на которых что-то держится: больше всего держит - выше
  held: DepMapServer[]
  lonely: DepMapServer[]
  orphans: DepMapCheck[]
}

// что держится на сервере: серверы за ним (вглубь) и мониторы на них, без повторов
function under(id: number, kids: Map<number, number[]>, mons: Map<number, DepMapCheck[]>) {
  const seen = new Set<number>([id])
  const stack = [...(kids.get(id) ?? [])]
  const checks = new Map<number, DepMapCheck>()
  for (const c of mons.get(id) ?? []) checks.set(c.id, c)
  let servers = 0
  while (stack.length) {
    const k = stack.pop()!
    if (seen.has(k)) continue
    seen.add(k)
    servers += 1
    for (const c of mons.get(k) ?? []) checks.set(c.id, c)
    stack.push(...(kids.get(k) ?? []))
  }
  return { servers, checks: [...checks.values()] }
}

function build(d: DepMap): Tree & { load: Map<number, ReturnType<typeof under>> } {
  const byId = new Map(d.servers.map((s) => [s.id, s]))
  const kids = new Map<number, number[]>()
  for (const s of d.servers) {
    if (s.parent_id != null && byId.has(s.parent_id)) kids.set(s.parent_id, [...(kids.get(s.parent_id) ?? []), s.id])
  }
  const mons = new Map<number, DepMapCheck[]>()
  const orphans: DepMapCheck[] = []
  for (const c of d.checks) {
    const ps = c.parents.filter((p) => byId.has(p.id))
    if (ps.length === 0) orphans.push(c)
    for (const p of ps) mons.set(p.id, [...(mons.get(p.id) ?? []), c])
  }
  const load = new Map(d.servers.map((s) => [s.id, under(s.id, kids, mons)]))
  const size = (id: number) => (load.get(id)?.checks.length ?? 0) + (load.get(id)?.servers ?? 0)
  const held = d.servers
    .filter((s) => size(s.id) > 0)
    .sort((a, b) => size(b.id) - size(a.id) || byName(a.name, b.name))
  const lonely = d.servers.filter((s) => size(s.id) === 0 && !(s.parent_id != null && byId.has(s.parent_id)))
  return { byId, kids, mons, held, lonely, orphans, load }
}

// site-9 раньше site-10
const byName = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true })

// лежащие первыми, потом по имени
const RANK: Record<string, number> = { down: 0, degraded: 1 }
const byState = (a: DepMapCheck, b: DepMapCheck) =>
  (RANK[a.status] ?? 2) - (RANK[b.status] ?? 2) || byName(a.name, b.name)

const chkDot = (st: string) =>
  st === 'up' ? ' lk-dot-ok' : st === 'down' ? ' lk-dot-bad' : st === 'degraded' ? ' lk-dot-warn' : ''
const srvDot = (s: DepMapServer) => (!s.enabled ? '' : s.online ? ' lk-dot-ok' : ' lk-dot-bad')
const silent = (s: DepMapServer) => s.enabled && !s.online

// цель без схемы и хвостового слеша; совпадает с именем - не показываем, это повтор
function shownTarget(c: DepMapCheck): string {
  const x = c.target.replace(/^[a-z]+:\/\//i, '').replace(/\/$/, '')
  return x.toLowerCase() === c.name.toLowerCase() ? '' : x
}

export function DepMapModal({ onClose, onUnauthorized, onOpen }: Props) {
  const { t } = useI18n()
  const [data, setData] = useState<DepMap | null>(null)
  const [err, setErr] = useState('')
  const [sec, setSec] = useState('over')
  const [q, setQ] = useState('')
  const [filter, setFilter] = useState('')
  const mainRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let alive = true
    getDepMap()
      .then((r) => alive && setData(r))
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        if (alive) setErr(String((e as Error).message || e))
      })
    return () => {
      alive = false
    }
  }, [onUnauthorized])

  const tr = useMemo(() => (data ? build(data) : null), [data])

  const go = (id: string) => {
    setSec(id)
    setFilter('')
    mainRef.current?.scrollTo({ top: 0 })
  }

  const stText = (st: string) =>
    st === 'down' ? t('недоступен') : st === 'degraded' ? t('тормозит') : st === 'up' ? '' : t('нет данных')
  const stTone = (st: string) => (st === 'down' ? 't-down' : st === 'degraded' ? 't-degraded' : 'muted')
  const srvState = (s: DepMapServer) =>
    !s.enabled
      ? t('выключен')
      : s.online
        ? t('на связи')
        : sinceShort(s.last_seen, t)
          ? t('не на связи {ago}', { ago: sinceShort(s.last_seen, t) })
          : t('не на связи')

  const offState = (s: DepMapServer) => (s.enabled && s.online ? '' : srvState(s))

  const held = tr?.held ?? []
  const orphans = tr?.orphans ?? []
  const lonely = tr?.lonely ?? []
  const hasSec = (id: number) => held.some((s) => s.id === id)
  const toSrv = (s: DepMapServer) => (hasSec(s.id) ? go(`s${s.id}`) : onOpen('servers', s.id, 'deps'))

  const SECS: SecDef[] = [
    { id: 'over', label: 'Сводка', group: '' },
    ...held.map((s) => ({ id: `s${s.id}`, label: s.name, group: 'Серверы' })),
    ...(orphans.length ? [{ id: 'orph', label: 'Мониторы', group: 'Без связей' }] : []),
    ...(lonely.length ? [{ id: 'lonely', label: 'Серверы', group: 'Без связей' }] : []),
  ]
  // в меню под подписью "Без связей" коротко, в заголовке раздела полностью
  const HEAD: Record<string, string> = { orph: 'Мониторы без сервера', lonely: 'Серверы без связей' }
  const cur = SECS.find((s) => s.id === sec) ?? SECS[0]
  const curSrv = /^s\d+$/.test(cur.id) ? tr?.byId.get(Number(cur.id.slice(1))) : undefined

  // в меню - сколько лежит; сервер молчит - так и написано
  const info: Record<string, SecInfo> = {}
  const side: Record<string, string> = {}
  const stateOf = (list: DepMapCheck[]): SecInfo => {
    const down = list.filter((c) => c.status === 'down').length
    const deg = list.filter((c) => c.status === 'degraded').length
    return { level: down ? 2 : deg ? 1 : 0, n: down || deg, line: '' }
  }
  for (const s of held) {
    const u = tr!.load.get(s.id)!
    info[`s${s.id}`] = stateOf(u.checks)
    side[`s${s.id}`] = silent(s) ? t('молчит') : String(u.checks.length + u.servers)
  }
  info.orph = stateOf(orphans)
  side.orph = String(orphans.length)
  side.lonely = String(lonely.length)

  const needle = (cur.id === 'over' ? q : filter).trim().toLowerCase()
  const hitC = (c: DepMapCheck) => c.name.toLowerCase().includes(needle) || c.target.toLowerCase().includes(needle)
  const filterBox = (n: number, ph: string) =>
    n >= FILTER_FROM && (
      <input
        className="checks-search modal-search"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder={t(ph)}
      />
    )

  const holds = (id: number) => {
    const u = tr!.load.get(id)!
    return [
      u.checks.length > 0 && t('мониторов: {n}', { n: u.checks.length }),
      u.servers > 0 && t('серверов за ним: {n}', { n: u.servers }),
    ]
      .filter(Boolean)
      .join(', ')
  }

  const srvLink = (s: DepMapServer) => (
    <span className="ans-node">
      <span className={`lk-dot${srvDot(s)}`} />
      <button className="linklike" onClick={() => toSrv(s)}>
        {s.name}
      </button>
    </span>
  )

  const monRows = (list: DepMapCheck[], sub?: string) =>
    [...list].sort(byState).map((c) => {
      const tg = shownTarget(c)
      return (
        <tr key={c.id}>
          <td>
            <span className="ans-node">
              <span className={`lk-dot${chkDot(c.status)}`} />
              <button className="linklike" onClick={() => onOpen('sites', c.id, sub)}>
                {c.name}
              </button>
            </span>
          </td>
          <td className="hide-m">{tg && <span className="mono muted small dm-tgt" title={c.target}>{tg}</span>}</td>
          <td className="small nowrap">
            <span className={stTone(c.status)}>{stText(c.status)}</span>
          </td>
        </tr>
      )
    })

  const monTable = (list: DepMapCheck[], sub?: string) => (
    <table className="sec-tbl dm-mons">
      <tbody>{monRows(list, sub)}</tbody>
    </table>
  )

  // Сводка: что ляжет вместе с каким сервером, плитками - что без связей
  const overview = () => {
    const linked = (data?.checks.length ?? 0) - orphans.length
    const behind = (data?.servers ?? []).filter((s) => s.parent_id != null && tr!.byId.has(s.parent_id))
    const off = orphans.filter((c) => c.mode === 'off').length
    const anyKids = held.some((s) => tr!.load.get(s.id)!.servers > 0)
    const anyDown = held.some((s) => info[`s${s.id}`].n > 0)
    const anyGroup = held.some((s) => s.group)
    // плитка ведет в раздел, когда он есть: пустые разделы в меню не показываем
    const tile = (id: string, el: ReactNode) =>
      SECS.some((x) => x.id === id) ? (
        <button className="srv-tile-btn" onClick={() => go(id)} title={t('к разделу')}>
          {el}
        </button>
      ) : (
        el
      )
    return (
      <>
        <p className="muted small al-lead">
          {t('Те же связи, по которым панель придерживает алерты: замолчал сервер - мониторы и серверы за ним не шумят по отдельности, а перечислены в его сообщении. Сервер монитора панель находит сама, когда он однозначен, иначе его задают в карточке монитора; шлюз сервера - в карточке сервера.')}
        </p>
        <div className="net-tiles srv-tiles">
          <NetTile
            label={t('Серверы, на которых что-то держится')}
            value={String(held.length)}
            sub={t('мониторов на них: {n}', { n: linked })}
          />
          {tile(
            'orph',
            <NetTile
              label={t('Мониторы без сервера')}
              value={String(orphans.length)}
              sub={
                off
                  ? t('сервер не найден: {a}, отвязаны вручную: {b}', { a: orphans.length - off, b: off })
                  : t('алерт по каждому придет отдельно')
              }
            />,
          )}
          {tile(
            'lonely',
            <NetTile label={t('Серверы без связей')} value={String(lonely.length)} sub={t('на них ничего не держится')} />,
          )}
          {behind.length > 0 && (
            <NetTile
              label={t('Серверы за шлюзом')}
              value={String(behind.length)}
              sub={behind.map((s) => s.name).join(', ')}
            />
          )}
        </div>
        {held.length === 0 ? (
          <p className="muted small">{t('Связей пока нет: ни один монитор не привязан к серверу.')}</p>
        ) : (
          <>
            <div className="srv-nav-grp rt-cap">{t('Что ляжет вместе с сервером')}</div>
            <table className="sec-tbl">
              <thead>
                <tr>
                  <th>{t('Сервер')}</th>
                  {anyGroup && <th className="hide-m">{t('Группа')}</th>}
                  <th className="num">{t('Мониторы')}</th>
                  {anyKids && <th className="num hide-m">{t('Серверы за ним')}</th>}
                  {anyDown && <th className="num">{t('Лежат')}</th>}
                </tr>
              </thead>
              <tbody>
                {held.map((s) => {
                  const u = tr!.load.get(s.id)!
                  const down = u.checks.filter((c) => c.status === 'down').length
                  return (
                    <tr key={s.id}>
                      <td>
                        {srvLink(s)}
                        {silent(s) && <span className="t-down small"> · {srvState(s)}</span>}
                      </td>
                      {anyGroup && <td className="muted small hide-m">{s.group}</td>}
                      <td className="num">{u.checks.length || ''}</td>
                      {anyKids && <td className="num hide-m">{u.servers || ''}</td>}
                      {anyDown && <td className="num t-down">{down || ''}</td>}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </>
        )}
      </>
    )
  }

  // Поиск на сводке: где находится сервер или монитор
  const found = () => {
    const srv = (data?.servers ?? []).filter((s) => s.name.toLowerCase().includes(needle))
    const chk = (data?.checks ?? []).filter(hitC).sort(byState)
    const rows = [
      ...srv.map((s) => ({ key: `s${s.id}`, s, c: undefined as DepMapCheck | undefined })),
      ...chk.map((c) => ({ key: `c${c.id}`, s: undefined as DepMapServer | undefined, c })),
    ]
    const srvWhere = (s: DepMapServer) => {
      const h = hasSec(s.id) ? holds(s.id) : ''
      const parent = s.parent_id != null ? tr!.byId.get(s.parent_id) : undefined
      if (!h && !parent) return <span className="muted">{t('ни с чем не связан')}</span>
      return (
        <>
          {h && (
            <button className="linklike" onClick={() => go(`s${s.id}`)}>
              {h}
            </button>
          )}
          {h && parent && ' · '}
          {parent && (
            <>
              {t('стоит за')}{' '}
              <button className="linklike" onClick={() => toSrv(parent)}>
                {parent.name}
              </button>
            </>
          )}
        </>
      )
    }
    if (!rows.length) return <p className="muted small">{t('Ничего не нашлось.')}</p>
    return (
      <>
        <table className="sec-tbl">
          <tbody>
            {rows.slice(0, FOUND_MAX).map(({ key, s, c }) => (
              <tr key={key}>
                <td>
                  {s
                    ? srvLink(s)
                    : c && (
                        <span className="ans-node">
                          <span className={`lk-dot${chkDot(c.status)}`} />
                          <button className="linklike" onClick={() => onOpen('sites', c.id)}>
                            {c.name}
                          </button>
                        </span>
                      )}
                </td>
                <td className="small">
                  {s ? (
                    srvWhere(s)
                  ) : c && c.parents.length ? (
                    c.parents.map((p, i) => {
                      const ps = tr!.byId.get(p.id)
                      return (
                        ps && (
                          <span key={p.id}>
                            {i > 0 && ', '}
                            {t('на')}{' '}
                            <button className="linklike" onClick={() => go(`s${p.id}`)}>
                              {ps.name}
                            </button>
                          </span>
                        )
                      )
                    })
                  ) : (
                    <button className="linklike muted" onClick={() => go('orph')}>
                      {c?.mode === 'off' ? t('отвязан вручную') : t('сервер не найден')}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length > FOUND_MAX && (
          <p className="muted small">{t('И еще {n} - уточните поиск.', { n: rows.length - FOUND_MAX })}</p>
        )}
      </>
    )
  }

  // Раздел сервера: его состояние, серверы за ним и мониторы, сгруппированные по тому, почему они на нем
  const server = (s: DepMapServer) => {
    const own = tr!.mons.get(s.id) ?? []
    const kids = (tr!.kids.get(s.id) ?? []).map((k) => tr!.byId.get(k)!).filter(Boolean)
    const parent = s.parent_id != null ? tr!.byId.get(s.parent_id) : undefined
    const list = own.filter((c) => !needle || hitC(c))
    const whyOf = (c: DepMapCheck) => c.parents.find((p) => p.id === s.id)?.why ?? 'manual'
    return (
      <>
        <p className="muted small al-lead">
          {t('Замолчит сервер - то, что ниже, не пришлет каждый свой алерт: оно перечислено в его сообщении, а свой алерт придет только от того, что не поднимется вместе с сервером.')}
        </p>
        {silent(s) && (
          <div className="al-note al-note-bad">
            <span>{t('Сервер {state}: алерты того, что на нем держится, ждут его.', { state: srvState(s) })}</span>
          </div>
        )}
        <div className="ans-kv small">
          {s.group && (
            <>
              <span className="muted">{t('Группа')}</span>
              <span>{s.group}</span>
            </>
          )}
          {parent && (
            <>
              <span className="muted">{t('Стоит за')}</span>
              <span>{srvLink(parent)}</span>
            </>
          )}
          <span className="muted">{t('Держится')}</span>
          <span>{holds(s.id)}</span>
        </div>
        {kids.length > 0 && (
          <>
            <div className="srv-nav-grp rt-cap">{t('Серверы за ним · {n}', { n: kids.length })}</div>
            <table className="sec-tbl">
              <tbody>
                {kids.map((k) => (
                  <tr key={k.id}>
                    <td>{srvLink(k)}</td>
                    <td className="muted small hide-m">{hasSec(k.id) && holds(k.id)}</td>
                    <td className="small nowrap">
                      <span className={silent(k) ? 't-down' : 'muted'}>{offState(k)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
        {own.length > 0 && filterBox(own.length, 'Фильтр: имя или адрес монитора…')}
        {own.length > 0 && !list.length && <p className="muted small">{t('Ничего не нашлось.')}</p>}
        {WHY.map((w) => {
          const part = list.filter((c) => whyOf(c) === w)
          return (
            part.length > 0 && (
              <div key={w}>
                <div className="srv-nav-grp rt-cap">{t(WHY_CAP[w])} · {part.length}</div>
                {monTable(part)}
              </div>
            )
          )
        })}
      </>
    )
  }

  const orphanSec = () => {
    const list = orphans.filter((c) => !needle || hitC(c))
    const auto = list.filter((c) => c.mode !== 'off')
    const off = list.filter((c) => c.mode === 'off')
    return (
      <>
        <p className="muted small al-lead">
          {t('Алерт по этим мониторам приходит сам по себе, даже если молчит сервер, на котором они работают. Сервер задается в карточке монитора, в разделе Зависимости - туда и ведет имя.')}
        </p>
        {filterBox(orphans.length, 'Фильтр: имя или адрес монитора…')}
        {!list.length && <p className="muted small">{t('Ничего не нашлось.')}</p>}
        {auto.length > 0 && (
          <>
            <div className="srv-nav-grp rt-cap">{t('Панель не нашла сервер · {n}', { n: auto.length })}</div>
            {monTable(auto, 'deps')}
          </>
        )}
        {off.length > 0 && (
          <>
            <div className="srv-nav-grp rt-cap">{t('Отвязаны вручную · {n}', { n: off.length })}</div>
            {monTable(off, 'deps')}
          </>
        )}
      </>
    )
  }

  const lonelySec = () => {
    const list = lonely.filter((s) => !needle || s.name.toLowerCase().includes(needle))
    const anyGroup = list.some((s) => s.group)
    return (
      <>
        <p className="muted small al-lead">
          {t('Ни один монитор не привязан к этим серверам, и за ними никто не стоит: замолчит такой сервер - придет только его алерт.')}
        </p>
        {filterBox(lonely.length, 'Фильтр: имя сервера…')}
        {!list.length ? (
          <p className="muted small">{t('Ничего не нашлось.')}</p>
        ) : (
          <table className="sec-tbl">
            <tbody>
              {list.map((s) => (
                <tr key={s.id}>
                  <td>{srvLink(s)}</td>
                  {anyGroup && <td className="muted small hide-m">{s.group}</td>}
                  <td className="small nowrap">
                    <span className={silent(s) ? 't-down' : 'muted'}>{offState(s)}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </>
    )
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Карта зависимостей')}</h3>
          {data && (
            <span className="muted small">
              {t('серверов: {s}, мониторов: {c}', { s: data.servers.length, c: data.checks.length })}
            </span>
          )}
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>

        <div className="al-body">
          <CardNav secs={SECS} active={cur.id} info={info} side={side} onGo={go} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>
                {curSrv ? curSrv.name : t(HEAD[cur.id] ?? cur.label)}{' '}
                {curSrv && !curSrv.enabled && <span className="type-chip">{t('выключен')}</span>}
              </h4>
              {cur.id === 'over' && tr && (
                <div className="modal-toolbar srv-head-tools">
                  <input
                    className="checks-search modal-search"
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    placeholder={t('найти сервер или монитор')}
                  />
                </div>
              )}
              {curSrv && (
                <button className="ghost small srv-head-btn" onClick={() => onOpen('servers', curSrv.id, 'deps')}>
                  {t('Карточка сервера')} ›
                </button>
              )}
            </div>
            {!tr ? (
              err ? <p className="form-error">{err}</p> : <p className="muted small">{t('загрузка…')}</p>
            ) : (
              <div className="srv-sec">
                {cur.id === 'over' && (needle ? found() : overview())}
                {curSrv && server(curSrv)}
                {cur.id === 'orph' && orphanSec()}
                {cur.id === 'lonely' && lonelySec()}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
