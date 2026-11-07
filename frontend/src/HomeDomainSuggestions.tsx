import { useEffect, useId, useState } from 'react'
import { AdoptSitesModal } from './AdoptSitesModal'
import { discoveredDomains, ignoreDomains, type Discovered } from './api'
import { domainSuggestionsEnabled, newDiscoveredDomains, setDomainSuggestionsEnabled } from './domainDiscovery'
import { useI18n } from './i18n'
import { IconSites } from './icons'

const REFRESH_MS = 120_000
const PREVIEW_LIMIT = 3

export function HomeDomainSuggestions({ username, onAdded, onOpenCheck }: {
  username: string
  onAdded: () => void
  onOpenCheck: (id: number) => void
}) {
  const { t } = useI18n()
  const listId = useId()
  const optionsId = useId()
  const [enabled, setEnabled] = useState(() => domainSuggestionsEnabled(username))
  const [expanded, setExpanded] = useState(false)
  const [optionsOpen, setOptionsOpen] = useState(false)
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
        <section className="home-domain-suggestions" aria-label={t('Новые домены')}>
          <div className="home-domain-strip">
            <div className="home-domain-label">
              <span className="home-domain-icon"><IconSites /></span>
              <strong>{t('Новые домены')}</strong>
              <span className="net-n">{candidates.length}</span>
            </div>
            <div className="home-domain-actions">
              <button type="button" className="ghost small home-domain-icon-btn"
                aria-label={t('Настройки подсказок')} title={t('Настройки подсказок')}
                aria-controls={optionsId} aria-expanded={optionsOpen}
                onClick={() => setOptionsOpen(!optionsOpen)}>⋯</button>
              <button type="button" className={expanded ? 'ghost small home-domain-icon-btn' : 'ghost small home-domain-toggle'}
                aria-label={expanded ? t('Свернуть список') : undefined}
                title={expanded ? t('Свернуть список') : undefined}
                aria-controls={listId} aria-expanded={expanded}
                onClick={() => {
                  setExpanded(!expanded)
                  setOptionsOpen(false)
                }}>
                {!expanded && t('Посмотреть')}
                <svg className="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                  <path d={expanded ? 'm6 6 12 12M18 6 6 18' : 'm6 9 6 6 6-6'} strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            </div>
          </div>
          <div id={optionsId} hidden={!optionsOpen} className="home-domain-options">
            <button type="button" className="ghost small"
              title={t('Вернуть подсказки можно в разделе «Сайты»')}
              onClick={() => {
                setDomainSuggestionsEnabled(username, false)
                setEnabled(false)
              }}>
              {t('Не показывать на главной')}
            </button>
          </div>
          <div id={listId} hidden={!expanded}>
            <ul>
              {candidates.slice(0, PREVIEW_LIMIT).map(({ domain }) => (
                <li key={domain}>
                  <span className="mono">{domain}</span>
                  <div className="home-domain-actions">
                    <button type="button" className="ghost small" onClick={() => setWizard(domain)}>{t('Добавить')}</button>
                    <button type="button" className="ghost small home-domain-icon-btn" disabled={!!hiding}
                      aria-label={t('Не мониторить')}
                      title={t('Убрать домен из предложений для всех пользователей панели')}
                      onClick={() => void hide(domain)}>
                      {hiding === domain ? '…' : (
                        <svg className="sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
                          <path d="m3 3 18 18M10.6 10.6a2 2 0 0 0 2.8 2.8M9 5.5a11 11 0 0 1 3-.5c5.5 0 9 5 10 7a15 15 0 0 1-3 3.7M6.3 6.3A16 16 0 0 0 2 12c1 2 4.5 7 10 7a10.2 10.2 0 0 0 4.8-1.2" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      )}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
            {failed && <p className="form-error" role="status">{t('Не удалось скрыть домен. Повторите попытку.')}</p>}
            {candidates.length > PREVIEW_LIMIT && (
              <div className="home-domain-footer">
                <button type="button" className="ghost small" onClick={() => setWizard('')}>
                  {t('Все домены ({n})', { n: candidates.length })} <span aria-hidden>→</span>
                </button>
              </div>
            )}
          </div>
        </section>
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
