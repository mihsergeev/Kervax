import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { backupCommand, backupCommandStatus, type Server } from './api'
import { useAuth } from './auth'
import { CountryFlag } from './CountryFlag'
import { useI18n } from './i18n'

// Журнал systemd по запросу (агент 2.29, helper units-setup 0.4): юнит, вся система или ядро,
// за период, с приоритетом не ниже выбранного. Как логи контейнеров: в панели не хранится,
// читается с ноды по кнопке; фильтр по тексту - уже в браузере.

const JOURNAL_AGENT = '2.29'
const JOURNAL_HELPER = '0.4'
// то же правило, что у бэкенда (schemas._UNIT_NAME) и агента: остальное не отправляем
const UNIT_RE = /^[A-Za-z0-9@._:\\][A-Za-z0-9@._:\\-]*\.(service|socket|timer|mount|automount|swap|path)$/
const PERIODS = [15, 60, 360, 1440, 10080]

type Prio = 'info' | 'warning' | 'err'
type Params = { target: string; since: number; prio: Prio; lines: number; n: number }

function verAtLeast(a: string | undefined | null, b: string): boolean {
  if (!a) return false
  const pa = a.split('.').map((x) => parseInt(x, 10) || 0)
  const pb = b.split('.').map((x) => parseInt(x, 10) || 0)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] ?? 0) !== (pb[i] ?? 0)) return (pa[i] ?? 0) > (pb[i] ?? 0)
  }
  return true
}

// можно ли читать журнал: null - можно, иначе что обновить
export function journalBlocker(s: Server): 'helper' | 'agent' | null {
  const r = s.last_report
  const hv = r?.setup_versions?.['units-setup']
  if (!hv || !verAtLeast(String(hv), JOURNAL_HELPER)) return 'helper'
  return verAtLeast(r?.agent_version, JOURNAL_AGENT) ? null : 'agent'
}

// "nginx" -> "nginx.service": так пишут чаще всего
function normUnit(v: string): string {
  const u = v.trim()
  if (!u) return ''
  if (UNIT_RE.test(u)) return u
  return UNIT_RE.test(`${u}.service`) ? `${u}.service` : ''
}

// юниты, которые нода сама называла: включенные (снимок changes-setup), упавшие, крутящиеся
function knownUnits(s: Server): string[] {
  const ex = s.last_report?.extras as Record<string, unknown> | undefined
  const ch = ex?.changes as { units?: unknown } | undefined
  const units = ex?.units as { units?: { unit?: string }[] } | undefined
  const out = new Set<string>()
  for (const u of Array.isArray(ch?.units) ? ch!.units : []) if (typeof u === 'string') out.add(u)
  for (const u of units?.units ?? []) if (u.unit) out.add(u.unit)
  for (const p of s.problems ?? []) if (p.kind === 'unit_loop' && p.unit) out.add(p.unit)
  return [...out].filter((u) => UNIT_RE.test(u)).sort()
}

// строка с ошибкой - красным, предупреждение - желтым: глаз сразу находит, с чего началось
const ERR_RE = /\b(error|failed|failure|fatal|panic|critical|segfault|oom|killed|denied)\b/i
const WARN_RE = /\b(warn|warning|deprecated|timeout|timed out|retry)\b/i

