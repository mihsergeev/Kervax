import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  createUser,
  deleteUser,
  getLoginGuard,
  getPanelRequests,
  getUsersOverview,
  listAccessGroups,
  resetUserPassword,
  saveLoginGuard,
  unbanLogin,
  updateUser,
  MIN_PASSWORD_LEN,
  type AccessGroups,
  type AuditEntry,
  type LoginGuard,
  type PanelRequests,
  type Role,
  type UserCard,
  type UsersOverview,
} from './api'
import { ROLE_LABEL, ROLE_SHORT, ROLE_WHAT, SECTION_LABEL, SECTIONS } from './access'
import { useAuth } from './auth'
import { CardNav, type SecDef } from './cardLayout'
import { useI18n } from './i18n'
import { LoginGuardSec } from './LoginGuardSec'
import { PanelRequestsSec } from './PanelRequestsSec'
import { banWords, guardDraftOf, sameGuard, type GuardDraft } from './loginGuard'
import { NetTile } from './NetLinks'
import { sinceShort } from './serverUtils'

// Окно "Пользователи" как карточка сервера: сводка по учеткам, у каждой учетки свой раздел
// (вход, доступ, пароль, последние действия), новая учетка - только обязательное, и журнал
// действий панели, которого больше нигде не видно.

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}
type T = (s: string, p?: Record<string, string | number>) => string


// действия журнала по-человечески; чего здесь нет - показываем как записано
const ACT: Record<string, string> = {
  login_ok: 'вход',
  login_fail: 'неверный пароль',
  login_2fa_fail: 'неверный код 2FA',
  login_blocked: 'вход заблокирован',
  login_lockout: 'блокировка входа',
  login_guard: 'изменил защиту входа',
  login_unban: 'разблокировал адрес',
  user_create: 'создал учетку',
  user_delete: 'удалил учетку',
  user_scope: 'изменил доступ учетки',
  user_reset_pw: 'сбросил пароль учетки',
  password_change: 'сменил свой пароль',
  '2fa_enable': 'включил 2FA',
  '2fa_disable': 'выключил 2FA',
  server_create: 'добавил сервер',
  server_update: 'изменил сервер',
  server_delete: 'удалил сервер',
  server_note: 'заметка к серверу',
  server_snooze: 'заглушил алерт сервера',
  check_create: 'добавил монитор',
  check_update: 'изменил монитор',
  check_delete: 'удалил монитор',
  check_note: 'заметка к монитору',
  checks_import: 'импорт мониторов',
  checks_bulk_delete: 'удалил мониторы',
  location_create: 'добавил локацию',
  location_update: 'изменил локацию',
  location_delete: 'удалил локацию',
  link_add: 'добавил связь между серверами',
  link_delete: 'удалил связь между серверами',
  maint_create: 'добавил окно обслуживания',
  maint_update: 'изменил окно обслуживания',
  maint_delete: 'удалил окно обслуживания',
  alerts_config: 'изменил алерты',
  retention_config: 'изменил сроки хранения',
  backup_config: 'изменил автобэкап',
  backup_run: 'создал бэкап панели',
  backup_restore: 'восстановил панель из бэкапа',
  vault_open: 'открыл сейф бэкапов',
  backup_credentials_view: 'посмотрел доступы бэкапа',
  ansible_token_issue: 'выпустил токен ansible',
  ansible_token_revoke: 'отключил доступ ansible',
  tg_link: 'привязал Telegram',
  tg_unlink: 'отвязал Telegram',
}
const LOGIN_ACTS = new Set(['login_ok', 'login_fail', 'login_2fa_fail', 'login_blocked', 'login_lockout'])
const FAIL_ACTS = new Set(['login_fail', 'login_2fa_fail', 'login_blocked', 'login_lockout'])

type Draft = { id: number; role: Role; sections: string[]; server_groups: string[]; site_groups: string[] }
const draftOf = (u: UserCard): Draft => ({
  id: u.id,
  role: u.role,
  sections: u.sections ?? [],
  server_groups: u.server_groups ?? [],
  site_groups: u.site_groups ?? [],
})
const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x))
const sameDraft = (a: Draft, b: Draft) =>
  a.role === b.role &&
  sameSet(a.sections, b.sections) &&
  sameSet(a.server_groups, b.server_groups) &&
  sameSet(a.site_groups, b.site_groups)

