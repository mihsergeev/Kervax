import type { MirrorCopy, MirrorReport } from '../src/api'
import { copyTone, verifyText, verifyTone } from '../src/bsrvMirrorState'

const now = Date.now() / 1000
const t = (s: string, p?: Record<string, string | number>) =>
  s.replace(/\{(\w+)\}/g, (_, key: string) => String(p?.[key] ?? ''))
const base = { src: 'primary', repo: 'repo', enabled: true, last_ok: now,
  verify_ts: now, verify_status: 'ok', verify_files: 5, verify_snapshot: 'a'.repeat(64) } as MirrorCopy
const report = { running: false, helper: '0.3' } as MirrorReport
const cases: Record<string, Partial<MirrorCopy>> = {
  ok: {}, failed: { verify_status: 'failed' }, unknown: { verify_status: 'unknown' },
  pending: { verify_status: '', verify_ts: 0 }, password: { verify_status: 'unknown', verify_code: 'password_missing' },
  empty: { verify_files: 0 }, no_snapshot: { verify_snapshot: '' }, stale: { verify_ts: now - 11 * 86400 },
}
console.log(JSON.stringify(Object.fromEntries(Object.entries(cases).map(([key, changes]) => {
  const copy = { ...base, ...changes }
  return [key, { tone: verifyTone(copy), copy: copyTone(copy, report), text: verifyText(copy, t) }]
}))))
