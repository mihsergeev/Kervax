import { useEffect, useMemo, useState } from 'react'
import { ApiError, getDepMap, type DepMap, type DepMapCheck, type DepMapServer } from './api'
import { useI18n } from './i18n'
import type { Section } from './App'

// Карта зависимостей (backend api/depmap.py): кто за кем стоит и какие мониторы на каком
// сервере. Отдельная страница из меню - смотрят, когда надо понять, что ляжет вместе с чем.

type Props = {
  onClose: () => void
  onUnauthorized: () => void
  onOpen: (section: Section, id: number) => void
}

// сколько мониторов показывать в карточке сразу, остальное по кнопке
const CHIPS = 10

type Tree = {
  byId: Map<number, DepMapServer>
  kids: Map<number, number[]>
  mons: Map<number, DepMapCheck[]>
  roots: number[]
  lonely: DepMapServer[]
  orphans: DepMapCheck[]
}

function build(d: DepMap): Tree {
  const byId = new Map(d.servers.map((s) => [s.id, s]))
  const kids = new Map<number, number[]>()
  for (const s of d.servers) {
    if (s.parent_id != null && byId.has(s.parent_id)) {
      kids.set(s.parent_id, [...(kids.get(s.parent_id) ?? []), s.id])
    }
  }
  const mons = new Map<number, DepMapCheck[]>()
  const orphans: DepMapCheck[] = []
  for (const c of d.checks) {
    const ps = c.parents.filter((p) => byId.has(p.id))
    if (ps.length === 0) orphans.push(c)
    for (const p of ps) mons.set(p.id, [...(mons.get(p.id) ?? []), c])
  }
  const roots: number[] = []
  const lonely: DepMapServer[] = []
  for (const s of d.servers) {
    if (s.parent_id != null && byId.has(s.parent_id)) continue
    if (!(kids.get(s.id)?.length || mons.get(s.id)?.length)) lonely.push(s)
    else roots.push(s.id)
  }
  return { byId, kids, mons, roots, lonely, orphans }
}

// что держится на сервере: серверы за ним (вглубь) и мониторы на них, без повторов
function under(id: number, tr: Tree): { servers: number; checks: DepMapCheck[] } {
  const seen = new Set<number>([id])
  const stack = [...(tr.kids.get(id) ?? [])]
  const checks = new Map<number, DepMapCheck>()
  for (const c of tr.mons.get(id) ?? []) checks.set(c.id, c)
  let servers = 0
  while (stack.length) {
    const k = stack.pop()!
    if (seen.has(k)) continue
    seen.add(k)
    servers += 1
    for (const c of tr.mons.get(k) ?? []) checks.set(c.id, c)
    stack.push(...(tr.kids.get(k) ?? []))
  }
  return { servers, checks: [...checks.values()] }
}

