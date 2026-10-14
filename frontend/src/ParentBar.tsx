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

// Сервер: за каким стоит (шлюз, VPN, гипервизор), кто стоит за ним и какие мониторы от него зависят
export function ServerParentBar({
  server,
  servers,
  canEdit,
  onSaved,
}: {
  server: Server
  servers: Server[]
  canEdit: boolean
  onSaved: () => void
}) {
  const { t } = useI18n()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(0)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [kids, setKids] = useState<string[]>([])
  useEffect(() => {
    let alive = true
    checksOverview()
      .then((o) => {
        if (alive)
          setKids(o.checks.filter((c) => (c.parents ?? []).some((p) => p.id === server.id)).map((c) => c.name))
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [server.id])
  const byId = new Map(servers.map((s) => [s.id, s]))
  const parent = server.parent_id ? byId.get(server.parent_id) : undefined
  const behind = servers.filter((s) => s.parent_id === server.id)
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

  if (editing)
    return (
      <div className="parent-bar parent-bar-edit">
        <label className="field">
          <span>{t('Стоит за сервером')}</span>
          <select value={draft} onChange={(e) => setDraft(Number(e.target.value))}>
            <option value={0}>{t('ни за каким')}</option>
            {choices.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <div className="note-bar-actions">
          <button disabled={busy} onClick={save}>
            {t('Сохранить')}
          </button>
          <button className="ghost" disabled={busy} onClick={() => setEditing(false)}>
            {t('Отмена')}
          </button>
          <span className="muted small">
            {t('Шлюз, VPN или гипервизор: пока он молчит, про этот сервер не пишем, он строкой в сообщении про него')}
          </span>
        </div>
        {err && <div className="muted small">{err}</div>}
      </div>
    )

  if (!parent && !behind.length && !kids.length) {
    if (!canEdit) return null
    return (
      <button
        className="ghost small note-add"
        onClick={() => {
          setDraft(0)
          setErr('')
          setEditing(true)
        }}
      >
        ⛓ {t('указать, за каким сервером стоит')}
      </button>
    )
  }
  const names = (xs: string[]) => (xs.length > 6 ? `${xs.slice(0, 6).join(', ')} ${t('и еще {n}', { n: xs.length - 6 })}` : xs.join(', '))
  return (
    <div className={`parent-bar${parent && !parent.online ? ' parent-bar-silent' : ''}`}>
      <span className="note-bar-ic">⛓</span>
      <span className="parent-bar-body">
        {parent && (
          <span>
            {t('стоит за')} <a href={`/?server=${parent.id}`}>{parent.name}</a>
            {!parent.online && <span className="parent-bar-warn"> · {t('не на связи')}</span>}
          </span>
        )}
        {behind.length > 0 && (
          <span>
            {t('за ним')}: {names(behind.map((s) => s.name).sort())}
          </span>
        )}
        {kids.length > 0 && (
          <span>
            {t('зависят мониторы')}: {names(kids)}
          </span>
        )}
      </span>
      {canEdit && (
        <button
          className="ghost small"
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
  )
}
