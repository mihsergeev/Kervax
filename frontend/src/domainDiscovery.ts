import type { Discovered } from './api'

// Keep the suggestion filter aligned with _adopt_problem in the checks API.
export function adoptable(domain: string): boolean {
  return domain.includes('.') && !domain.includes('*') && !domain.startsWith('~') && !/[ \t,;\\]/.test(domain)
}

export function newDiscoveredDomains(found: Discovered | null) {
  if (!found) return []
  const ignored = new Set(found.ignored)
  return found.domains.filter(({ domain }) =>
    adoptable(domain) && !Object.hasOwn(found.hosts, domain) && !ignored.has(domain),
  )
}

const preferenceKey = (username: string) => `kervax_home_domain_suggestions:${username}`

export function domainSuggestionsEnabled(username: string) {
  return localStorage.getItem(preferenceKey(username)) !== 'off'
}

export function setDomainSuggestionsEnabled(username: string, enabled: boolean) {
  if (enabled) localStorage.removeItem(preferenceKey(username))
  else localStorage.setItem(preferenceKey(username), 'off')
}
