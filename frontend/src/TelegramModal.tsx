import { useCallback, useEffect, useState } from 'react'
import {
  deleteTelegram,
  getTelegram,
  linkTelegram,
  confirmTelegram,
  testTelegram,
  updateTelegram,
  type TelegramState,
} from './api'
import { useI18n } from './i18n'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

// Персональные алерты: свой чат вместо общего канала панели.
//
// chat id руками не спрашиваем: узнать свой без сторонних ботов человек не может,
// а перепутать чужой — запросто, и тогда алерты про инфраструктуру уходят незнакомцу.
// Поэтому привязка кодом: панель выдаёт код, человек шлёт его боту, панель находит
// код среди сообщений бота и запоминает чат отправителя. Ручной ввод оставлен для
// групповых чатов и каналов (там своего «Start» нет).
export function TelegramModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [st, setSt] = useState<TelegramState | null>(null)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [note, setNote] = useState('')
  const [manual, setManual] = useState(false)
  const [chat, setChat] = useState('')
  const [token, setToken] = useState('')

  const load = useCallback(() => {
    getTelegram()
      .then((s) => {
        setSt(s)
        setChat(s.chat_id)
      })
      .catch(() => onUnauthorized())
  }, [onUnauthorized])

  useEffect(load, [load])

  const run = async (fn: () => Promise<TelegramState>, ok = '') => {
    setBusy(true)
    setErr('')
    setNote('')
    try {
      const s = await fn()
      setSt(s)
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
      setSt((s) => (s ? { ...s, bot: r.bot || s.bot } : s))
    } catch (e) {
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Мои алерты в Telegram')}</h3>
          <button className="ghost" onClick={onClose}>{t('Закрыть')}</button>
        </div>

        {st == null ? (
          <p className="muted">{t('загрузка…')}</p>
        ) : (
          <>
            <p className="muted small">
              {t('Приходят алерты только по тому, что доступно вашей учётной записи — по её разделам и группам.')}
            </p>

            <div className="tg-state">
              <span className={`sdot ${st.ready ? 'sdot-up' : 'sdot-down'}`} />
              {st.ready
                ? t('Привязано: чат {id}', { id: st.chat_id })
                : t('Не привязано — алерты не приходят')}
              {st.bot && <span className="type-chip mono">{st.bot}</span>}
              {st.own_token && <span className="type-chip">{t('свой бот')}</span>}
            </div>

            {st.ready && (
              <label className="tg-row">
                <input
                  type="checkbox"
                  checked={st.alerts}
                  disabled={busy}
                  onChange={(e) => run(() => updateTelegram({ alerts: e.target.checked }))}
                />
                {t('Получать алерты')}
              </label>
            )}

            {!st.ready && (
              <div className="tg-steps">
                {code ? (
                  <>
                    <div className="tg-step">
                      {t('1. Напишите боту {bot} это сообщение:', { bot: st.bot || '' })}
                    </div>
                    <div className="agent-advice-cmd">
                      <pre>{code}</pre>
                      <button
                        className="ghost"
                        onClick={() => navigator.clipboard?.writeText(code)}
                      >
                        {t('Копировать')}
                      </button>
                    </div>
                    <div className="tg-step">{t('2. Потом нажмите «Проверить привязку».')}</div>
                    <button
                      className="primary"
                      disabled={busy}
                      onClick={() => run(confirmTelegram, t('Готово — чат привязан'))}
                    >
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
                  <button
                    className="ghost"
                    disabled={busy}
                    onClick={() => run(testTelegram, t('Тестовое сообщение отправлено'))}
                  >
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
              <div className="tg-manual">
                <label>
                  {t('ID чата')}
                  <input
                    className="mono"
                    value={chat}
                    onChange={(e) => setChat(e.target.value)}
                    placeholder="-1001234567890"
                  />
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
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() =>
                    run(
                      () => updateTelegram({ chat_id: chat.trim(), token: token.trim() }),
                      t('Сохранено'),
                    )
                  }
                >
                  {t('Сохранить')}
                </button>
                <p className="muted small">
                  {t('Свой бот нужен, только если хотите отдельного бота или свой прокси к Telegram. Для группы добавьте бота в неё и укажите её ID.')}
                </p>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
