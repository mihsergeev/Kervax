import { useEffect, useState } from 'react'
import { ApiError, getAlerts, putAlerts, testAlerts } from './api'
import { ServerAlertRulesSection, SiteAlertRulesSection } from './ServerAlertRules'
import { useI18n } from './i18n'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

export function AlertsModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [tgToken, setTgToken] = useState('')
  const [tgChat, setTgChat] = useState('')
  const [tgApi, setTgApi] = useState('')
  const [webhook, setWebhook] = useState('')
  const [flood, setFlood] = useState(6)
  const [muted, setMuted] = useState(false)
  const [enabled, setEnabled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    getAlerts()
      .then((c) => {
        setTgToken(c.telegram_token)
        setTgChat(c.telegram_chat)
        setTgApi(c.telegram_api)
        setWebhook(c.webhook)
        setFlood(c.flood_threshold)
        setMuted(c.muted)
        setEnabled(c.enabled)
      })
      .catch(() => onUnauthorized())
  }, [onUnauthorized])

  // Сохраняет то, что сейчас в форме, и возвращает актуальный конфиг.
  async function persist() {
    const c = await putAlerts({
      telegram_token: tgToken,
      telegram_chat: tgChat,
      telegram_api: tgApi,
      webhook,
      flood_threshold: flood,
      muted,
    })
    setTgApi(c.telegram_api)
    setEnabled(c.enabled)
    setMuted(c.muted)
    setFlood(c.flood_threshold)
    return c
  }

  async function save() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      await persist()
      setMsg(t('Сохранено.'))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  // Тест проверяет то, что на экране: сперва сохраняем форму, затем шлём тест.
  async function test() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const c = await persist()
      if (!c.enabled) {
        setErr(t('Заполните Telegram (токен и Chat ID) или Webhook.'))
        return
      }
      const r = await testAlerts()
      if (r.sent) setMsg(t('Тестовое уведомление отправлено.'))
      else setErr(r.errors.join('; ') || t('Не отправлено — проверьте настройки.'))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Алерты')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <p className="muted small">
          {t('Уведомления о падении/восстановлении мониторов и скором истечении сертификатов.')}
        </p>

        <div className="settings-group">
          <h4>Telegram</h4>
          <label className="field">
            <span>{t('Токен бота')}</span>
            <input value={tgToken} onChange={(e) => setTgToken(e.target.value)} placeholder="123456:ABC…" />
          </label>
          <label className="field">
            <span>Chat ID</span>
            <input value={tgChat} onChange={(e) => setTgChat(e.target.value)} placeholder="-1001234567890" />
          </label>
          <label className="field">
            <span>{t('Адрес API (для обхода блокировок)')}</span>
            <input
              value={tgApi}
              onChange={(e) => setTgApi(e.target.value)}
              placeholder="https://api.telegram.org"
            />
          </label>
          <p className="muted small">
            {t('Свой прокси/зеркало Bot API, если api.telegram.org недоступен. Пусто = по умолчанию.')}
          </p>
        </div>

        <div className="settings-group">
          <h4>Webhook</h4>
          <label className="field">
            <span>URL</span>
            <input value={webhook} onChange={(e) => setWebhook(e.target.value)} placeholder="https://…" />
          </label>
        </div>

        <div className="settings-group">
          <h4>{t('Антифлуд')}</h4>
          <label className="field">
            <span>{t('Группировать при ≥ N алертов за цикл (0 = выкл)')}</span>
            <input
              type="number"
              min={0}
              max={1000}
              value={flood}
              onChange={(e) => setFlood(Math.max(0, Number(e.target.value) || 0))}
            />
          </label>
          <p className="muted small">
            {t('При массовых событиях (упал стек/аплинк) шлём один дайджест вместо потока сообщений.')}
          </p>
        </div>

        <div className="settings-group">
          <label className="check-toggle">
            <input
              type="checkbox"
              checked={muted}
              onChange={(e) => setMuted(e.target.checked)}
            />
            <span>
              <b>{t('Не слать алерты (пауза)')}</b>
              <span className="muted small">
                {t('Временно тушит все уведомления. Инциденты продолжают фиксироваться.')}
              </span>
            </span>
          </label>
        </div>

        <p className="muted small">
          {muted
            ? t('⏸ Алерты на паузе')
            : enabled
              ? t('Каналы настроены ✓')
              : t('Каналы не настроены')}
        </p>

        <SiteAlertRulesSection onUnauthorized={onUnauthorized} />
        <ServerAlertRulesSection onUnauthorized={onUnauthorized} />

        {err && <p className="form-error">{err}</p>}
        {msg && <p className="form-ok">{msg}</p>}
        <div className="modal-actions">
          <button className="ghost" onClick={test} disabled={busy}>
            {t('Тест')}
          </button>
          <button onClick={save} disabled={busy}>
            {busy ? t('…') : t('Сохранить')}
          </button>
        </div>
      </div>
    </div>
  )
}
