// Карточка проблемы, которая требует действий на нескольких серверах. Заглушка до нового общая
// для всех групп: галки у строк, "выбрать все", "заглушить выбранные (N)", индивидуальная
// заглушка, внизу "заглушено N, показать" с "вернуть". Ключ заглушки
// пункта (mute) дает бэкенд: osupd:<вид>@ у ОС, home:<группа>@ у остальных (app/home_mute.py), тот же,
// что видит карточка сервера.
import { Fragment, useState, type ReactNode } from 'react'
import { CountryFlag } from './CountryFlag'
import { grpMuteNote, grpMuteTip } from './homeGroups'
import { useI18n } from './i18n'

export type ActGroupItem = {
  key: string
  name?: string
  cc?: string
  detail?: string // что на этом сервере, без имени и без самой проблемы
  tip?: string // всплывашка строки
  mute?: string // ключ заглушки до нового; нет - пункт не глушится
}

export function ActGroup<I extends ActGroupItem>({
  grp,
  items,
  muted,
  canMute,
  onOpen,
  onMute,
  recheckItem,
}: {
  grp: string
  items: I[] // живые, не заглушенные
  muted: I[]
  canMute: boolean
  onOpen: (it: I) => void
  onMute: (list: I[], on: boolean) => Promise<unknown>
  recheckItem?: (it: I) => ReactNode
}) {
  const { t } = useI18n()
  const [sel, setSel] = useState<Set<string>>(() => new Set())
  const [busy, setBusy] = useState(false)
  const [mOpen, setMOpen] = useState(false)
  const tip = grpMuteTip(grp, t)
  const mutable = canMute ? items.filter((x) => x.mute) : []
  const picked = mutable.filter((x) => sel.has(x.key))
  const allOn = mutable.length > 0 && picked.length === mutable.length
  const pick = (keys: string[], on: boolean) =>
    setSel((prev) => {
      const n = new Set(prev)
      for (const k of keys) if (on) n.add(k)
      else n.delete(k)
      return n
    })
  const run = (list: I[], on: boolean) => {
    if (!list.length || busy) return
    setBusy(true)
    onMute(list, on).finally(() => {
      setBusy(false)
      pick(list.map((x) => x.key), false)
    })
  }
  // Одинаковый текст у нескольких строк (6 серверов на Ubuntu 20.04 с одним и тем же сроком) - одной
  // строкой над ними, в строках серверов только имя. Свой текст и подсказки остаются у сервера.
  const row = (it: I, low = false, bare = false) => {
    const canMuteItem = mutable.includes(it)
    const actions = !low ? recheckItem?.(it) : null
    const details = [!bare && it.detail, it.tip && it.tip !== it.detail && it.tip].filter(Boolean).join('\n')
    return (
      <div key={it.key} className={`home-queue-inspector-member${low ? ' home-attention-muted' : ''}`}>
        <div className="home-queue-inspector-member-head">
          {canMuteItem && mutable.length > 1 && (
            <input type="checkbox" className="home-sel-box" checked={sel.has(it.key)} aria-label={it.name}
              onChange={(e) => pick([it.key], e.target.checked)} />
          )}
          <button type="button" className="linklike" onClick={() => onOpen(it)}>
            <CountryFlag code={it.cc} /> {it.name} <span aria-hidden="true">→</span>
          </button>
        </div>
        {details && <div className="home-queue-inspector-body">{details}</div>}
        {(actions || canMuteItem || (low && canMute)) && <div className="home-queue-inspector-actions">
          {actions}
          {canMuteItem && <button type="button" className="ghost small" title={tip} disabled={busy}
            onClick={() => run([it], true)}>{t('Не алертить')}</button>}
          {low && canMute && <button type="button" className="linklike small" disabled={busy}
            onClick={() => run([it], false)}>{t('вернуть')}</button>}
        </div>}
      </div>
    )
  }
  // Строки по тексту: одинаковый у нескольких (живых и заглушенных вместе) - заголовком над первыми
  // из них, у заглушенных под ним текст не повторяется
  const many = new Map<string, number>()
  for (const x of [...items, ...muted]) if (x.detail) many.set(x.detail, (many.get(x.detail) ?? 0) + 1)
  const heads = new Set<string>()
  const rows = (list: I[], low: boolean) => {
    const by = new Map<string, I[]>()
    for (const x of list) by.set(x.detail ?? '', [...(by.get(x.detail ?? '') ?? []), x])
    return [...by].map(([d, xs]) => {
      if (!d || (many.get(d) ?? 0) < 2) return xs.map((x) => row(x, low))
      const head = !heads.has(d)
      heads.add(d)
      return (
        <Fragment key={`d-${low ? 'm' : 'l'}-${xs[0].key}`}>
          {head && <div className="home-grp-common">{d}</div>}
          {xs.map((x) => row(x, low, true))}
        </Fragment>
      )
    })
  }
  let bar: ReactNode = null
  if (mutable.length > 1)
    bar = (
      <div className="home-sel-bar">
        <label className="home-sel-all">
          <input type="checkbox" className="home-sel-box" checked={allOn}
            onChange={() => pick(mutable.map((x) => x.key), !allOn)} />
          {t('выбрать все')}
        </label>
        <button className="ghost small" disabled={!picked.length || busy} title={tip} onClick={() => run(picked, true)}>
          {picked.length ? t('заглушить выбранные ({n})', { n: picked.length }) : t('заглушить выбранные')}
        </button>
        <span className="muted small">{grpMuteNote(grp, t)}</span>
      </div>
    )
  // одна строка: галка и "выбрать все" ни к чему, сразу кнопка
  else if (mutable.length === 1)
    bar = (
      <div className="home-sel-bar">
        <button className="ghost small" disabled={busy} title={tip} onClick={() => run(mutable, true)}>
          {t('заглушить {name}', { name: mutable[0].name ?? '' })}
        </button>
        <span className="muted small">{grpMuteNote(grp, t)}</span>
      </div>
    )
  return (
    <div className="home-queue-inspector-members" role="group" aria-label={t('Серверы группы')}>
      {bar}
      {rows(items, false)}
      {muted.length > 0 && (
        <div className="home-sel-foot">
          <button type="button" className="linklike muted small" aria-expanded={mOpen} onClick={() => setMOpen(!mOpen)}>
            {mOpen ? t('скрыть заглушенные') : t('заглушено {n}, показать', { n: muted.length })}
          </button>
          {mOpen && canMute && muted.length > 1 && (
            <button type="button" className="linklike muted small" disabled={busy} onClick={() => run(muted, false)}>
              {t('вернуть все')}
            </button>
          )}
        </div>
      )}
      {mOpen && rows(muted, true)}
    </div>
  )
}