/** Выбор набора значений кнопками. Пусто = без ограничений, и это подписано:
 *  иначе "ничего не отмечено" читается как "ничего не доступно". */
function PickList({
  title,
  hint,
  all,
  value,
  onChange,
  t,
}: {
  title: string
  hint: string
  all: { key: string; label: string }[]
  value: string[]
  onChange: (v: string[]) => void
  t: T
}) {
  const toggle = (k: string) => onChange(value.includes(k) ? value.filter((x) => x !== k) : [...value, k])
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

const scopeText = (u: UserCard, t: T) =>
  u.role === 'admin'
    ? t('все разделы и группы')
    : [
        (u.sections?.length ?? 0) === 0
          ? t('все разделы')
          : (u.sections ?? []).map((x) => t(SECTION_LABEL[x] ?? x)).join(', '),
        (u.server_groups?.length ?? 0) === 0
          ? t('все серверы')
          : t('серверы: {g}', { g: (u.server_groups ?? []).join(', ') }),
        (u.site_groups?.length ?? 0) === 0 ? t('все сайты') : t('сайты: {g}', { g: (u.site_groups ?? []).join(', ') }),
      ].join(' · ')

export function UsersModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const { username: meName } = useAuth()
  const [ov, setOv] = useState<UsersOverview | null>(null)
  const [guard, setGuard] = useState<LoginGuard | null>(null)
  const [gDraft, setGDraft] = useState<GuardDraft | null>(null)
  const [preq, setPreq] = useState<PanelRequests | null>(null)
  const [groups, setGroups] = useState<AccessGroups>({ servers: [], sites: [] })
  const [sec, setSec] = useState('over')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  // новая учетка
  const [nName, setNName] = useState('')
  const [nPass, setNPass] = useState('')
  const [nRole, setNRole] = useState<Role>('viewer')
  // сброс пароля в карточке учетки
  const [pw, setPw] = useState('')
  // журнал: чей и какие записи
  const [logWho, setLogWho] = useState('')
  const [logKind, setLogKind] = useState<'all' | 'changes' | 'logins'>('all')
  const mainRef = useRef<HTMLDivElement>(null)

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    },
    [onUnauthorized, t],
  )
  const load = useCallback(() => getUsersOverview().then(setOv).catch(fail), [fail])
  const loadGuard = useCallback(() => getLoginGuard().then(setGuard).catch(fail), [fail])
  useEffect(() => {
    load()
    loadGuard()
    getPanelRequests()
      .then(setPreq)
      .catch(() => setPreq(null))
    listAccessGroups()
      .then(setGroups)
      .catch(() => setGroups({ servers: [], sites: [] }))
  }, [load, loadGuard])

  const users = ov?.users ?? []
  const curUser = sec.startsWith('u') ? users.find((u) => `u${u.id}` === sec) : undefined
  const d = curUser ? (draft && draft.id === curUser.id ? draft : draftOf(curUser)) : null
  const gd = guard ? (gDraft ?? guardDraftOf(guard)) : null
  const gDirty = sec === 'guard' && !!guard && !!gd && !sameGuard(gd, guardDraftOf(guard))
  const dirty = (!!curUser && !!d && !sameDraft(d, draftOf(curUser))) || gDirty

  const go = (id: string) => {
    if (dirty && !window.confirm(t('Уйти без сохранения? Изменения пропадут.'))) return
    setDraft(null)
    setGDraft(null)
    setPw('')
    setErr(null)
    setMsg(null)
    setSec(id)
    mainRef.current?.scrollTo({ top: 0 })
  }
  const close = () => {
    if (dirty && !window.confirm(t('Закрыть без сохранения? Изменения пропадут.'))) return
    onClose()
  }
  const patch = (p: Partial<Draft>) => {
    if (!curUser) return
    setMsg(null)
    setDraft((x) => ({ ...(x && x.id === curUser.id ? x : draftOf(curUser)), ...p }))
  }

  async function act(fn: () => Promise<string | void>) {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const m = await fn()
      if (m) setMsg(m)
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }
  const saveAccess = () =>
    act(async () => {
      if (!d) return
      await updateUser(d.id, {
        role: d.role,
        sections: d.sections,
        server_groups: d.server_groups,
        site_groups: d.site_groups,
      })
      await load()
      setDraft(null)
      return t('Доступ сохранен.')
    })
  const saveGuard = () =>
    act(async () => {
      if (!gd) return
      setGuard(await saveLoginGuard(gd))
      setGDraft(null)
      return t('Защита входа сохранена.')
    })
  const unban = (key: string) =>
    act(async () => {
      setGuard(await unbanLogin(key))
      return t('Адрес {k} разблокирован.', { k: key })
    })
  const create = () =>
    act(async () => {
      const u = await createUser({ username: nName.trim(), password: nPass, role: nRole })
      setNName('')
      setNPass('')
      setNRole('viewer')
      await load()
      setSec(`u${u.id}`)
      return u.role === 'admin'
        ? t('Учетка создана.')
        : t('Учетка создана. Какие разделы и группы ей видны - в доступе ниже.')
    })
  const resetPw = (u: UserCard) =>
    act(async () => {
      await resetUserPassword(u.id, pw)
      setPw('')
      return t('Пароль обновлен, прежние входы этой учетки сброшены.')
    })
  const remove = (u: UserCard) => {
    if (!window.confirm(t('Удалить учётку «{name}»?', { name: u.username }))) return
    act(async () => {
      await deleteUser(u.id)
      await load()
      setDraft(null)
      setSec('over')
    })
  }

  const when = (iso: string) =>
    new Date(iso).toLocaleString([], { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' })
  // в журнале без года: записи свежие, а узкая колонка даты влезает на телефоне
  const whenShort = (iso: string) =>
    new Date(iso).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  const ago = (iso: string) => {
    const a = sinceShort(iso, t)
    return a ? t('{a} назад', { a }) : t('только что')
  }
  const actText = (a: AuditEntry) => t(ACT[a.action] ?? a.action)
  const admins = users.filter((u) => u.role === 'admin')
  const with2fa = users.filter((u) => u.totp_enabled).length
  const nameBtn = (u: UserCard) => (
    <button className="linklike" onClick={() => go(`u${u.id}`)}>
      {u.username}
    </button>
  )
  const roleChip = (r: string) => <span className={`role-chip role-${r}`}>{t(ROLE_LABEL[r] ?? r)}</span>

  const logTable = (rows: AuditEntry[], who: boolean) => (
    <table className="sec-tbl">
      <thead>
        <tr>
          <th>{t('Когда')}</th>
          {who && <th>{t('Кто')}</th>}
          <th>{t('Что')}</th>
          <th className="hide-m">{t('Объект')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((a) => (
          <tr key={a.id}>
            <td className="nowrap">{whenShort(a.ts)}</td>
            {who && <td className="nowrap">{a.username || '-'}</td>}
            <td className={FAIL_ACTS.has(a.action) ? 't-degraded' : ''}>
              {actText(a)}
              {a.detail && <span className="muted small"> · {a.detail}</span>}
            </td>
            <td className="hide-m mono muted small">{a.target}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )

  const SECS: SecDef[] = [
    { id: 'over', label: 'Сводка', group: '' },
    ...users.map((u) => ({ id: `u${u.id}`, label: u.username, group: 'Учетки' })),
    { id: 'new', label: 'Новая учетка', group: 'Добавить' },
    { id: 'guard', label: 'Защита входа', group: 'Защита' },
    { id: 'reqs', label: 'Запросы к панели', group: 'Защита' },
    { id: 'log', label: 'Журнал действий', group: 'Журнал' },
  ]
  const side: Record<string, string> = {}
  for (const u of users) side[`u${u.id}`] = t(ROLE_SHORT[u.role] ?? u.role)
  if (guard?.bans.length) side.guard = t('блок {n}', { n: guard.bans.length })
  if (preq?.active.length) side.reqs = t('сейчас {n}', { n: preq.active.length })
  const cur = SECS.find((s) => s.id === sec) ?? SECS[0]

  const audit = ov?.audit ?? []
  const logRows = audit.filter(
    (a) =>
      (!logWho || a.username === logWho) &&
      (logKind === 'all' || (logKind === 'logins') === LOGIN_ACTS.has(a.action)),
  )
  const logNames = [...new Set(audit.map((a) => a.username).filter(Boolean))].sort()

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Пользователи')}</h3>
          {ov && <span className="muted small">{t('учеток: {n}', { n: users.length })}</span>}
          <button className="ghost" onClick={close}>
            {t('Закрыть')}
          </button>
        </div>

        <div className="al-body">
          <CardNav secs={SECS} active={cur.id} info={{}} side={side} onGo={go} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>
                {curUser ? curUser.username : t(cur.label)} {curUser && roleChip(curUser.role)}
                {curUser?.totp_enabled && <span className="type-chip">2FA</span>}
              </h4>
            </div>
            {!ov ? (
              err ? <p className="form-error">{err}</p> : <p className="muted small">{t('загрузка…')}</p>
            ) : (
              <div className="srv-sec">
                {cur.id === 'over' && (
                  <>
                    <p className="muted small al-lead">
                      {t('Роль задает, что можно менять, разделы - какие вкладки видны, группы - какие серверы и мониторы вообще доступны. Пусто в разделах или группах - без ограничений.')}
                    </p>
                    <div className="net-tiles srv-tiles">
                      <NetTile
                        label={t('Учеток')}
                        value={String(users.length)}
                        sub={t('админов {a} · правка {e} · просмотр {v}', {
                          a: admins.length,
                          e: users.filter((u) => u.role === 'editor').length,
                          v: users.filter((u) => u.role === 'viewer').length,
                        })}
                      />
                      <NetTile label={t('С 2FA')} value={`${with2fa} / ${users.length}`} sub={t('включает сам пользователь')} />
                      <button className="srv-tile-btn" onClick={() => go('guard')} title={t('к защите входа')}>
                        <NetTile
                          label={t('Неудачных входов за сутки')}
                          value={String(ov.fails_24h)}
                          sub={ov.fails_24h ? t('с адресов: {n}', { n: ov.fail_ips_24h }) : t('не было')}
                          tone={ov.fails_24h ? 'warn' : undefined}
                        />
                      </button>
                      {guard && (
                        <button className="srv-tile-btn" onClick={() => go('guard')} title={t('к защите входа')}>
                          <NetTile
                            label={t('Заблокированы сейчас')}
                            value={guard.bans.length ? guard.bans.map((b) => b.key).slice(0, 2).join(', ') : t('никто')}
                            sub={
                              guard.bans.length > 2
                                ? t('и еще {n}', { n: guard.bans.length - 2 })
                                : t('после {n} неудач за {w} мин, {ban}', {
                                    n: guard.max_failures,
                                    w: guard.window_minutes,
                                    ban: banWords(guard.ban_minutes, t),
                                  })
                            }
                            tone={guard.bans.length ? 'warn' : undefined}
                          />
                        </button>
                      )}
                      {!!preq?.active.length && (
                        <button className="srv-tile-btn" onClick={() => go('reqs')} title={t('к запросам к панели')}>
                          <NetTile
                            label={t('Левые запросы в панель')}
                            value={preq.active.map((a) => (a.servers.length === 1 ? a.servers[0].name : a.key)).slice(0, 2).join(', ')}
                            sub={preq.active.length > 2 ? t('и еще {n}', { n: preq.active.length - 2 }) : preq.active[0].what}
                            tone="bad"
                          />
                        </button>
                      )}
                    </div>
                    <table className="sec-tbl">
                      <thead>
                        <tr>
                          <th>{t('Учетка')}</th>
                          <th className="hide-m">{t('Доступ')}</th>
                          <th>{t('Последний вход')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {users.map((u) => (
                          <tr key={u.id}>
                            <td>
                              <span className="us-name">
                                {nameBtn(u)} {roleChip(u.role)}
                                {u.totp_enabled && <span className="type-chip">2FA</span>}
                              </span>
                            </td>
                            <td className="hide-m muted small">{scopeText(u, t)}</td>
                            <td className="small">
                              {u.last_login ? ago(u.last_login) : <span className="muted">{t('не входил')}</span>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </>
                )}

                {curUser && d && (
                  <>
                    <p className="muted small al-lead">{t(ROLE_WHAT[curUser.role] ?? '')}</p>
                    <div className="ans-kv small">
                      <span className="muted">{t('Последний вход')}</span>
                      <span>
                        {curUser.last_login ? (
                          <>
                            {when(curUser.last_login)} <span className="muted">({ago(curUser.last_login)})</span>
                            {curUser.last_ip && <span className="mono muted"> · {curUser.last_ip}</span>}
                          </>
                        ) : (
                          <span className="muted">{t('еще не входил')}</span>
                        )}
                      </span>
                      <span className="muted">{t('Неудачных входов за сутки')}</span>
                      <span className={curUser.fails_24h ? 't-degraded' : ''}>{curUser.fails_24h}</span>
                      <span className="muted">2FA</span>
                      <span>
                        {curUser.totp_enabled ? t('включена') : <span className="muted">{t('не включена, включает сам пользователь в своем меню')}</span>}
                      </span>
                      <span className="muted">{t('Алерты в Telegram')}</span>
                      <span>{curUser.tg_linked ? t('привязаны') : <span className="muted">{t('не привязаны')}</span>}</span>
                      <span className="muted">{t('Действий за 30 дней')}</span>
                      <span>{curUser.actions_30d}</span>
                      <span className="muted">{t('Создана')}</span>
                      <span>{when(curUser.created_at)}</span>
                    </div>

                    <div className="srv-nav-grp rt-cap">{t('Доступ')}</div>
                    <label className="field us-role">
                      <span>{t('Роль')}</span>
                      <select value={d.role} onChange={(e) => patch({ role: e.target.value as Role })}>
                        <option value="viewer">{t('Только просмотр')}</option>
                        <option value="editor">{t('Правка')}</option>
                        <option value="admin">{t('Админ')}</option>
                      </select>
                    </label>
                    {d.role !== 'admin' ? (
                      <>
                        <PickList
                          title="Разделы"
                          hint="не отмечено — доступны все"
                          all={SECTIONS}
                          value={d.sections}
                          onChange={(v) => patch({ sections: v })}
                          t={t}
                        />
                        <PickList
                          title="Группы серверов"
                          hint="не отмечено — видны все"
                          all={groups.servers.map((g) => ({ key: g, label: g }))}
                          value={d.server_groups}
                          onChange={(v) => patch({ server_groups: v })}
                          t={t}
                        />
                        <PickList
                          title="Группы сайтов"
                          hint="не отмечено — видны все"
                          all={groups.sites.map((g) => ({ key: g, label: g }))}
                          value={d.site_groups}
                          onChange={(v) => patch({ site_groups: v })}
                          t={t}
                        />
                      </>
                    ) : (
                      <p className="muted small">{t('Админу область не режется: он управляет учетками и может снять ее себе сам.')}</p>
                    )}

                    <div className="srv-nav-grp rt-cap">{t('Пароль')}</div>
                    <div className="rt-row">
                      <label className="field us-pw">
                        <span>{t('Новый пароль (мин. {n})', { n: MIN_PASSWORD_LEN })}</span>
                        <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" />
                      </label>
                      <button className="ghost" onClick={() => resetPw(curUser)} disabled={busy || pw.length < MIN_PASSWORD_LEN}>
                        {t('Сбросить пароль')}
                      </button>
                    </div>

                    <div className="srv-nav-grp rt-cap">{t('Последние действия')}</div>
                    {(() => {
                      const mine = audit.filter((a) => a.username === curUser.username)
                      return mine.length ? (
                        <>
                          {logTable(mine.slice(0, 10), false)}
                          {mine.length > 10 && (
                            <button
                              className="ghost small"
                              onClick={() => {
                                go('log')
                                setLogWho(curUser.username)
                                setLogKind('all')
                              }}
                            >
                              {t('весь журнал учетки')} ›
                            </button>
                          )}
                        </>
                      ) : (
                        <p className="muted small">{t('В журнале записей нет.')}</p>
                      )
                    })()}

                    <div className="srv-nav-grp rt-cap">{t('Удаление')}</div>
                    {(() => {
                      const why =
                        curUser.username === meName
                          ? t('Свою учётку удалить нельзя')
                          : curUser.role === 'admin' && admins.length <= 1
                            ? t('Это последний администратор — панель останется без управления')
                            : ''
                      return (
                        <div className="ans-acts">
                          <button className="ghost danger-btn" disabled={!!why || busy} title={why} onClick={() => remove(curUser)}>
                            {t('Удалить учетку')}
                          </button>
                          {why && <span className="muted small">{why}</span>}
                        </div>
                      )
                    })()}
                  </>
                )}

                {cur.id === 'new' && (
                  <>
                    <p className="muted small al-lead">
                      {t('Только логин, пароль и роль. Какие разделы и группы видны - в карточке учетки после создания.')}
                    </p>
                    <div className="form-row us-form">
                      <label className="field">
                        <span>{t('Логин')}</span>
                        <input value={nName} onChange={(e) => setNName(e.target.value)} autoComplete="off" />
                      </label>
                      <label className="field">
                        <span>{t('Пароль (мин. {n})', { n: MIN_PASSWORD_LEN })}</span>
                        <input type="password" value={nPass} onChange={(e) => setNPass(e.target.value)} autoComplete="new-password" />
                      </label>
                    </div>
                    <label className="field us-role">
                      <span>{t('Роль')}</span>
                      <select value={nRole} onChange={(e) => setNRole(e.target.value as Role)}>
                        <option value="viewer">{t('Только просмотр')}</option>
                        <option value="editor">{t('Правка')}</option>
                        <option value="admin">{t('Админ')}</option>
                      </select>
                    </label>
                    <p className="muted small">{t(ROLE_WHAT[nRole])}</p>
                    <div className="ans-acts">
                      <button onClick={create} disabled={busy || !nName.trim() || nPass.length < MIN_PASSWORD_LEN}>
                        {busy ? t('…') : t('Создать')}
                      </button>
                    </div>
                  </>
                )}

                {cur.id === 'guard' &&
                  (guard && gd ? (
                    <LoginGuardSec guard={guard} draft={gd} onDraft={(x) => (setMsg(null), setGDraft(x))} onUnban={unban} busy={busy} t={t} />
                  ) : (
                    <p className="muted small">{t('загрузка…')}</p>
                  ))}

                {cur.id === 'reqs' &&
                  (preq ? <PanelRequestsSec data={preq} t={t} /> : <p className="muted small">{t('загрузка…')}</p>)}

                {cur.id === 'log' && (
                  <>
                    <p className="muted small al-lead">
                      {t('Кто и что менял в панели, входы и неудачные попытки. Показаны последние {n} записей.', { n: audit.length })}
                    </p>
                    <div className="rt-row">
                      <div className="win-switch">
                        {(
                          [
                            ['all', 'Все'],
                            ['changes', 'Изменения'],
                            ['logins', 'Входы'],
                          ] as const
                        ).map(([k, l]) => (
                          <button key={k} className={`win-btn${logKind === k ? ' win-btn-active' : ''}`} onClick={() => setLogKind(k)}>
                            {t(l)}
                          </button>
                        ))}
                      </div>
                      <label className="field rt-days us-who">
                        <span>{t('Кто')}</span>
                        <select value={logWho} onChange={(e) => setLogWho(e.target.value)}>
                          <option value="">{t('все')}</option>
                          {logNames.map((n) => (
                            <option key={n} value={n}>
                              {n}
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>
                    {logRows.length ? (
                      logTable(logRows.slice(0, 200), !logWho)
                    ) : (
                      <p className="muted small">{t('Записей нет.')}</p>
                    )}
                    {logRows.length > 200 && <p className="muted small">{t('Показаны первые 200 из {n}.', { n: logRows.length })}</p>}
                  </>
                )}
              </div>
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
              {t('Есть несохраненные изменения')} · {curUser ? curUser.username : t('Защита входа')}
            </span>
          ) : ov ? (
            <span className="muted small">
              {t('Админов: {n}', { n: admins.length })}
              {meName ? ` · ${t('вы: {u}', { u: meName })}` : ''}
            </span>
          ) : null}
          <span className="al-foot-btns">
            {dirty && (
              <button className="ghost" onClick={() => (setDraft(null), setGDraft(null))} disabled={busy}>
                {t('Отмена')}
              </button>
            )}
            {curUser && (
              <button onClick={saveAccess} disabled={!dirty || busy}>
                {busy ? t('…') : t('Сохранить')}
              </button>
            )}
            {cur.id === 'guard' && guard && (
              <button onClick={saveGuard} disabled={!gDirty || busy}>
                {busy ? t('…') : t('Сохранить')}
              </button>
            )}
          </span>
        </div>
      </div>
    </div>
  )
}
