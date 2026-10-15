import { useEffect, useState } from 'react'
import {
  checksOverview,
  listServers,
  updateCheck,
  updateServer,
  type Check,
  type Server,
} from './api'
import { useI18n } from './i18n'

// Зависимости (backend app/parents.py). Сервер замолчал - то, что на нем держится, не шумит
// отдельными алертами: оно строкой в его сообщении, а свой алерт ждет, пока сервер вернется.
// Здесь видно, от кого объект зависит, и это можно поменять.

function whyText(why: string, t: (k: string) => string): string {
  if (why === 'local') return t('проверяется изнутри него')
  if (why === 'site') return t('обслуживает домен')
  if (why === 'ip') return t('это его адрес')
  return t('задано вручную')
}

type SrvOpt = { id: number; name: string; parent_id?: number | null; online?: boolean }

function useServerOpts(active: boolean) {
  const [opts, setOpts] = useState<SrvOpt[] | null>(null)
  useEffect(() => {
    if (!active || opts) return
    listServers()
      .then((ss) => setOpts(ss.map((s) => ({ id: s.id, name: s.name, parent_id: s.parent_id, online: s.online }))))
      .catch(() => setOpts([]))
  }, [active, opts])
  return opts
}

// Монитор: от какого сервера зависит (авто, вручную или ни от какого)
export function CheckParentBar({
  check,
  canEdit,
  onSaved,
}: {
  check: Check
  canEdit: boolean
  onSaved: () => void
}) {
  const { t } = useI18n()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('auto')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const opts = useServerOpts(editing)
  const mode = check.parent_mode ?? 'auto'
  const parents = check.parents ?? []

  const start = () => {
    setDraft(mode === 'server' && check.parent_server_id ? `s${check.parent_server_id}` : mode)
    setErr('')
    setEditing(true)
  }
  const save = async () => {
    setBusy(true)
    setErr('')
    try {
      const body = draft.startsWith('s')
        ? { parent_mode: 'server' as const, parent_server_id: Number(draft.slice(1)) }
        : { parent_mode: draft as 'auto' | 'off', parent_server_id: null }
      await updateCheck(check.id, body)
      setEditing(false)
      onSaved()
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }

  if (editing)
    return (
      <div className="parent-bar parent-bar-edit">
        <label className="field">
          <span>{t('Зависит от сервера')}</span>
          <select value={draft} onChange={(e) => setDraft(e.target.value)} disabled={!opts}>
            <option value="auto">{t('найти самой панели')}</option>
            <option value="off">{t('ни от какого')}</option>
            {(opts ?? []).map((o) => (
              <option key={o.id} value={`s${o.id}`}>
                {o.name}
              </option>
            ))}
          </select>
        </label>
        <div className="note-bar-actions">
          <button disabled={busy || !opts} onClick={save}>
            {t('Сохранить')}
          </button>
          <button className="ghost" disabled={busy} onClick={() => setEditing(false)}>
            {t('Отмена')}
          </button>
          <span className="muted small">
            {t('Пока сервер молчит, алерт монитора ждет его, а сам монитор назван в сообщении про сервер')}
          </span>
        </div>
        {err && <div className="muted small">{err}</div>}
      </div>
    )

  if (!parents.length) {
    if (!canEdit) return null
    return (
      <button className="ghost small note-add" onClick={start}>
        ⛓ {mode === 'off' ? t('ни от какого сервера не зависит') : t('указать сервер, от которого зависит')}
      </button>
    )
  }
  return (
    <div className={`parent-bar${parents.some((p) => p.silent) ? ' parent-bar-silent' : ''}`}>
      <span className="note-bar-ic">⛓</span>
      <span className="parent-bar-body">
        {parents.map((p) => (
          <span key={p.id}>
            {t('зависит от')} <a href={`/?server=${p.id}`}>{p.name}</a>
            <span className="muted small"> · {whyText(p.why, t)}</span>
            {p.silent && (
              <span className="parent-bar-warn"> · {t('сервер не на связи, алерты монитора ждут его')}</span>
            )}
          </span>
        ))}
      </span>
      {canEdit && (
        <button className="ghost small" onClick={start}>
          {t('изменить')}
        </button>
      )}
    </div>
  )
}

// Мониторы, которые зависят от сервера: пока он молчит, их алерты ждут его
export function useDependentChecks(serverId: number): { id: number; name: string }[] {
  const [kids, setKids] = useState<{ id: number; name: string }[]>([])
  useEffect(() => {
    let alive = true
    checksOverview()
      .then((o) => {
        if (alive)
          setKids(
            o.checks
              .filter((c) => (c.parents ?? []).some((p) => p.id === serverId))
              .map((c) => ({ id: c.id, name: c.name }))
              .sort((a, b) => a.name.localeCompare(b.name)),
          )
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [serverId])
  return kids
}

// Раздел "Зависимости" карточки сервера: за каким стоит (шлюз, VPN, гипервизор), кто стоит
// за ним и какие мониторы от него зависят. Раньше это была строка над графиками, и мониторы
// дальше шестого прятались в "и еще N"
export function ServerDeps({
  server,
  servers,
  kids,
  canEdit,
  onSaved,
}: {
  server: Server
  servers: Server[]
  kids: { id: number; name: string }[]
  canEdit: boolean
  onSaved: () => void
}) {
  const { t } = useI18n()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(0)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const byId = new Map(servers.map((s) => [s.id, s]))
  const parent = server.parent_id ? byId.get(server.parent_id) : undefined
  const behind = servers.filter((s) => s.parent_id === server.id).sort((a, b) => a.name.localeCompare(b.name))
  // в выборе нет самого сервера и тех, кто стоит за ним (цепочка замкнулась бы)
  const under = (id: number): boolean => {
    let cur = byId.get(id)?.parent_id
    const seen = new Set<number>()
    while (cur && !seen.has(cur)) {
      if (cur === server.id) return true
      seen.add(cur)
      cur = byId.get(cur)?.parent_id
    }
    return false
  }
  const choices = servers.filter((s) => s.id !== server.id && !under(s.id)).sort((a, b) => a.name.localeCompare(b.name))

  const save = async () => {
    setBusy(true)
    setErr('')
    try {
      await updateServer(server.id, { parent_id: draft || null })
      setEditing(false)
      onSaved()
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="net-cards">
      <div className="chart-card net-card net-card-wide">
        <div className="net-head">
          <div className="chart-cap">{t('Стоит за сервером')}</div>
          {canEdit && !editing && (
            <button
              className="ghost small row-act"
              onClick={() => {
                setDraft(server.parent_id ?? 0)
                setErr('')
                setEditing(true)
              }}
            >
              {t('изменить')}
            </button>
          )}
        </div>
        {editing ? (
          <div className="deps-edit">
            <select value={draft} onChange={(e) => setDraft(Number(e.target.value))}>
              <option value={0}>{t('ни за каким')}</option>
              {choices.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
            <button disabled={busy} onClick={save}>
              {t('Сохранить')}
            </button>
            <button className="ghost" disabled={busy} onClick={() => setEditing(false)}>
              {t('Отмена')}
            </button>
          </div>
        ) : parent ? (
          <div className="deps-list">
            <a href={`/?server=${parent.id}`}>{parent.name}</a>
            {!parent.online && <span className="parent-bar-warn">{t('не на связи')}</span>}
          </div>
        ) : (
          <div className="muted small">{t('ни за каким')}</div>
        )}
        {err && <div className="form-error small">{err}</div>}
        <div className="net-sub">
          {t('Шлюз, VPN или гипервизор: пока он молчит, про этот сервер не пишем, он строкой в сообщении про него')}
        </div>
      </div>
      <div className="chart-card net-card">
        <div className="net-head">
          <div className="chart-cap">{t('За ним стоят')}</div>
          <span className="muted small">{behind.length}</span>
        </div>
        {behind.length ? (
          <div className="deps-list">
            {behind.map((s) => (
              <a key={s.id} href={`/?server=${s.id}`}>
                {s.name}
              </a>
            ))}
          </div>
        ) : (
          <div className="muted small">{t('никто')}</div>
        )}
        <div className="net-sub">{t('Пока этот сервер молчит, про них не пишем отдельно')}</div>
      </div>
      <div className="chart-card net-card">
        <div className="net-head">
          <div className="chart-cap">{t('Зависят мониторы')}</div>
          <span className="muted small">{kids.length}</span>
        </div>
        {kids.length ? (
          <div className="deps-list">
            {kids.map((k) => (
              <a key={k.id} href={`/?check=${k.id}`}>
                {k.name}
              </a>
            ))}
          </div>
        ) : (
          <div className="muted small">{t('ни один')}</div>
        )}
        <div className="net-sub">
          {t('Пока сервер молчит, их алерты ждут его, а сами мониторы названы в сообщении про сервер')}
        </div>
      </div>
    </div>
  )
}
