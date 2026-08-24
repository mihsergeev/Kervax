import { useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  getServerAlertRules,
  getSiteAlertRules,
  putServerAlertRules,
  putSiteAlertRules,
  type ServerAlertRule as Rule,
  type ServerAlertRules as Rules,
} from './api'
import { useI18n } from './i18n'

// Редактор ГЛОБАЛЬНЫХ правил алертов (серверов или сайтов): тумблер вкл/выкл + текст.
// Правила применяются КО ВСЕМ. Пороги и точечные исключения — в карточке сущности.
function AlertRulesSection({
  title,
  getRules,
  putRules,
  onUnauthorized,
  hint,
}: {
  title: string
  getRules: () => Promise<Rules>
  putRules: (r: Record<string, Rule>) => Promise<Rules>
  onUnauthorized: () => void
  hint?: string
}) {
  const { t } = useI18n()
  const [data, setData] = useState<Rules | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    },
    [onUnauthorized, t],
  )

  useEffect(() => {
    getRules().then(setData).catch(fail)
  }, [getRules, fail])

  function setRule(kind: string, patch: Partial<Rule>) {
    setData((d) =>
      d ? { ...d, rules: { ...d.rules, [kind]: { ...d.rules[kind], ...patch } } } : d,
    )
    setMsg(null)
  }

  async function save() {
    if (!data) return
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      // все правила — глобальные (ко всем); scope больше не задаётся тут
      const rules: Record<string, Rule> = Object.fromEntries(
        Object.entries(data.rules).map(([k, r]) => [
          k,
          { ...r, scope_type: 'all' as const, scope: [] },
        ]),
      )
      setData(await putRules(rules))
      setMsg(t('Сохранено.'))
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  if (!data) return <p className="muted small">{t('загрузка…')}</p>

  return (
    <div className="settings-group">
      <h4>{title}</h4>
      <div className="arule-list">
        {data.kinds.map((k) => {
          const r = data.rules[k.key]
          const expanded = open === k.key
          return (
            <div key={k.key} className={`arule${r.enabled ? '' : ' arule-off'}`}>
              <div
                className="arule-head"
                role="button"
                tabIndex={0}
                onClick={() => setOpen(expanded ? null : k.key)}
                onKeyDown={(e) =>
                  (e.key === 'Enter' || e.key === ' ') && setOpen(expanded ? null : k.key)
                }
              >
                <input
                  type="checkbox"
                  className="arule-check"
                  checked={r.enabled}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => setRule(k.key, { enabled: e.target.checked })}
                />
                <span className="arule-name">{k.label}</span>
                <span className="arule-sum muted small">
                  {r.enabled ? t('ко всем') : t('выключен')}
                </span>
                <span className="arule-caret">{expanded ? '▾' : '▸'}</span>
              </div>

              {expanded && r.enabled && (
                <div className="arule-body">
                  <label className="field">
                    <span className="muted small">{t('Текст сообщения')}</span>
                    <input
                      className="rule-text"
                      value={r.text}
                      placeholder={k.default_text}
                      onChange={(e) => setRule(k.key, { text: e.target.value })}
                    />
                  </label>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {err && <p className="form-error">{err}</p>}
      {msg && <p className="form-ok">{msg}</p>}
      <p className="muted small">
        {t('Правила действуют на все. Пороги и исключения — в настройках конкретного сервера/монитора.')}
      </p>
      {hint && <p className="muted small">{hint}</p>}
      <div className="modal-actions" style={{ justifyContent: 'flex-start' }}>
        <button onClick={save} disabled={busy}>
          {busy ? t('…') : t('Сохранить правила')}
        </button>
      </div>
    </div>
  )
}

export function ServerAlertRulesSection({ onUnauthorized }: { onUnauthorized: () => void }) {
  const { t } = useI18n()
  return (
    <AlertRulesSection
      title={t('Алерты серверов')}
      getRules={getServerAlertRules}
      putRules={putServerAlertRules}
      onUnauthorized={onUnauthorized}
    />
  )
}

export function SiteAlertRulesSection({ onUnauthorized }: { onUnauthorized: () => void }) {
  const { t } = useI18n()
  return (
    <AlertRulesSection
      title={t('Алерты сайтов')}
      getRules={getSiteAlertRules}
      putRules={putSiteAlertRules}
      onUnauthorized={onUnauthorized}
      hint={t('По умолчанию алерт выглядит так: «адрес 🔴 — имя-ссылкой на монитор — текст ошибки». Свой текст в поле выше отключает адрес и ссылку.')}
    />
  )
}
