import { useEffect, useState } from 'react'
import { AdoptSitesModal } from './AdoptSitesModal'
import { discoveredDomains, ignoreDomains, type Discovered } from './api'
import { domainSuggestionsEnabled, newDiscoveredDomains, setDomainSuggestionsEnabled } from './domainDiscovery'
import { useI18n } from './i18n'

const REFRESH_MS = 120_000
const PREVIEW_LIMIT = 3

export function HomeDomainSuggestions({ username, onAdded, onOpenCheck }: {
  username: string
  onAdded: () => void
  onOpenCheck: (id: number) => void
}) {
  const { t } = useI18n()
  const [enabled, setEnabled] = useState(() => domainSuggestionsEnabled(username))
  const [found, setFound] = useState<Discovered | null>(null)
  const [revision, setRevision] = useState(0)
  const [wizard, setWizard] = useState<string | null>(null) // empty string opens the full list
  const [hiding, setHiding] = useState('')
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (!enabled) return
    let stopped = false
    let pending = false
    const refresh = async () => {
      if (pending || document.hidden) return
      pending = true
      try {
        const result = await discoveredDomains()
        if (!stopped) setFound(result)
      } catch {
        // Discovery is optional. Keep the last successful result.
      } finally {
        pending = false
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, REFRESH_MS)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      stopped = true
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [enabled, revision])

  if (!enabled || !found) return null
  const candidates = newDiscoveredDomains(found)

  const updateIgnored = (domains: string[], ignore: boolean) => {
    setFound((prev) => {
      if (!prev) return prev
      const ignored = new Set(prev.ignored)
      for (const domain of domains) {
        if (ignore) ignored.add(domain)
        else ignored.delete(domain)
      }
      return { ...prev, ignored: [...ignored] }
    })
    setRevision((n) => n + 1)
  }

  const hide = async (domain: string) => {
    setHiding(domain)
    setFailed(false)
    try {
      await ignoreDomains([domain])
      updateIgnored([domain], true)
    } catch {
      setFailed(true)
    } finally {
      setHiding('')
    }
  }

  return (
    <>
      {candidates.length > 0 && (
        <details className="home-domain-suggestions">
          <summary className="muted">{t('Новые домены')} <span className="net-n">{candidates.length}</span></summary>
          <ul>
            {candidates.slice(0, PREVIEW_LIMIT).map(({ domain }) => (
              <li key={domain}>
                <span className="mono">{domain}</span>
                <div className="home-domain-actions">
                  <button className="ghost small" onClick={() => setWizard(domain)}>{t('Добавить')}</button>
                  <button className="linklike small" disabled={!!hiding}
                    title={t('Убрать домен из предложений для всех пользователей панели')}
                    onClick={() => void hide(domain)}>
                    {hiding === domain ? '…' : t('Не мониторить')}
                  </button>
                </div>
              </li>
            ))}
          </ul>
          {failed && <p className="form-error" role="status">{t('Не удалось скрыть домен. Повторите попытку.')}</p>}
          <div className="home-domain-actions">
            {candidates.length > PREVIEW_LIMIT && (
              <button className="linklike small" onClick={() => setWizard('')}>{t('Показать все ({n})', { n: candidates.length })}</button>
            )}
            <button className="linklike small"
              title={t('Вернуть подсказки можно в разделе «Сайты»')}
              onClick={() => {
                setDomainSuggestionsEnabled(username, false)
                setEnabled(false)
              }}>
              {t('Не показывать на главной')}
            </button>
          </div>
        </details>
      )}
      {wizard !== null && (
        <AdoptSitesModal
          title={t('Домены, найденные на серверах')}
          items={wizard ? found.domains.filter(({ domain }) => domain === wizard) : found.domains}
          hosts={found.hosts}
          groups={found.groups}
          ignored={found.ignored}
          preselected={wizard}
          onClose={() => setWizard(null)}
          onDone={(hosts, created) => {
            setFound((prev) => prev && { ...prev, hosts })
            setRevision((n) => n + 1)
            if (created > 0) onAdded()
          }}
          onIgnored={updateIgnored}
          onOpenCheck={(id) => {
            setWizard(null)
            onOpenCheck(id)
          }}
        />
      )}
    </>
  )
}
