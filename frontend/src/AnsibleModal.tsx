import { useEffect, useState } from 'react'
import {
  ApiError,
  getAnsibleAccess,
  issueAnsibleToken,
  revokeAnsibleToken,
  type AnsibleAccess,
} from './api'
import { useI18n } from './i18n'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

// Короткое имя панели для группы kervax_<имя>: kervax.acdev.pro -> acdev
function panelName(host: string): string {
  const parts = host.split('.').filter((p) => p && p !== 'www' && p !== 'kervax')
  return parts.length > 1 ? parts[0] : host
}

// Доступ ansible к списку нод. Плагин инвентаря в репо ansible спрашивает у каждой
// панели её ноды и сам собирает группы: тогда helper'ы на всех панелях обновляет одна
// команда, а не три скопированных списка хостов.
export function AnsibleModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [acc, setAcc] = useState<AnsibleAccess | null>(null)
  const [token, setToken] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const [tick, setTick] = useState(0)
  useEffect(() => {
    getAnsibleAccess()
      .then(setAcc)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized()
        else setErr(e instanceof Error ? e.message : String(e))
      })
  }, [tick, onUnauthorized])

  async function run(fn: () => Promise<unknown>) {
    setBusy(true)
    setErr(null)
    try {
      await fn()
      setTick((x) => x + 1)
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    } finally {
      setBusy(false)
    }
  }

  const issue = () => {
    if (acc?.enabled && !window.confirm(t('Выпустить новый токен? Старый сразу перестанет работать.'))) return
    run(async () => {
      const r = await issueAnsibleToken()
      setToken(r.token)
      setCopied(false)
    })
  }
  const revoke = () => {
    if (!window.confirm(t('Отключить доступ ansible? Плагин инвентаря перестанет получать ноды этой панели.'))) return
    run(async () => {
      await revokeAnsibleToken()
      setToken(null)
    })
  }

  const fmt = (ts?: string | null) => (ts ? new Date(ts).toLocaleString() : '-')
  const snippet = token
    ? `  - name: ${panelName(location.hostname)}\n    url: ${location.origin}\n    token: ${token}\n`
    : ''

  return (
    <div className="modal-backdrop">
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>Ansible</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <p className="muted small">
          {t('Плагин инвентаря в репо ansible (inventory_plugins/kervax_panels.py) спрашивает у каждой панели её ноды и собирает группы: kervax - все ноды с агентом, kervax_outdated - где плейбук что-то обновит. Тогда helper-скрипты на всех панелях обновляет одна команда:')}
        </p>
        <div className="agent-advice-cmd">
          <pre>ansible-playbook playbooks/kervax_helpers.yml -l kervax_outdated</pre>
        </div>
        <p className="muted small">
          {t('Токен открывает только список нод - имена и адреса, без секретов и без управления.')}
        </p>

        {acc == null ? (
          <p className="muted">{t('загрузка…')}</p>
        ) : (
          <div className="settings-group">
            <h4>{acc.enabled ? t('Доступ включен') : t('Доступ выключен')}</h4>
            {acc.enabled && (
              <p className="muted small">
                {t('Токен выпущен: {a}. Ansible спрашивал последний раз: {b}.', {
                  a: fmt(acc.created_at),
                  b: fmt(acc.used_at),
                })}
              </p>
            )}
            {token && (
              <>
                <p className="small">
                  {t('Токен показывается один раз. Добавьте эти строки в inventories/kervax_panels.yml, в список panels:')}
                </p>
                <div className="agent-advice-cmd">
                  <pre>{snippet}</pre>
                  <button
                    className="ghost"
                    onClick={() => {
                      navigator.clipboard?.writeText(snippet)
                      setCopied(true)
                    }}
                  >
                    {copied ? t('Скопировано') : t('Копировать')}
                  </button>
                </div>
              </>
            )}
            {err && <p className="form-error">{err}</p>}
            <div className="modal-actions">
              {acc.enabled && (
                <button className="ghost" onClick={revoke} disabled={busy}>
                  {t('Отключить')}
                </button>
              )}
              <button onClick={issue} disabled={busy}>
                {busy ? t('…') : acc.enabled ? t('Выпустить новый токен') : t('Выпустить токен')}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
