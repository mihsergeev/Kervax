// Группа "Требует действий": одна проблема на нескольких серверах одной строкой, по клику - строки
// серверов. Заглушка до нового общая для всех групп: галки у строк, "выбрать все", "заглушить
// выбранные (N)", 🔕 у строки по наведению, внизу "заглушено N, показать" с "вернуть". Ключ заглушки
// пункта (mute) дает бэкенд: osupd:<вид>@ у ОС, home:<группа>@ у остальных (app/home_mute.py), тот же,
// что видит карточка сервера.
import { useState, type ReactNode } from 'react'
import { CountryFlag } from './CountryFlag'
import { grpLabel, grpMuteNote, grpMuteTip } from './homeGroups'
import { useI18n } from './i18n'
import { shortList } from './serverUtils'

export type ActGroupItem = {
  key: string
  icon: string
  name?: string
  cc?: string
  detail?: string // что на этом сервере, без имени и без самой проблемы
  tip?: string // всплывашка строки
  mute?: string // ключ заглушки до нового; нет - пункт не глушится
  muted?: boolean
}

// 🔕 у строки: по наведению (CSS), глушит один пункт
export function QuickMute({ tip, busy, onMute }: { tip: string; busy: boolean; onMute: () => void }) {
  return (
    <span className="quick-mute" title={tip} role="button"
      onClick={(e) => {
        e.stopPropagation()
        if (!busy) onMute()
      }}>
      🔕
    </span>
  )
}

export function ActGroup<I extends ActGroupItem>({
  grp,
  items,
  muted,
  open,
  canMute,
  onToggle,
  onOpen,
  onMute,
}: {
  grp: string
  items: I[] // живые, не заглушенные
  muted: I[]
  open: boolean
  canMute: boolean
  onToggle: () => void
  onOpen: (it: I) => void
  onMute: (list: I[], on: boolean) => Promise<unknown>
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
  // строкой над ними, в строках серверов только имя. Свой текст - в строке, как было
  const row = (it: I, low = false, bare = false) => {
    const go = () => onOpen(it)
    // галка нужна, когда есть из чего выбирать; одну строку глушит кнопка над ней или 🔕
    const box = mutable.length > 1 && mutable.includes(it)
    return (
      <div key={it.key} role="button" tabIndex={0} title={it.tip}
        className={`home-attention-row home-attention-subrow${low ? ' home-attention-muted' : ''}`}
        onClick={go} onKeyDown={(e) => e.key === 'Enter' && e.target === e.currentTarget && go()}>
        {box && (
          <input type="checkbox" className="home-sel-box" checked={sel.has(it.key)} aria-label={it.name}
            onClick={(e) => e.stopPropagation()} onChange={(e) => pick([it.key], e.target.checked)} />
        )}
        <span className="home-attention-txt">
          <CountryFlag code={it.cc} /> <b>{it.name}</b>
          {it.detail && !bare ? <span className="muted"> - {it.detail}</span> : null}
        </span>
        {low
          ? canMute && (
              <button className="linklike small" disabled={busy}
                onClick={(e) => {
                  e.stopPropagation()
                  run([it], false)
                }}>
                {t('вернуть')}
              </button>
            )
          : mutable.includes(it) && <QuickMute tip={tip} busy={busy} onMute={() => run([it], true)} />}
        <span className="home-open">→</span>
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
        <div key={`d-${low ? 'm' : 'l'}-${xs[0].key}`}>
          {head && <div className="home-grp-common">{d}</div>}
          {xs.map((x) => row(x, low, true))}
        </div>
      )
    })
  }
  const names = shortList(items.map((x) => x.name ?? ''), t, 4)
  const icon = items.some((x) => x.icon === '⛔') ? '⛔' : items[0].icon
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
    <div className={`home-attention-grpbox${open ? ' open' : ''}`}>
      <button className="home-attention-row" onClick={onToggle}>
        <span className="lk-dot lk-dot-warn" />
        <span className="home-attention-ico">{icon}</span>
        <span className="home-attention-txt">
          {grpLabel(grp, t)}
          <span className="muted"> · {t('серверов: {n}', { n: items.length })} ({names})</span>
          {muted.length > 0 && <span className="muted"> · {t('заглушено {n}', { n: muted.length })}</span>}
        </span>
        <span className="home-open">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="home-attention-sub">
          {bar}
          {rows(items, false)}
          {muted.length > 0 && (
            <div className="home-sel-foot">
              <button className="linklike muted small" onClick={() => setMOpen(!mOpen)}>
                {mOpen ? t('скрыть заглушенные') : t('заглушено {n}, показать', { n: muted.length })}
              </button>
              {mOpen && canMute && muted.length > 1 && (
                <button className="linklike muted small" disabled={busy} onClick={() => run(muted, false)}>
                  {t('вернуть все')}
                </button>
              )}
            </div>
          )}
          {mOpen && rows(muted, true)}
        </div>
      )}
    </div>
  )
}
