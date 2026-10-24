import { useCallback, useEffect, useRef, useState } from 'react'
import QRCode from 'qrcode'
import {
  ApiError,
  MIN_PASSWORD_LEN,
  changePassword,
  confirmTelegram,
  deleteTelegram,
  disable2FA,
  enable2FA,
  get2FA,
  getMyActivity,
  getTelegram,
  linkTelegram,
  me,
  setup2FA,
  testTelegram,
  updateTelegram,
  type MyActivity,
  type TelegramState,
  type TwoFASetup,
  type UserOut,
} from './api'
import { ROLE_LABEL, ROLE_WHAT, SECTION_LABEL } from './access'
import { CardNav, type SecDef } from './cardLayout'
import { tr, useI18n } from './i18n'
import { useEscape } from './useEscape'
import { NetTile } from './NetLinks'

// "Моя учетка" из меню: своя роль и доступ, последние входы, пароль, вход по коду из
// приложения (2FA) и свои алерты в Telegram. Раскладка как у карточки сервера: раньше это
// были три отдельных окна-формы.

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

const SECS: SecDef[] = [
  { id: 'over', label: 'Сводка', group: '' },
  { id: 'password', label: 'Пароль', group: 'Вход' },
  { id: '2fa', label: 'Код из приложения', group: 'Вход' },
  { id: 'telegram', label: 'Telegram', group: 'Уведомления' },
]