export function DepMapModal({ onClose, onUnauthorized, onOpen }: Props) {
  const { t } = useI18n()
  const [data, setData] = useState<DepMap | null>(null)
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [open, setOpen] = useState<Set<string>>(() => new Set())

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
  const needle = q.trim().toLowerCase()
  const hit = (name: string) => !!needle && name.toLowerCase().includes(needle)

  // есть ли совпадение в узле или под ним - иначе при поиске прячем
  const matches = (id: number, depth = 0): boolean => {
    if (!tr || !needle) return true
    const s = tr.byId.get(id)
    if (!s || depth > 20) return false
    if (hit(s.name) || (tr.mons.get(id) ?? []).some((c) => hit(c.name))) return true
    return (tr.kids.get(id) ?? []).some((k) => matches(k, depth + 1))
  }

  const toggle = (key: string) =>
    setOpen((prev) => {
      const n = new Set(prev)
      if (n.has(key)) n.delete(key)
      else n.add(key)
      return n
    })

  const whyText = (why: string) =>
    ({
      local: t('проверяется изнутри этого сервера'),
      site: t('домен обслуживает этот сервер'),
      ip: t('цель - адрес этого сервера'),
      manual: t('задано вручную'),
    })[why] ?? why

  const chip = (c: DepMapCheck, sid?: number) => {
    const why = sid != null ? c.parents.find((p) => p.id === sid)?.why : undefined
    return (
      <button
        key={c.id}
        className={`dm-chip dm-st-${c.status}${hit(c.name) ? ' dm-hit' : ''}`}
        title={why ? whyText(why) : undefined}
        onClick={() => onOpen('sites', c.id)}
      >
        <span className="dm-dot" />
        {c.name}
      </button>
    )
  }

  const chips = (list: DepMapCheck[], key: string, sid?: number) => {
    // лежащие и совпавшие с поиском - первыми, чтобы не прятались за "еще"
    const sorted = [...list].sort(
      (a, b) =>
        Number(b.status === 'down') - Number(a.status === 'down') || Number(hit(b.name)) - Number(hit(a.name)),
    )
    const all = open.has(key)
    const shown = all ? sorted : sorted.slice(0, CHIPS)
    return (
      <div className="dm-chips">
        {shown.map((c) => chip(c, sid))}
        {sorted.length > CHIPS && (
          <button className="ghost small dm-more" onClick={() => toggle(key)}>
            {all ? t('свернуть') : t('еще {n}', { n: sorted.length - CHIPS })}
          </button>
        )}
      </div>
    )
  }

  const node = (id: number, depth: number, trail: Set<number>) => {
    if (!tr) return null
    const s = tr.byId.get(id)
    if (!s || trail.has(id) || !matches(id)) return null
    const next = new Set(trail).add(id)
    const u = under(id, tr)
    const down = u.checks.filter((c) => c.status === 'down').length
    const kidsList = tr.kids.get(id) ?? []
    const mons = tr.mons.get(id) ?? []
    const state = !s.enabled ? 'off' : s.online ? 'up' : 'down'
    return (
      <div key={id} className={`dm-node${depth === 0 ? ' dm-root' : ''}`}>
        <div className="dm-head">
          <span className={`dm-dot dm-srv-${state}`} title={state === 'down' ? t('не на связи') : undefined} />
          <button className={`dm-name${hit(s.name) ? ' dm-hit' : ''}`} onClick={() => onOpen('servers', id)}>
            {s.name}
          </button>
          {s.group && <span className="muted small">{s.group}</span>}
        </div>
        {(u.servers > 0 || u.checks.length > 0) && (
          <div className="dm-impact small">
            {u.servers > 0 && u.checks.length > 0
              ? t('на нем держатся серверы ({s}) и мониторы ({c})', { s: u.servers, c: u.checks.length })
              : u.servers > 0
                ? t('на нем держатся серверы: {n}', { n: u.servers })
                : t('на нем держатся мониторы: {n}', { n: u.checks.length })}
            {down > 0 && <span className="t-down"> · {t('лежат сейчас: {n}', { n: down })}</span>}
          </div>
        )}
        {mons.length > 0 && chips(mons, `s${id}`, id)}
        {kidsList.length > 0 && <div className="dm-kids">{kidsList.map((k) => node(k, depth + 1, next))}</div>}
      </div>
    )
  }

  // сначала цепочки (у кого есть серверы за спиной), потом по числу мониторов
  const roots = tr
    ? [...tr.roots].sort((a, b) => {
        const ua = under(a, tr)
        const ub = under(b, tr)
        return ub.servers - ua.servers || ub.checks.length - ua.checks.length
      })
    : []
  const lonely = tr ? tr.lonely.filter((s) => !needle || hit(s.name)) : []
  const orphans = tr ? tr.orphans.filter((c) => !needle || hit(c.name)) : []

  return (
    <div className="modal-backdrop">
      <div className="card modal dm-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Карта зависимостей')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="muted small">
          {t(
            'Те же связи, по которым панель придерживает алерты: замолчал сервер - его мониторы и серверы за ним не шумят отдельно, а названы в его сообщении. Сервер за шлюзом задается в карточке сервера, сервер монитора - в карточке монитора.',
          )}
        </div>
        <div className="modal-toolbar">
          <input
            className="dm-search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t('найти сервер или монитор')}
          />
          <span className="dm-legend small muted">
            <span className="dm-leg">
              <span className="dm-dot dm-srv-up" /> {t('на связи')}
            </span>
            <span className="dm-leg">
              <span className="dm-dot dm-srv-down" /> {t('молчит')}
            </span>
            <span className="dm-leg">
              <span className="dm-dot dm-st-down-dot" /> {t('лежит')}
            </span>
          </span>
        </div>
        {err && <p className="form-error">{err}</p>}
        {!data && !err && <p className="muted small">{t('загрузка…')}</p>}
        {tr && roots.length === 0 && !needle && (
          <p className="muted small">{t('Связей пока нет: ни один монитор не привязан к серверу.')}</p>
        )}
        {tr && <div className="dm-grid">{roots.map((id) => node(id, 0, new Set()))}</div>}
        {orphans.length > 0 && (
          <section className="rep-sec">
            <button className="ghost small dm-sec-btn" onClick={() => toggle('orphans')}>
              {open.has('orphans') ? '▾' : '▸'} {t('Мониторы без сервера: {n}', { n: orphans.length })}
            </button>
            {(open.has('orphans') || !!needle) && chips(orphans, 'orph')}
          </section>
        )}
        {lonely.length > 0 && (
          <section className="rep-sec">
            <button className="ghost small dm-sec-btn" onClick={() => toggle('lonely')}>
              {open.has('lonely') ? '▾' : '▸'} {t('Серверы без связей: {n}', { n: lonely.length })}
            </button>
            {(open.has('lonely') || !!needle) && (
              <div className="dm-chips">
                {lonely.map((s) => (
                  <button
                    key={s.id}
                    className={`dm-chip${hit(s.name) ? ' dm-hit' : ''}`}
                    onClick={() => onOpen('servers', s.id)}
                  >
                    <span className={`dm-dot dm-srv-${!s.enabled ? 'off' : s.online ? 'up' : 'down'}`} />
                    {s.name}
                  </button>
                ))}
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  )
}
