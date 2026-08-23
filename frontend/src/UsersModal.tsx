import { useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  createUser,
  deleteUser,
  listAccessGroups,
  listUsers,
  type AccessGroups,
  resetUserPassword,
  updateUser,
  type PanelUser,
  type Role,
  MIN_PASSWORD_LEN,
} from './api'
import { useI18n } from './i18n'
import { useAuth } from './auth'

// Разделы верхнего меню. Ключи ДОЛЖНЫ совпадать с ALL_SECTIONS в backend/app/deps.py:
// по ним бэкенд решает, какие маршруты открыты, а фронт — какие вкладки рисовать.
const SECTIONS: { key: string; label: string }[] = [
  { key: 'sites', label: 'Сайты' },
  { key: 'servers', label: 'Серверы' },
  { key: 'docker', label: 'Докер' },
  { key: 'kuber', label: 'Кубер' },
  { key: 'services', label: 'Сервисы' },
  { key: 'backups', label: 'Бэкапы' },
]

const SECTION_LABEL: Record<string, string> = Object.fromEntries(
  SECTIONS.map((x) => [x.key, x.label]),
)

const ROLE_LABEL: Record<string, string> = {
  admin: 'Админ',
  editor: 'Правка',
  viewer: 'Только просмотр',
}

/** Выбор набора значений галочками. Пусто = без ограничений, и это подписано:
 *  иначе «ничего не отмечено» читается как «ничего не доступно». */
function PickList({
  title, hint, all, value, onChange, t,
}: {
  title: string
  hint: string
  all: { key: string; label: string }[]
  value: string[]
  onChange: (v: string[]) => void
  t: (s: string, p?: Record<string, string | number>) => string
}) {
  const toggle = (k: string) =>
    onChange(value.includes(k) ? value.filter((x) => x !== k) : [...value, k])
  return (
    <div className="acl-pick">
      <div className="acl-pick-head">
        <span>{t(title)}</span>
        <span className="muted small">{value.length === 0 ? t(hint) : ''}</span>
      </div>
      <div className="acl-pick-items">
        {all.map((x) => {
          const on = value.includes(x.key)
          return (
            <button
              key={x.key}
              type="button"
              className={`acl-chip${on ? ' acl-chip-on' : ''}`}
              aria-pressed={on}
              onClick={() => toggle(x.key)}
            >
              {t(x.label)}
            </button>
          )
        })}
        {all.length === 0 && <span className="muted small">{t('Групп пока нет')}</span>}
      </div>
    </div>
  )
}

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

/** Правка роли и области существующей учётки. Сохраняем по кнопке, а не по каждому
 *  клику: иначе снятие последней галочки на секунду означало бы «доступно всё». */
