import { useState } from 'react'
import type { LoginGuard } from './api'
import { banWords, type GuardDraft } from './loginGuard'

type T = (s: string, p?: Record<string, string | number>) => string

// раздел "Защита входа" окна Пользователи: правило блокировки, белый список, кто заблокирован
// и откуда были неудачные входы. Черновик и сохранение - в UsersModal (кнопка в подвале).

const hhmm = (iso: string) => {
  const d = new Date(iso)
  const day = d.toDateString() === new Date().toDateString()
  return d.toLocaleString([], day ? { hour: '2-digit', minute: '2-digit' } : { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function LoginGuardSec({
  guard,
  draft,
  onDraft,
  onUnban,
  busy,
  t,
}: {
  guard: LoginGuard
  draft: GuardDraft
  onDraft: (d: GuardDraft) => void
  onUnban: (key: string) => void
  busy: boolean
  t: T
}) {
  const [net, setNet] = useState('')
  const [note, setNote] = useState('')
  const add = (n: string, nt: string) => {
    const v = n.trim()
    if (!v || draft.allow.some((a) => a.net === v)) return
    onDraft({ ...draft, allow: [...draft.allow, { net: v, note: nt.trim() }] })
    setNet('')
    setNote('')
  }
  const mineIn = !!guard.my_allowed || draft.allow.some((a) => a.net === guard.my_ip)

  return (
    <>
      <p className="muted small al-lead">
        {t('После {n} неудачных входов за {w} минут адрес блокируется {ban}, повторно за сутки - на сутки. С адресов из белого списка вход не блокируется никогда, но пароль и код с них все равно нужны.', {
          n: draft.max_failures,
          w: guard.window_minutes,
          ban: banWords(draft.ban_minutes, t),
        })}
      </p>

      <div className="srv-nav-grp rt-cap">{t('Правило')}</div>
      <div className="form-row lg-rule">
        <label className="field">
          <span>{t('Неудачных входов за {w} минут', { w: guard.window_minutes })}</span>
          <select value={draft.max_failures} onChange={(e) => onDraft({ ...draft, max_failures: Number(e.target.value) })}>
            {guard.fail_choices.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{t('Блокировать')}</span>
          <select value={draft.ban_minutes} onChange={(e) => onDraft({ ...draft, ban_minutes: Number(e.target.value) })}>
            {guard.ban_choices.map((m) => (
              <option key={m} value={m}>
                {banWords(m, t)}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="srv-nav-grp rt-cap">{t('Белый список')}</div>
      {draft.allow.length ? (
        <table className="sec-tbl">
          <thead>
            <tr>
              <th>{t('Адрес или подсеть')}</th>
              <th>{t('Подпись')}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {draft.allow.map((a) => (
              <tr key={a.net}>
                <td className="mono nowrap">
                  {a.net}
                  {a.net === guard.my_ip && <span className="type-chip">{t('ваш')}</span>}
                </td>
                <td>{a.note || <span className="muted">-</span>}</td>
                <td className="right">
                  <button className="ghost small" disabled={busy} onClick={() => onDraft({ ...draft, allow: draft.allow.filter((x) => x.net !== a.net) })}>
                    {t('Убрать')}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted small">{t('Пусто: блокируется любой адрес.')}</p>
      )}
      <div className="form-row lg-add">
        <label className="field">
          <span>{t('Адрес или подсеть')}</span>
          <input value={net} onChange={(e) => setNet(e.target.value)} placeholder="203.0.113.7, 203.0.113.0/24" autoComplete="off" onKeyDown={(e) => e.key === 'Enter' && add(net, note)} />
        </label>
        <label className="field">
          <span>{t('Подпись')}</span>
          <input value={note} maxLength={80} onChange={(e) => setNote(e.target.value)} placeholder={t('например, офис')} autoComplete="off" onKeyDown={(e) => e.key === 'Enter' && add(net, note)} />
        </label>
        <button className="ghost" disabled={busy || !net.trim()} onClick={() => add(net, note)}>
          {t('Добавить')}
        </button>
      </div>
      <p className="muted small">
        {t('Ваш адрес сейчас:')} <span className="mono">{guard.my_ip}</span>
        {guard.my_allowed ? (
          <> · {t('в белом списке ({n})', { n: guard.my_allowed })}</>
        ) : !mineIn ? (
          <>
            {' '}
            <button className="linklike" disabled={busy} onClick={() => add(guard.my_ip, t('мой адрес'))}>
              {t('добавить мой адрес')}
            </button>
          </>
        ) : null}
      </p>

      <div className="srv-nav-grp rt-cap">{t('Заблокированы сейчас')}</div>
      {guard.bans.length ? (
        <table className="sec-tbl">
          <thead>
            <tr>
              <th>{t('Адрес')}</th>
              <th>{t('До')}</th>
              <th className="hide-m">{t('Какие логины')}</th>
              <th className="hide-m">{t('Попыток в блокировке')}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {guard.bans.map((b) => (
              <tr key={b.key}>
                <td className="mono nowrap">
                  {b.key}
                  {b.repeat && <span className="type-chip">{t('повторно')}</span>}
                </td>
                <td className="nowrap">{hhmm(b.until)}</td>
                <td className="hide-m">{b.logins || <span className="muted">-</span>}</td>
                <td className="hide-m">{b.blocked}</td>
                <td className="right">
                  <button className="ghost small" disabled={busy} onClick={() => onUnban(b.key)}>
                    {t('Разблокировать')}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted small">{t('Сейчас никто не заблокирован.')}</p>
      )}

      <div className="srv-nav-grp rt-cap">{t('Неудачные входы за сутки')}</div>
      {guard.recent.length ? (
        <table className="sec-tbl">
          <thead>
            <tr>
              <th>{t('Адрес')}</th>
              <th>{t('Неудач')}</th>
              <th>{t('Какие логины')}</th>
              <th className="hide-m">{t('Последняя')}</th>
            </tr>
          </thead>
          <tbody>
            {guard.recent.map((r) => (
              <tr key={r.key}>
                <td className="mono">
                  {r.key}
                  {r.allowed && <span className="type-chip">{t('белый список')}</span>}
                  {r.banned_until && <span className="type-chip lg-banned">{t('заблокирован до {h}', { h: hhmm(r.banned_until) })}</span>}
                </td>
                <td>{r.fails}</td>
                <td>{r.logins.join(', ') || <span className="muted">-</span>}</td>
                <td className="hide-m nowrap">{hhmm(r.last)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted small">{t('За сутки неудачных входов не было.')}</p>
      )}
    </>
  )
}
