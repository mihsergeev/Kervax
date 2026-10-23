import { useState } from 'react'
import { expirySnooze, type Check, type ExpirySnooze } from './api'
import { useI18n } from './i18n'

// Отложенные сроки сайта (backend/app/expiry_snooze.py): "напомнить за 7 дней", "за 1 день",
// "не следить". Пока до конца больше выбранного, пункт не на главной и алерт молчит; на пороге
// отложки возвращается. Продлили - отложка снимается сама, "не следить" остается.

export type ExpKind = 'ssl' | 'domain'
type Mode = 7 | 1 | 0
type T = (s: string, v?: Record<string, string | number>) => string

// срок плавает на день между пробами, продление отодвигает его на месяцы (RENEW_SLACK бэкенда)
const RENEW_SLACK = 2

export function expEntry(c: Check, kind: ExpKind): ExpirySnooze | null {
  return c.expiry_snooze?.[kind] ?? null
}

const daysOf = (c: Check, kind: ExpKind) => (kind === 'ssl' ? c.ssl_days : c.domain_days)

// Пункт сейчас скрыт: не следим, или отложен и до конца больше выбранного (hidden бэкенда)
export function expHidden(c: Check, kind: ExpKind): boolean {
  const e = expEntry(c, kind)
  if (!e) return false
  if (e.mode === 0) return true
  const d = daysOf(c, kind)
  if (d == null) return false
  if (e.days != null && d > e.days + RENEW_SLACK) return false
  return d > e.mode
}

export function expSnoozeText(e: ExpirySnooze, t: T): string {
  return e.mode === 0 ? t('не следим') : t('напомню за {n} дн.', { n: e.mode })
}

export function expSnoozeTip(e: ExpirySnooze, t: T): string {
  const who = { who: e.by || '?', when: e.at ? new Date(e.at).toLocaleString() : '?' }
  return e.mode === 0
    ? t('не следить отметил {who}, {when}', who)
    : t('отложил {who}, {when}. Продлят - отложка снимется сама', who)
}

// что можно выбрать сейчас: "за 7" - пока до конца больше 7 дней, "за 1" - больше 1
export function expModes(days: number | null): Mode[] {
  const out: Mode[] = []
  if (days != null && days > 7) out.push(7)
  if (days != null && days > 1) out.push(1)
  out.push(0)
  return out
}

const modeLabel = (m: Mode, kind: ExpKind, t: T, short: boolean) =>
  m === 7
    ? short ? t('за 7 дн.') : t('напомнить за 7 дней')
    : m === 1
      ? short ? t('за 1 день') : t('напомнить за 1 день')
      : short ? t('не следить') : kind === 'domain' ? t('не следить за доменом') : t('не следить за сертификатом')

const modeTip = (m: Mode, t: T) =>
  m === 0
    ? t('Пункт уйдет с главной и из алертов навсегда. Вернуть - строкой "не следим" внизу блока или в карточке')
    : t('Пункт уйдет с главной и из алертов, пока до конца больше {n} дн. Продлят - отложка снимется сама', { n: m })

// Выбор в строке: на главной - компактно, рядом с 🔕
export function ExpirySnoozePick({
  check, kind, short = false, onDone, onClose,
}: {
  check: Check
  kind: ExpKind
  short?: boolean
  onDone: () => void
  onClose?: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const pick = (m: Mode) => {
    setBusy(true)
    setErr('')
    expirySnooze(check.id, kind, m)
      .then(() => { onClose?.(); onDone() })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }
  return (
    <span className="home-mute-pick exp-snooze-pick" onClick={(e) => e.stopPropagation()}>
      {short && <span className="muted">{t('напомнить:')}</span>}
      {expModes(daysOf(check, kind)).map((m) => (
        <span key={m} className={`home-mute-opt${busy ? ' muted' : ''}`} role="button" title={modeTip(m, t)}
          onClick={() => !busy && pick(m)}>
          {modeLabel(m, kind, t, short)}
        </span>
      ))}
      {onClose && <span className="home-mute-opt muted" role="button" onClick={onClose}>✕</span>}
      {err && <span className="form-error small">{err}</span>}
    </span>
  )
}

// Состояние отложки: "🔕 напомню за 1 дн." с подсказкой кто и когда и "вернуть"
export function ExpirySnoozeState({
  check, kind, canEdit, onDone,
}: {
  check: Check
  kind: ExpKind
  canEdit: boolean
  onDone: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const e = expEntry(check, kind)
  if (!e) return null
  const back = () => {
    setBusy(true)
    expirySnooze(check.id, kind, null).then(onDone).catch(() => {}).finally(() => setBusy(false))
  }
  return (
    <span className="exp-snooze-state" onClick={(ev) => ev.stopPropagation()}>
      <span className="type-chip muted" title={expSnoozeTip(e, t)}>🔕 {expSnoozeText(e, t)}</span>
      {canEdit && (
        <button className="linklike small" disabled={busy} onClick={back}
          title={t('Пункт вернется на главную и в алерты по обычным порогам')}>
          {e.mode === 0 ? t('следить снова') : t('вернуть')}
        </button>
      )}
    </span>
  )
}
