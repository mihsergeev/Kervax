import type { AllowEntry, LoginGuard } from './api'

// черновик раздела "Защита входа" и слова для срока блокировки (LoginGuardSec, UsersModal)

type T = (s: string, p?: Record<string, string | number>) => string

export type GuardDraft = { max_failures: number; ban_minutes: number; allow: AllowEntry[] }

export const guardDraftOf = (g: LoginGuard): GuardDraft => ({
  max_failures: g.max_failures,
  ban_minutes: g.ban_minutes,
  allow: g.allow.map((a) => ({ net: a.net, note: a.note ?? '' })),
})
export const sameGuard = (a: GuardDraft, b: GuardDraft) => JSON.stringify(a) === JSON.stringify(b)

const BAN_WORDS: Record<number, string> = { 15: 'на 15 минут', 60: 'на час', 360: 'на 6 часов', 1440: 'на сутки' }
export const banWords = (m: number, t: T) => (BAN_WORDS[m] ? t(BAN_WORDS[m]) : t('на {n} мин', { n: m }))