export function JournalModal({ server: s, unit: unit0, onClose }: { server: Server; unit: string; onClose: () => void }) {
  const { t } = useI18n()
  const known = useMemo(() => knownUnits(s), [s])
  const first = unit0 === '_all' || unit0 === '_kernel' ? unit0 : normUnit(unit0)
  const [draft, setDraft] = useState(first.startsWith('_') ? '' : first)
  const [unitMode, setUnitMode] = useState(!first.startsWith('_'))
  const [p, setP] = useState<Params>({ target: first, since: 60, prio: 'info', lines: 300, n: 0 })
  const [text, setText] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const seq = useRef(0)

  useEffect(() => {
    if (!p.target) return
    const my = ++seq.current
    setLoading(true)
    setErr('')
    void (async () => {
      try {
        const c = await backupCommand(s.id, {
          action: 'unit_journal',
          unit: p.target,
          since_min: p.since,
          lines: p.lines,
          prio: p.prio,
        })
        let last = c
        const t0 = Date.now()
        while (last.status !== 'done' && last.status !== 'error' && Date.now() - t0 < 2 * 60_000) {
          await new Promise((res) => setTimeout(res, 1500))
          if (my !== seq.current) return
          last = await backupCommandStatus(s.id, c.id)
        }
        if (my !== seq.current) return
        if (last.status === 'done' && last.ok) setText(last.result ?? '')
        else setErr(last.result || t('не удалось получить журнал'))
      } catch (e) {
        if (my === seq.current) setErr(e instanceof Error ? e.message : String(e))
      } finally {
        if (my === seq.current) setLoading(false)
      }
    })()
  }, [p, s.id, t])

  const set = (patch: Partial<Params>) => setP((cur) => ({ ...cur, ...patch, n: cur.n + 1 }))
  const showUnit = () => {
    const u = normUnit(draft)
    if (!u) {
      setErr(t('Имя юнита вида nginx.service или nginx'))
      return
    }
    setDraft(u)
    set({ target: u })
  }
  const all = useMemo(() => {
    const body = (text ?? '').replace(/\n+$/, '')
    return body === '' || body.trim() === '-- No entries --' ? [] : body.split('\n')
  }, [text])
  const ql = q.trim().toLowerCase()
  const shown = ql ? all.filter((l) => l.toLowerCase().includes(ql)) : all
  const download = () => {
    const blob = new Blob([text ?? ''], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')
    a.href = url
    a.download = `${s.name}_${p.target.replace(/^_/, '')}_${stamp}.txt`
    a.click()
    URL.revokeObjectURL(url)
  }
  const periodLabel = (m: number) =>
    m < 60 ? t('{n} мин', { n: m }) : m < 1440 ? t('{n} ч', { n: m / 60 }) : t('{n} дн', { n: m / 1440 })
  const prios: { k: Prio; l: string }[] = [
    { k: 'info', l: t('все') },
    { k: 'warning', l: t('предупреждения') },
    { k: 'err', l: t('ошибки') },
  ]
  const title = p.target === '_all' ? t('Весь журнал') : p.target === '_kernel' ? t('Ядро') : p.target || t('Журнал')
  // ровно столько строк, сколько просили: за период их, скорее всего, больше. Или агент
  // обрезал ответ по размеру (90 000 байт, агент 2.29): строк меньше, чем просили
  const cut = all.length >= p.lines
  const bySize = !cut && (text?.length ?? 0) >= 80_000
  return createPortal(
    <div className="modal-backdrop">
      <div className="card modal logs-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3 className="mono">
            {title} <span className="muted small">· <CountryFlag code={s.country} /> {s.name}</span>
          </h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <div className="logs-toolbar">
          <div className="win-switch">
            <button
              className={`win-btn${unitMode ? ' win-btn-active' : ''}`}
              disabled={loading}
              onClick={() => {
                setUnitMode(true)
                if (normUnit(draft)) set({ target: normUnit(draft) })
              }}
            >
              {t('юнит')}
            </button>
            {(['_all', '_kernel'] as const).map((k) => (
              <button
                key={k}
                className={`win-btn${!unitMode && p.target === k ? ' win-btn-active' : ''}`}
                disabled={loading}
                onClick={() => {
                  setUnitMode(false)
                  set({ target: k })
                }}
              >
                {k === '_all' ? t('весь журнал') : t('ядро')}
              </button>
            ))}
          </div>
          {unitMode && (
            <form
              className="jr-unit"
              onSubmit={(e) => {
                e.preventDefault()
                showUnit()
              }}
            >
              <input
                className="mono"
                list={`jr-units-${s.id}`}
                value={draft}
                placeholder="nginx.service"
                autoFocus={!first}
                onChange={(e) => setDraft(e.target.value)}
              />
              <datalist id={`jr-units-${s.id}`}>
                {known.map((u) => (
                  <option key={u} value={u} />
                ))}
              </datalist>
              <button className="ghost" disabled={loading || !draft.trim()}>
                {t('Показать')}
              </button>
            </form>
          )}
        </div>
        <div className="logs-toolbar">
          <div className="win-switch">
            {PERIODS.map((m) => (
              <button
                key={m}
                className={`win-btn${p.since === m ? ' win-btn-active' : ''}`}
                disabled={loading}
                onClick={() => set({ since: m })}
              >
                {periodLabel(m)}
              </button>
            ))}
          </div>
          <div className="win-switch">
            {prios.map(({ k, l }) => (
              <button
                key={k}
                className={`win-btn${p.prio === k ? ' win-btn-active' : ''}`}
                disabled={loading}
                onClick={() => set({ prio: k })}
              >
                {l}
              </button>
            ))}
          </div>
          <div className="win-switch">
            {[300, 1000].map((n) => (
              <button
                key={n}
                className={`win-btn${p.lines === n ? ' win-btn-active' : ''}`}
                disabled={loading}
                onClick={() => set({ lines: n })}
              >
                {t('{n} строк', { n })}
              </button>
            ))}
          </div>
          <div className="spacer" />
          <button className="ghost" disabled={loading || !p.target} onClick={() => set({})}>
            {loading ? t('…') : t('Обновить')}
          </button>
          <button className="ghost" disabled={!all.length} onClick={download}>
            {t('Скачать .txt')}
          </button>
        </div>
        {all.length > 0 && (
          <div className="logs-toolbar">
            <input className="jr-filter" value={q} placeholder={t('фильтр по тексту')} onChange={(e) => setQ(e.target.value)} />
            <span className="muted small">
              {ql ? t('строк: {k} из {n}', { k: shown.length, n: all.length }) : t('строк: {n}', { n: all.length })}
            </span>
          </div>
        )}
        {err && <p className="form-error">{err}</p>}
        {!p.target ? (
          <div className="logs-empty muted">{t('Выберите юнит или весь журнал.')}</div>
        ) : loading && text == null ? (
          <div className="muted small">{t('Запрашиваю журнал у агента…')}</div>
        ) : text == null ? null : all.length === 0 ? (
          <div className="logs-empty muted">
            {t('Нет записей за выбранный период. Увеличьте период или выберите "все" вместо ошибок.')}
          </div>
        ) : shown.length === 0 ? (
          <div className="logs-empty muted">{t('Под фильтр ничего не попало.')}</div>
        ) : (
          <pre className={`logs-pre${loading ? ' jr-stale' : ''}`}>
            {shown.map((l, i) => (
              <span key={i} className={ERR_RE.test(l) ? 'jr-err' : WARN_RE.test(l) ? 'jr-warn' : undefined}>
                {l}
                {'\n'}
              </span>
            ))}
          </pre>
        )}
        <div className="muted small">
          {cut
            ? t('Показаны последние {n} строк: за период их больше, сократите период или поднимите приоритет.', { n: p.lines })
            : bySize
              ? t('Показаны последние {n} строк: больше за один раз агент не передает, сократите период или поднимите приоритет.', { n: all.length })
              : t('Журнал не хранится в панели - читается с ноды по запросу.')}
        </div>
      </div>
    </div>,
    document.body,
  )
}

// Карточка в разделе "Процессы": открыть журнал без SSH. Наблюдателю не показываем: чтение идет
// через команду агенту, а изменяющие запросы ему закрыты.
export function JournalCard({ server: s }: { server: Server }) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [open, setOpen] = useState<string | null>(null)
  if (isViewer) return null
  const block = journalBlocker(s)
  return (
    <div className="detail-inc" id="mcard-journal">
      <div className="chart-cap">{t('Журнал systemd')}</div>
      {block ? (
        <div className="muted small">
          {block === 'agent'
            ? t('Журнал откроется после обновления агента до 2.29.')
            : t('Журнал читается через helper units-setup 0.4: обновите его (блок "Обновления" на главной).')}
        </div>
      ) : (
        <div className="unit-actions small">
          <button className="ghost small row-act" onClick={() => setOpen('_all')}>
            {t('Весь журнал')}
          </button>
          <button className="ghost small row-act" onClick={() => setOpen('_kernel')}>
            {t('Ядро')}
          </button>
          <button className="ghost small row-act" onClick={() => setOpen('')}>
            {t('Юнит...')}
          </button>
        </div>
      )}
      {open != null && <JournalModal server={s} unit={open} onClose={() => setOpen(null)} />}
    </div>
  )
}