function AccessEditor({ u, allGroups, onSaved, t }: {
  u: PanelUser
  allGroups: AccessGroups
  onSaved: () => void
  t: (s: string, p?: Record<string, string | number>) => string
}) {
  const [role, setRole] = useState<Role>(u.role)
  const [secs, setSecs] = useState<string[]>(u.sections ?? [])
  const [srvGrps, setSrvGrps] = useState<string[]>(u.server_groups ?? [])
  const [siteGrps, setSiteGrps] = useState<string[]>(u.site_groups ?? [])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const save = async () => {
    setBusy(true)
    setErr(null)
    try {
      await updateUser(u.id, {
        role,
        sections: secs,
        server_groups: srvGrps,
        site_groups: siteGrps,
      })
      onSaved()
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="acl-editor">
      <label className="field">
        <span>{t('Роль')}</span>
        <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="viewer">{t('Только просмотр')}</option>
          <option value="editor">{t('Правка')}</option>
          <option value="admin">{t('Админ')}</option>
        </select>
      </label>
      {role !== 'admin' && (
        <>
          <PickList
            title="Разделы" hint="не отмечено — доступны все"
            all={SECTIONS} value={secs} onChange={setSecs} t={t}
          />
          <PickList
            title="Группы серверов" hint="не отмечено — видны все"
            all={allGroups.servers.map((g) => ({ key: g, label: g }))}
            value={srvGrps} onChange={setSrvGrps} t={t}
          />
          <PickList
            title="Группы сайтов" hint="не отмечено — видны все"
            all={allGroups.sites.map((g) => ({ key: g, label: g }))}
            value={siteGrps} onChange={setSiteGrps} t={t}
          />
        </>
      )}
      {err && <p className="form-error small">{err}</p>}
      <div className="modal-actions">
        <button disabled={busy} onClick={save}>{busy ? t('…') : t('Сохранить доступ')}</button>
      </div>
    </div>
  )
}

export function UsersModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const { username: meName } = useAuth()
  const [users, setUsers] = useState<PanelUser[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // форма создания
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>('viewer')
  const [secs, setSecs] = useState<string[]>([])
  const [srvGrps, setSrvGrps] = useState<string[]>([])
  const [siteGrps, setSiteGrps] = useState<string[]>([])
  const [allGroups, setAllGroups] = useState<AccessGroups>({ servers: [], sites: [] })
  // какую учётку правим (её область раскрыта под строкой)
  const [editId, setEditId] = useState<number | null>(null)

  const load = useCallback(() => {
    listUsers()
      .then((u) => {
        setUsers(u)
        setErr(null)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        setErr(e instanceof Error ? e.message : t('Ошибка'))
      })
  }, [onUnauthorized, t])

  useEffect(() => {
    load()
    listAccessGroups()
      .then(setAllGroups)
      .catch(() => setAllGroups({ servers: [], sites: [] }))
  }, [load])

  const submit = async () => {
    if (!username.trim() || password.length < MIN_PASSWORD_LEN) return
    setBusy(true)
    setErr(null)
    try {
      await createUser({
        username: username.trim(),
        password,
        role,
        sections: secs,
        server_groups: srvGrps,
        site_groups: siteGrps,
      })
      setUsername('')
      setPassword('')
      setRole('viewer')
      setSecs([])
      setSrvGrps([])
      setSiteGrps([])
      load()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (u: PanelUser) => {
    if (!window.confirm(t('Удалить учётку «{name}»?', { name: u.username }))) return
    try {
      await deleteUser(u.id)
      load()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    }
  }

  const resetPw = async (u: PanelUser) => {
    const pw = window.prompt(
      t('Новый пароль для «{name}» (мин. {n} симв.):', { name: u.username, n: MIN_PASSWORD_LEN }),
    )
    if (pw == null) return
    if (pw.length < MIN_PASSWORD_LEN) {
      setErr(t('Пароль минимум {n} символов', { n: MIN_PASSWORD_LEN }))
      return
    }
    try {
      await resetUserPassword(u.id, pw)
      setErr(null)
      window.alert(t('Пароль обновлён'))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal users-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Пользователи')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <p className="muted small">
          {t('Роль задаёт, что можно менять; разделы — какие вкладки видны; группы — какие серверы и мониторы вообще доступны. Пусто в разделах или группах означает «без ограничений».')}
        </p>

        <div className="users-list">
          {users?.map((u) => (
            <div key={u.id} className="user-row">
              <div className="user-main">
                <div className="user-title">
                  <span className="user-name">{u.username}</span>
                  <span className={`role-chip role-${u.role}`}>
                    {t(ROLE_LABEL[u.role] ?? u.role)}
                  </span>
                  {u.totp_enabled && <span className="type-chip">2FA</span>}
                </div>
                {/* область — отдельной строкой: рядом с кнопками она переносилась
                    посреди фразы и строка выглядела сломанной */}
                <div className="user-scope muted small">
                  {(u.sections?.length ?? 0) === 0
                    ? t('все разделы')
                    : (u.sections ?? []).map((x) => t(SECTION_LABEL[x] ?? x)).join(', ')}
                  {' · '}
                  {(u.server_groups?.length ?? 0) === 0
                    ? t('все серверы')
                    : t('серверы: {g}', { g: (u.server_groups ?? []).join(', ') })}
                  {' · '}
                  {(u.site_groups?.length ?? 0) === 0
                    ? t('все сайты')
                    : t('сайты: {g}', { g: (u.site_groups ?? []).join(', ') })}
                </div>
              </div>
              <div className="user-actions">
                <button className="ghost small" onClick={() => setEditId(editId === u.id ? null : u.id)}>
                  {editId === u.id ? t('Свернуть') : t('Доступ')}
                </button>
                <button className="ghost small" onClick={() => resetPw(u)}>
                  {t('Сбросить пароль')}
                </button>
                {/* Бэкенд эти два случая всё равно отбивает (400). Кнопка не должна
                    предлагать действие, которое заведомо не выполнится. */}
                {(() => {
                  const self = u.username === meName
                  const lastAdmin =
                    u.role === 'admin' && (users ?? []).filter((x) => x.role === 'admin').length <= 1
                  const why = self
                    ? t('Свою учётку удалить нельзя')
                    : lastAdmin
                      ? t('Это последний администратор — панель останется без управления')
                      : ''
                  return (
                    <button
                      className="ghost small danger-btn"
                      disabled={!!why}
                      title={why}
                      onClick={() => remove(u)}
                    >
                      {t('Удалить')}
                    </button>
                  )
                })()}
              </div>
              {editId === u.id && <AccessEditor u={u} allGroups={allGroups} onSaved={load} t={t} />}
            </div>
          ))}
          {users && users.length === 0 && (
            <div className="muted small">{t('Нет учёток.')}</div>
          )}
        </div>

        <div className="menu-divider" />
        <h4>{t('Новая учётка')}</h4>
        <div className="form-row">
          <label className="field">
            <span>{t('Логин')}</span>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="off"
            />
          </label>
          <label className="field">
            <span>{t('Пароль (мин. {n})', { n: MIN_PASSWORD_LEN })}</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
            />
          </label>
        </div>
        <label className="field">
          <span>{t('Роль')}</span>
          <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            <option value="viewer">{t('Только просмотр')}</option>
            <option value="editor">{t('Правка')}</option>
            <option value="admin">{t('Админ')}</option>
          </select>
        </label>
        {/* админу область не режем: он и так управляет учётками и может её снять себе */}
        {role !== 'admin' && (
          <>
            <PickList
              title="Разделы" hint="не отмечено — доступны все"
              all={SECTIONS} value={secs} onChange={setSecs} t={t}
            />
            <PickList
              title="Группы серверов" hint="не отмечено — видны все"
              all={allGroups.servers.map((g) => ({ key: g, label: g }))}
              value={srvGrps} onChange={setSrvGrps} t={t}
            />
            <PickList
              title="Группы сайтов" hint="не отмечено — видны все"
              all={allGroups.sites.map((g) => ({ key: g, label: g }))}
              value={siteGrps} onChange={setSiteGrps} t={t}
            />
          </>
        )}

        {err && <p className="form-error">{err}</p>}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            {t('Отмена')}
          </button>
          <button
            onClick={submit}
            disabled={busy || !username.trim() || password.length < MIN_PASSWORD_LEN}
          >
            {busy ? t('…') : t('Создать')}
          </button>
        </div>
      </div>
    </div>
  )
}
