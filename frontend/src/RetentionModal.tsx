import { useEffect, useState } from 'react'
import { ApiError, getRetention, putRetention } from './api'
import { useI18n } from './i18n'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

const PRESETS = [
  { label: '30д', days: 30 },
  { label: '90д', days: 90 },
  { label: 'полгода', days: 180 },
  { label: 'год', days: 365 },
  { label: '2 года', days: 730 },
]

function RetentionField({
  label,
  hint,
  value,
  onChange,
}: {
  label: string
  hint: string
  value: number
  onChange: (v: number) => void
}) {
  const { t } = useI18n()
  return (
    <div className="settings-group">
      <h4>{label}</h4>
      <p className="muted small">{hint}</p>
      <label className="field">
        <span>{t('Хранить, дней')}</span>
        <input
          type="number"
          min={1}
          max={3650}
          value={value}
          onChange={(e) => onChange(e.target.value === '' ? 1 : Number(e.target.value))}
        />
      </label>
      <div className="win-switch">
        {PRESETS.map((p) => (
          <button
            key={p.days}
            className={`win-btn${value === p.days ? ' win-btn-active' : ''}`}
            onClick={() => onChange(p.days)}
          >
            {t(p.label)}
          </button>
        ))}
      </div>
    </div>
  )
}

export function RetentionModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [serverDays, setServerDays] = useState(30)
  const [sampleDays, setSampleDays] = useState(60)
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    getRetention()
      .then((r) => {
        setServerDays(r.server_days)
        setSampleDays(r.sample_days)
        setLoaded(true)
      })
      .catch(() => onUnauthorized())
  }, [onUnauthorized])

  async function save() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const r = await putRetention({ server_days: serverDays, sample_days: sampleDays })
      setServerDays(r.server_days)
      setSampleDays(r.sample_days)
      setMsg(t('Сохранено.'))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Хранение данных')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <p className="muted small">
          {t('Данные старше указанного срока удаляются автоматически. Дольше хранить — больше места в БД.')}
        </p>

        {!loaded ? (
          <p className="muted">{t('загрузка…')}</p>
        ) : (
          <>
            <RetentionField
              label={t('Метрики серверов')}
              hint={t('CPU / память / сеть / диск с агентов.')}
              value={serverDays}
              onChange={setServerDays}
            />
            <RetentionField
              label={t('История проверок сайтов')}
              hint={t('Время ответа и статусы мониторов, инциденты.')}
              value={sampleDays}
              onChange={setSampleDays}
            />
            {err && <p className="form-error">{err}</p>}
            {msg && <p className="form-ok">{msg}</p>}
            <div className="modal-actions">
              <button onClick={save} disabled={busy}>
                {busy ? t('…') : t('Сохранить')}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
