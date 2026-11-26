// Карточка проблемы, которая требует действий на нескольких серверах. Заглушка до нового общая
// для всех групп: галки у строк, "выбрать все", "заглушить выбранные (N)", индивидуальная
// заглушка, внизу "заглушено N, показать" с "вернуть". Ключ заглушки
// пункта (mute) дает бэкенд: osupd:<вид>@ у ОС, home:<группа>@ у остальных (app/home_mute.py), тот же,
// что видит карточка сервера.
import { useId, useState, type ReactNode } from 'react'
import { HomeIssueMember } from './HomeIssueMember'
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
  const mutedId = useId()
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
  const row = (it: I, low = false) => {
    const canMuteItem = !low && canMute && !!it.mute
    const recheck = low ? null : recheckItem?.(it)
    const actions = (recheck || canMuteItem || (low && canMute)) && <>
      {recheck}
      {canMuteItem && <button type="button" className="ghost small" title={tip} disabled={busy}
        onClick={() => run([it], true)}>{t('Не алертить')}</button>}
      {low && canMute && <button type="button" className="ghost small" disabled={busy}
        onClick={() => run([it], false)}>{t('вернуть')}</button>}
    </>
    return <HomeIssueMember key={it.key} name={it.name} country={it.cc} detail={it.detail}
      extra={it.tip !== it.detail ? it.tip : undefined} muted={low} actions={actions} onOpen={() => onOpen(it)}
      select={canMuteItem && mutable.length > 1 && (
        <input type="checkbox" className="home-sel-box" checked={sel.has(it.key)} aria-label={it.name || t('Выбрать')}
          onChange={(e) => pick([it.key], e.target.checked)} />
      )} />
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
    <div className="home-queue-inspector-group" role="group" aria-label={t('Серверы группы')}>
      {bar}
      <ul className="home-queue-inspector-members" role="list">{items.map((it) => row(it))}</ul>
      {muted.length > 0 && (
        <>
          <div className="home-sel-foot">
            <button type="button" className="linklike muted small" aria-expanded={mOpen} aria-controls={mutedId}
              onClick={() => setMOpen((open) => !open)}>
              {mOpen ? t('скрыть заглушенные') : t('заглушено {n}, показать', { n: muted.length })}
            </button>
            {mOpen && canMute && muted.length > 1 && (
              <button type="button" className="linklike muted small" disabled={busy} onClick={() => run(muted, false)}>
                {t('вернуть все')}
              </button>
            )}
          </div>
          <ul id={mutedId} className="home-queue-inspector-members" role="list" hidden={!mOpen}>
            {mOpen && muted.map((it) => row(it, true))}
          </ul>
        </>
      )}
    </div>
  )
}