const fmt = (iso: string) => new Date(iso).toLocaleString([], { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' })

export function AccountModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  useEscape(onClose)
  const [sec, setSec] = useState('over')
  const [user, setUser] = useState<UserOut | null>(null)
  const [tfa, setTfa] = useState<boolean | null>(null)
  const [tg, setTg] = useState<TelegramState | null>(null)
  const [act, setAct] = useState<MyActivity | null>(null)
  const mainRef = useRef<HTMLDivElement>(null)

  // ошибка загрузки - текстом вместо вечной "загрузка…"
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setLoadErr(e instanceof Error ? e.message : tr('Ошибка'))
    },
    [onUnauthorized],
  )
  const loadAct = useCallback(() => getMyActivity().then(setAct).catch(fail), [fail])

  useEffect(() => {
    me().then(setUser).catch(fail)
    get2FA()
      .then((s) => setTfa(s.enabled))
      .catch(fail)
    getTelegram().then(setTg).catch(fail)
    loadAct()
  }, [fail, loadAct])

  const go = (id: string) => {
    setSec(id)
    mainRef.current?.scrollTo({ top: 0 })
  }
  const cur = SECS.find((s) => s.id === sec) ?? SECS[0]
  const side: Record<string, string> = {}
  if (tfa != null) side['2fa'] = tfa ? t('вкл') : t('выкл')
  if (tg) side.telegram = tg.ready ? (tg.alerts ? t('вкл') : t('выкл')) : t('нет')

  // разделы и группы: пусто - без ограничений
  const scope = (xs: string[] | null | undefined, label?: Record<string, string>) =>
    xs?.length ? xs.map((x) => (label ? t(label[x] ?? x) : x)).join(', ') : t('все')
  const result = (a: string) =>
    a === 'login_ok'
      ? { text: t('вход'), cls: '' }
      : a === 'login_2fa_fail'
        ? { text: t('неверный код 2FA'), cls: 't-degraded' }
        : a === 'login_fail'
          ? { text: t('неверный пароль'), cls: 't-degraded' }
          : { text: t('заблокирован после перебора'), cls: 't-down' }

  const overview = () => (
    <>
      <p className="muted small al-lead">
        {t('Ваша учетка: пароль, вход по коду из приложения и свои алерты в Telegram. Роль, разделы и группы задает админ в окне Пользователи.')}
      </p>
      <div className="net-tiles srv-tiles">
        {user && (
          <NetTile label={t('Роль')} value={t(ROLE_LABEL[user.role] ?? user.role)} sub={t(ROLE_WHAT[user.role] ?? '')} />
        )}
        {tfa != null && (
          <button className="srv-tile-btn" onClick={() => go('2fa')} title={t('к разделу')}>
            <NetTile
              label={t('Код из приложения')}
              value={tfa ? t('включен') : t('выключен')}
              sub={tfa ? t('при входе спрашивается код') : t('вход только по паролю')}
              tone={tfa ? 'ok' : 'warn'}
            />
          </button>
        )}
        {tg && (
          <button className="srv-tile-btn" onClick={() => go('telegram')} title={t('к разделу')}>
            <NetTile
              label={t('Алерты в Telegram')}
              value={tg.ready ? (tg.alerts ? t('приходят') : t('выключены')) : t('нет')}
              sub={tg.ready ? t('чат {id}', { id: tg.chat_id }) : t('чат не привязан')}
            />
          </button>
        )}
        {act && (
          <NetTile
            label={t('Неудачных входов за сутки')}
            value={String(act.fails_24h)}
            sub={act.fails_24h ? t('под вашим логином') : t('не было')}
            tone={act.fails_24h ? 'warn' : undefined}
          />
        )}
      </div>
      {user && (
        <div className="ans-kv small">
          <span className="muted">{t('Разделы')}</span>
          <span>{scope(user.sections, SECTION_LABEL)}</span>
          <span className="muted">{t('Группы серверов')}</span>
          <span>{scope(user.server_groups)}</span>
          <span className="muted">{t('Группы сайтов')}</span>
          <span>{scope(user.site_groups)}</span>
          {act && (
            <>
              <span className="muted">{t('Пароль менялся')}</span>
              <span>{act.password_changed ? fmt(act.password_changed) : <span className="muted">{t('в журнале нет')}</span>}</span>
            </>
          )}
        </div>
      )}
      {act && act.logins.length > 0 && (
        <>
          <div className="srv-nav-grp rt-cap">{t('Последние входы')}</div>
          <table className="sec-tbl">
            <tbody>
              {act.logins.map((l, i) => {
                const r = result(l.action)
                return (
                  <tr key={i}>
                    <td className="small nowrap">{fmt(l.ts)}</td>
                    <td className="small">
                      <span className="mono">{l.ip}</span>
                    </td>
                    <td className="small">
                      <span className={r.cls}>{r.text}</span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </>
      )}
    </>
  )

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal al-small" onClick={(e) => e.stopPropagation()}>
        <div className="al-head">
          <h3>{t('Моя учетка')}</h3>
          {user && <span className="muted small">{user.username}</span>}
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="al-body">
          <CardNav secs={SECS} active={cur.id} info={{}} side={side} onGo={go} />
          <div className="al-main" ref={mainRef}>
            <div className="srv-main-head">
              <h4>{t(cur.label)}</h4>
            </div>
            <div className="srv-sec">
              {loadErr && <p className="form-error small">{loadErr}</p>}
              {cur.id === 'over' && overview()}
              {cur.id === 'password' && <PasswordSec changed={act?.password_changed ?? null} onDone={loadAct} onUnauthorized={onUnauthorized} />}
              {cur.id === '2fa' && <TwoFASec enabled={tfa} failed={!!loadErr} onChange={setTfa} />}
              {cur.id === 'telegram' && <TelegramSec st={tg} failed={!!loadErr} onChange={setTg} />}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// Пароль: смена закрывает все прежние сессии, кроме этого окна (ему выдается новый токен)
function PasswordSec({ changed, onDone, onUnauthorized }: { changed: string | null; onDone: () => void; onUnauthorized: () => void }) {
  const { t } = useI18n()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  const tooShort = next.length > 0 && next.length < MIN_PASSWORD_LEN
  const mismatch = confirm.length > 0 && next !== confirm
  const canSubmit = !!current && next.length >= MIN_PASSWORD_LEN && next === confirm && !busy

  async function submit() {
    setBusy(true)
    setError(null)
    setDone(false)
    try {
      await changePassword(current, next)
      setDone(true)
      setCurrent('')
      setNext('')
      setConfirm('')
      onDone()
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return onUnauthorized()
      setError(err instanceof Error ? err.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <p className="muted small al-lead">
        {t('Смена пароля завершит все другие сессии: везде, кроме этого окна, придется войти заново.')}{' '}
        {changed ? t('Последний раз пароль менялся {d}.', { d: fmt(changed) }) : ''}
      </p>
      <div className="al-narrow ac-form">
        <label className="field">
          <span>{t('Текущий пароль')}</span>
          <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" />
        </label>
        <label className="field">
          <span>{t('Новый пароль (мин. {n} символов)', { n: MIN_PASSWORD_LEN })}</span>
          <input type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />
        </label>
        <label className="field">
          <span>{t('Повторите новый пароль')}</span>
          <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
        </label>
      </div>
      {tooShort && <p className="form-error">{t('Пароль должен быть не короче {n} символов', { n: MIN_PASSWORD_LEN })}</p>}
      {mismatch && <p className="form-error">{t('Пароли не совпадают')}</p>}
      {error && <p className="form-error">{error}</p>}
      {done && <p className="form-ok">{t('Пароль изменён. Все прежние сессии завершены.')}</p>}
      <div className="ans-acts">
        <button onClick={submit} disabled={!canSubmit}>
          {busy ? t('…') : t('Сменить пароль')}
        </button>
      </div>
    </>
  )
}

// Вход по коду из приложения (TOTP): включить - QR и подтверждение кодом, выключить - текущим кодом
function TwoFASec({ enabled, failed, onChange }: { enabled: boolean | null; failed?: boolean; onChange: (on: boolean) => void }) {
  const { t } = useI18n()
  const [setup, setSetup] = useState<TwoFASetup | null>(null)
  const [qr, setQr] = useState<{ uri: string; url: string } | null>(null)
  const [otp, setOtp] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!setup) return
    let alive = true
    QRCode.toDataURL(setup.otpauth_uri, { margin: 1, width: 240 })
      .then((url) => alive && setQr({ uri: setup.otpauth_uri, url }))
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [setup])
  const qrUrl = setup && qr?.uri === setup.otpauth_uri ? qr.url : null

  const run = async (fn: () => Promise<void>, wrong: string) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
    } catch (err) {
      setError(err instanceof Error ? err.message : wrong)
    } finally {
      setBusy(false)
    }
  }
  const startSetup = () =>
    run(async () => {
      setSetup(await setup2FA())
      setOtp('')
    }, t('Ошибка'))
  const confirmEnable = () =>
    run(async () => {
      const s = await enable2FA(otp.trim())
      onChange(s.enabled)
      setSetup(null)
      setOtp('')
    }, t('Неверный код'))
  const turnOff = () =>
    run(async () => {
      const s = await disable2FA(otp.trim())
      onChange(s.enabled)
      setOtp('')
    }, t('Неверный код'))

  const codeField = (focus: boolean) => (
    <label className="field al-narrow">
      <span>{t('Код из приложения (2FA)')}</span>
      <input value={otp} onChange={(e) => setOtp(e.target.value)} inputMode="numeric" placeholder="123456" autoFocus={focus} />
    </label>
  )

  if (enabled == null) return failed ? null : <p className="muted small">{t('загрузка…')}</p>
  if (enabled)
    return (
      <>
        <p className="form-ok">{t('2FA включена. Вход требует код из приложения.')}</p>
        <p className="muted small">{t('Чтобы отключить, введите текущий код из приложения-аутентификатора.')}</p>
        {codeField(false)}
        {error && <p className="form-error">{error}</p>}
        <div className="ans-acts">
          <button className="danger-solid" onClick={turnOff} disabled={busy || !otp.trim()}>
            {busy ? t('…') : t('Отключить 2FA')}
          </button>
        </div>
      </>
    )
  if (setup)
    return (
      <>
        <p className="muted small al-lead">
          {t('Отсканируйте QR в приложении-аутентификаторе (Google Authenticator, Aegis, 1Password) или введите ключ вручную, затем подтвердите кодом.')}
        </p>
        <div className="qr-wrap ac-qr">
          {qrUrl ? <img src={qrUrl} alt="2FA QR" width={200} height={200} /> : <span className="muted small">{t('генерация QR…')}</span>}
        </div>
        <p className="muted small">
          {t('Ключ:')} <span className="mono">{setup.secret}</span>
        </p>
        {codeField(true)}
        {error && <p className="form-error">{error}</p>}
        <div className="ans-acts">
          <button onClick={confirmEnable} disabled={busy || !otp.trim()}>
            {busy ? t('…') : t('Включить')}
          </button>
          <button className="ghost" onClick={() => setSetup(null)}>
            {t('Отмена')}
          </button>
        </div>
      </>
    )
  return (
    <>
      <p className="muted small al-lead">
        {t('Добавьте второй фактор к входу в панель — одноразовый код из приложения-аутентификатора (TOTP).')}
      </p>
      {error && <p className="form-error">{error}</p>}
      <div className="ans-acts">
        <button onClick={startSetup} disabled={busy}>
          {busy ? t('…') : t('Включить 2FA')}
        </button>
      </div>
    </>
  )
}

// Свои алерты: свой чат вместо общего канала панели.
//
// chat id руками не спрашиваем: узнать свой без сторонних ботов человек не может, а перепутать
// чужой - запросто, и тогда алерты про инфраструктуру уходят незнакомцу. Поэтому привязка
// кодом: панель выдает код, человек шлет его боту, панель находит код среди сообщений бота и
// запоминает чат отправителя. Ручной ввод оставлен для групповых чатов и каналов (там своего
// "Start" нет).
function TelegramSec({ st, failed, onChange }: { st: TelegramState | null; failed?: boolean; onChange: (s: TelegramState) => void }) {
  const { t } = useI18n()
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [note, setNote] = useState('')
  const [manual, setManual] = useState(false)
  const [chat, setChat] = useState(st?.chat_id ?? '')
  const [token, setToken] = useState('')

  const run = async (fn: () => Promise<TelegramState>, ok = '') => {
    setBusy(true)
    setErr('')
    setNote('')
    try {
      const s = await fn()
      onChange(s)
      setChat(s.chat_id)
      if (ok) setNote(ok)
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  const getCode = async () => {
    setBusy(true)
    setErr('')
    setNote('')
    try {
      const r = await linkTelegram()
      setCode(r.code)
      if (st && r.bot) onChange({ ...st, bot: r.bot })
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  if (!st) return failed ? null : <p className="muted small">{t('загрузка…')}</p>
  return (
    <>
      <p className="muted small al-lead">
        {t('Приходят алерты только по тому, что доступно вашей учётной записи — по её разделам и группам.')}
      </p>
      <div className="tg-state">
        <span className={`sdot ${st.ready ? 'sdot-up' : 'sdot-down'}`} />
        {st.ready ? t('Привязано: чат {id}', { id: st.chat_id }) : t('Не привязано — алерты не приходят')}
        {st.bot && <span className="type-chip mono">{st.bot}</span>}
        {st.own_token && <span className="type-chip">{t('свой бот')}</span>}
      </div>

      {st.ready && (
        <label className="tg-row">
          <input type="checkbox" checked={st.alerts} disabled={busy} onChange={(e) => run(() => updateTelegram({ alerts: e.target.checked }))} />
          {t('Получать алерты')}
        </label>
      )}

      {!st.ready && (
        <div className="tg-steps">
          {code ? (
            <>
              <div className="tg-step">{t('1. Напишите боту {bot} это сообщение:', { bot: st.bot || '' })}</div>
              <div className="agent-advice-cmd">
                <pre>{code}</pre>
                <button className="ghost" onClick={() => navigator.clipboard?.writeText(code)}>
                  {t('Копировать')}
                </button>
              </div>
              <div className="tg-step">{t('2. Потом нажмите «Проверить привязку».')}</div>
              <button className="primary" disabled={busy} onClick={() => run(confirmTelegram, t('Готово — чат привязан'))}>
                {busy ? t('проверяем…') : t('Проверить привязку')}
              </button>
            </>
          ) : (
            <button className="primary" disabled={busy} onClick={getCode}>
              {t('Привязать Telegram')}
            </button>
          )}
        </div>
      )}

      {err && <p className="form-error">{err}</p>}
      {note && <p className="tg-ok">{note}</p>}

      <div className="tg-actions">
        {st.ready && (
          <>
            <button className="ghost" disabled={busy} onClick={() => run(testTelegram, t('Тестовое сообщение отправлено'))}>
              {t('Отправить тест')}
            </button>
            <button
              className="ghost"
              disabled={busy}
              onClick={() => {
                setCode('')
                run(deleteTelegram, t('Отвязано'))
              }}
            >
              {t('Отвязать')}
            </button>
          </>
        )}
        <button className="ghost" onClick={() => setManual(!manual)}>
          {manual ? t('скрыть ручные настройки') : t('Настроить вручную')}
        </button>
      </div>

      {manual && (
        <div className="tg-manual al-narrow">
          <label>
            {t('ID чата')}
            <input className="mono" value={chat} onChange={(e) => setChat(e.target.value)} placeholder="-1001234567890" />
          </label>
          <label>
            {t('Токен своего бота (пусто — общий бот панели)')}
            <input
              className="mono"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder={st.own_token ? t('задан — введите новый, чтобы заменить') : ''}
            />
          </label>
          {/* пустое поле при заданном своем боте значит не менять, иначе смена одного чата молча
              стирала токен. Вернуться на общий бот - отдельной кнопкой */}
          <button className="primary" disabled={busy} onClick={() => run(() => updateTelegram({
            chat_id: chat.trim(), ...(token.trim() || !st.own_token ? { token: token.trim() } : {}),
          }), t('Сохранено'))}>
            {t('Сохранить')}
          </button>
          {st.own_token && (
            <button className="ghost" disabled={busy} onClick={() => run(() => updateTelegram({ token: '' }), t('Сохранено'))}>
              {t('Вернуться на общий бот')}
            </button>
          )}
          <p className="muted small">
            {t('Свой бот нужен, только если хотите отдельного бота или свой прокси к Telegram. Для группы добавьте бота в неё и укажите её ID.')}
          </p>
        </div>
      )}
    </>
  )
}
