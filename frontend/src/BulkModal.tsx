import { useState } from 'react'
import { ApiError, bulkUpdateChecks, type CheckBulk } from './api'
import { useI18n } from './i18n'

type Props = {
  onClose: () => void
  onApplied: () => void
  onUnauthorized: () => void
  ids?: number[] // задан → применяем только к выбранным; иначе ко всем
}

type Kind = 'num' | 'days' | 'bool' | 'text'
const FIELDS: { key: keyof CheckBulk; label: string; kind: Kind; def: string }[] = [
  { key: 'interval_seconds', label: 'Интервал, сек', kind: 'num', def: '60' },
  { key: 'degraded_ms', label: 'Порог «медленно», мс', kind: 'num', def: '2000' },
  { key: 'retries', label: 'Повторы при сбое', kind: 'num', def: '3' },
  { key: 'alert_after_failures', label: 'Алерт после N сбоев подряд', kind: 'num', def: '3' },
  { key: 'degraded_after_failures', label: 'Алерт «медленно» после N подряд', kind: 'num', def: '10' },
  { key: 'expected_status', label: 'Ожидаемый статус', kind: 'text', def: '200-399' },
  { key: 'ssl_warn_days', label: 'SSL: напоминать за, дн.', kind: 'days', def: '14, 7, 1' },
  { key: 'domain_warn_days', label: 'Домен: напоминать за, дн.', kind: 'days', def: '7, 1' },
  // булевы переключатели (вкл/выкл разом)
  { key: 'check_locations', label: 'Проверять из локаций (прокси)', kind: 'bool', def: 'off' },
  { key: 'probe_local', label: 'Проверять локально, с самого сервера', kind: 'bool', def: 'off' },
  { key: 'check_all_ips', label: 'Проверять все IP-адреса домена', kind: 'bool', def: 'off' },
  { key: 'check_ssl', label: 'Следить за SSL-сертификатом', kind: 'bool', def: 'on' },
  { key: 'check_domain', label: 'Следить за сроком домена', kind: 'bool', def: 'on' },
]

const parseDays = (s: string) => s.split(/[^0-9]+/).filter(Boolean).map(Number)

export function BulkModal({ onClose, onApplied, onUnauthorized, ids }: Props) {
  const { t } = useI18n()
  const [on, setOn] = useState<Record<string, boolean>>({})
  const [val, setVal] = useState<Record<string, string>>(() =>
    Object.fromEntries(FIELDS.map((f) => [f.key, f.def])),
  )
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const anyOn = FIELDS.some((f) => on[f.key])

  async function apply() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const fields: CheckBulk = {}
      for (const f of FIELDS) {
        if (!on[f.key]) continue
        if (f.kind === 'days') (fields[f.key] as number[]) = parseDays(val[f.key])
        else if (f.kind === 'bool') (fields[f.key] as boolean) = val[f.key] === 'on'
        else if (f.kind === 'text') (fields[f.key] as string) = val[f.key]
        else (fields[f.key] as number) = Number(val[f.key])
      }
      const r = await bulkUpdateChecks(fields, ids)
      setMsg(t('Применено к {n} мониторам.', { n: r.updated }))
      onApplied()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal bulk-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>
            {ids
              ? t('Применить к выбранным ({n})', { n: ids.length })
              : t('Применить ко всем мониторам')}
          </h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <p className="muted small">
          {ids
            ? t('Отметьте поля, которые применить к {n} выбранным мониторам. Остальные настройки не тронутся.', { n: ids.length })
            : t('Отметьте поля, которые применить ко всем мониторам разом. Остальные настройки не тронутся.')}
        </p>

        <div className="bulk-list">
          {FIELDS.map((f) => (
            <div key={f.key} className={`bulk-row${on[f.key] ? ' bulk-row-on' : ''}`}>
              <label className="checkbox bulk-check">
                <input
                  type="checkbox"
                  checked={!!on[f.key]}
                  onChange={(e) => setOn((s) => ({ ...s, [f.key]: e.target.checked }))}
                />
                {t(f.label)}
              </label>
              {f.kind === 'bool' ? (
                <select
                  value={val[f.key]}
                  disabled={!on[f.key]}
                  onChange={(e) => setVal((s) => ({ ...s, [f.key]: e.target.value }))}
                >
                  <option value="on">{t('Включить')}</option>
                  <option value="off">{t('Выключить')}</option>
                </select>
              ) : (
                <input
                  value={val[f.key]}
                  disabled={!on[f.key]}
                  onChange={(e) => setVal((s) => ({ ...s, [f.key]: e.target.value }))}
                  placeholder={f.def}
                />
              )}
            </div>
          ))}
        </div>

        {err && <p className="form-error">{err}</p>}
        {msg && <p className="form-ok">{msg}</p>}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            {t('Отмена')}
          </button>
          <button onClick={apply} disabled={!anyOn || busy}>
            {busy ? t('…') : t('Применить')}
          </button>
        </div>
      </div>
    </div>
  )
}
