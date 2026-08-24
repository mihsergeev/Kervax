import { useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { ApiError, adoptDomains, ignoreDomains } from './api'
import { useI18n } from './i18n'

// Мастер «Поставить на мониторинг»: список доменов, найденных агентами на веб-серверах
// (nginx/Apache/Caddy, Ingress, Gateway API), с отметкой галочками и созданием мониторов.
//
// Два входа, один компонент: из «Сервисов» приходит список одного веб-сервиса, из
// «Сайтов» — весь парк. Разница только во входных данных, поведение одинаковое.

export type AdoptItem = { domain: string; servers?: string[] }

// Годится ли имя в монитор. Та же логика, что на бэкенде (_adopt_problem в
// backend/app/api/checks.py): маску и regexp проверять нечем — HTTP-монитору нужен
// конкретный адрес.
export function adoptable(d: string): boolean {
  return !!d && !d.includes('*') && !d.startsWith('~') && d.includes('.')
}

// за раз столько же, сколько принимает POST /api/checks/adopt
const ADOPT_MAX = 500

// Домены второго уровня, за которыми регистрируют третий: для них «зона» — три метки,
// иначе shop.msk.ru и blog.msk.ru слиплись бы в одну кучу «msk.ru». Полный PSL сюда
// тащить не за чем — список нужен только для ГРУППИРОВКИ в списке, ошибка не критична.
const SLD = new Set([
  'co', 'com', 'net', 'org', 'edu', 'gov', 'mil', 'ac', 'or', 'ne', 'go',
  'msk', 'spb', 'nnov', 'sochi', 'pp', 'in', 'biz', 'info',
])

// «Зона» домена — то, по чему группируем список (обычно регистрируемое имя).
export function zoneOf(domain: string): string {
  const parts = domain.replace(/^\*\./, '').split('.').filter(Boolean)
  if (parts.length <= 2) return parts.join('.')
  const take = SLD.has(parts[parts.length - 2]) ? 3 : 2
  return parts.slice(-take).join('.')
}

// new — можно завести; monitored — уже есть монитор; bad — маска/regexp;
// skipped — человек сказал «мониторить не нужно» (общий список на панель)
type Status = 'new' | 'monitored' | 'bad' | 'skipped'

type Row = { domain: string; servers: string[]; status: Status; checkId: number }

export function AdoptSitesModal({
  title,
  items,
  hosts,
  groups,
  ignored,
  onClose,
  onDone,
  onIgnored,
  onOpenCheck,
}: {
  title: string
  items: AdoptItem[]
  hosts: Record<string, number>
  groups: string[]
  ignored?: string[] // домены, помеченные «не нужен» — прячем из предложений
  onClose: () => void
  onDone: (hosts: Record<string, number>, created: number) => void
  onIgnored?: (domains: string[], ignore: boolean) => void
  onOpenCheck?: (id: number) => void
}) {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const [group, setGroup] = useState('')
  const [onlyNew, setOnlyNew] = useState(true)
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [done, setDone] = useState('')
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  // локальная копия «ненужных»: правим её сразу, не дожидаясь перезагрузки списка
  const [skip, setSkip] = useState<Set<string>>(() => new Set(ignored || []))
  const [showSkipped, setShowSkipped] = useState(false)

  // строки со статусом: считаем один раз на смену входных данных/карты мониторов
  const rows: Row[] = useMemo(() => {
    const seen = new Map<string, Row>()
    for (const it of items) {
      const domain = it.domain.trim().toLowerCase()
      if (!domain) continue
      const id = hosts[domain]
      const row = seen.get(domain)
      if (row) {
        // один домен может висеть на нескольких нодах — склеиваем источники
        for (const s of it.servers || []) if (!row.servers.includes(s)) row.servers.push(s)
        continue
      }
      seen.set(domain, {
        domain,
        servers: [...(it.servers || [])],
        status:
          id != null
            ? 'monitored'
            : skip.has(domain)
              ? 'skipped'
              : adoptable(domain)
                ? 'new'
                : 'bad',
        checkId: id ?? 0,
      })
    }
    return [...seen.values()].sort((a, b) => a.domain.localeCompare(b.domain))
  }, [items, hosts, skip])

  const ql = q.trim().toLowerCase()
  const visible = rows.filter(
    (r) =>
      // «не нужные» не мешаются под ногами, пока их явно не попросят показать
      (r.status !== 'skipped' || showSkipped) &&
      (!onlyNew || r.status === 'new' || (showSkipped && r.status === 'skipped')) &&
      (!ql || r.domain.includes(ql) || r.servers.some((s) => s.toLowerCase().includes(ql))),
  )

  // зоны в порядке «где больше доступного к добавлению» — самое полезное сверху
  const zones = useMemo(() => {
    const by = new Map<string, Row[]>()
    for (const r of visible) {
      const z = zoneOf(r.domain)
      const list = by.get(z)
      if (list) list.push(r)
      else by.set(z, [r])
    }
    return [...by.entries()]
      .map(([zone, list]) => ({ zone, list, fresh: list.filter((r) => r.status === 'new') }))
      .sort((a, b) => b.fresh.length - a.fresh.length || a.zone.localeCompare(b.zone))
  }, [visible])

  const freshVisible = visible.filter((r) => r.status === 'new')
  const picked = freshVisible.filter((r) => sel.has(r.domain))
  const total = rows.length
  const newTotal = rows.filter((r) => r.status === 'new').length
  const skipTotal = rows.filter((r) => r.status === 'skipped').length

  const toggle = (domain: string) => {
    setSel((prev) => {
      const next = new Set(prev)
      if (next.has(domain)) next.delete(domain)
      else next.add(domain)
      return next
    })
  }
  const setMany = (list: Row[], on: boolean) => {
    setSel((prev) => {
      const next = new Set(prev)
      for (const r of list) {
        if (on) next.add(r.domain)
        else next.delete(r.domain)
      }
      return next
    })
  }
  const toggleZone = (zone: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(zone)) next.delete(zone)
      else next.add(zone)
      return next
    })
  }

  // «мониторить не нужно» / «вернуть в предложения»
  const mark = async (domains: string[], ignore: boolean) => {
    if (domains.length === 0) return
    setErr('')
    try {
      await ignoreDomains(domains, ignore)
      setSkip((prev) => {
        const next = new Set(prev)
        for (const d of domains) {
          if (ignore) next.add(d)
          else next.delete(d)
        }
        return next
      })
      setSel((prev) => {
        const next = new Set(prev)
        for (const d of domains) next.delete(d)
        return next
      })
      onIgnored?.(domains, ignore)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    }
  }

  const submit = async () => {
    setBusy(true)
    setErr('')
    setDone('')
    try {
      const chunk = picked.slice(0, ADOPT_MAX).map((r) => r.domain)
      const res = await adoptDomains(chunk, group.trim())
      setSel((prev) => {
        const next = new Set(prev)
        for (const d of chunk) next.delete(d)
        return next
      })
      setDone(t('создано мониторов: {n}', { n: res.created }))
      onDone(res.hosts, res.created)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal adopt-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{title}</h3>
          <button className="ghost" onClick={onClose}>{t('Закрыть')}</button>
        </div>

        <div className="adopt-sub muted small">
          {t('найдено доменов: {n}', { n: total })}
          {' · '}
          {newTotal > 0
            ? t('вне мониторинга: {n}', { n: newTotal })
            : t('все уже под мониторингом')}
          {skipTotal > 0 && (
            <>
              {' · '}
              <button className="linklike" onClick={() => setShowSkipped(!showSkipped)}>
                {showSkipped
                  ? t('скрыть ненужные ({n})', { n: skipTotal })
                  : t('ненужных: {n}', { n: skipTotal })}
              </button>
            </>
          )}
        </div>

        <div className="adopt-tools">
          <input
            className="adopt-search mono"
            placeholder={t('фильтр по домену или ноде…')}
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <label className="adopt-chk">
            <input
              type="checkbox"
              checked={onlyNew}
              onChange={(e) => setOnlyNew(e.target.checked)}
            />
            {t('только новые')}
          </label>
          <button
            className="ghost"
            disabled={freshVisible.length === 0}
            onClick={() => setMany(freshVisible, true)}
          >
            {t('выбрать все ({n})', { n: freshVisible.length })}
          </button>
          <button className="ghost" disabled={sel.size === 0} onClick={() => setSel(new Set())}>
            {t('снять выбор')}
          </button>
          {picked.length > 0 && (
            <button
              className="ghost"
              onClick={() => mark(picked.map((r) => r.domain), true)}
            >
              {t('не нужны ({n})', { n: picked.length })}
            </button>
          )}
        </div>

        <div className="adopt-list">
          {zones.length === 0 ? (
            <div className="muted small">
              {onlyNew && newTotal === 0
                ? t('Все найденные домены уже стоят на мониторинге.')
                : t('ничего не найдено')}
            </div>
          ) : (
            zones.map(({ zone, list, fresh }) => {
              const shut = collapsed.has(zone)
              const on = fresh.length > 0 && fresh.every((r) => sel.has(r.domain))
              return (
                <div key={zone} className="adopt-zone">
                  <div className="adopt-zone-head">
                    <input
                      type="checkbox"
                      checked={on}
                      disabled={fresh.length === 0}
                      // частичный выбор внутри зоны — «квадратик», а не галка
                      ref={(el) => {
                        if (el) el.indeterminate = !on && fresh.some((r) => sel.has(r.domain))
                      }}
                      onChange={(e) => setMany(fresh, e.target.checked)}
                    />
                    <button className="adopt-zone-name" onClick={() => toggleZone(zone)}>
                      <span className="adopt-caret">{shut ? '▸' : '▾'}</span>
                      <span className="mono">{zone}</span>
                    </button>
                    <span className="muted small">
                      {fresh.length > 0
                        ? t('новых: {n} из {m}', { n: fresh.length, m: list.length })
                        : t('всё покрыто ({m})', { m: list.length })}
                    </span>
                  </div>
                  {!shut && (
                    <div className="adopt-rows">
                      {list.map((r) => (
                        <div key={r.domain} className={`adopt-row adopt-${r.status}`}>
                          {r.status === 'new' ? (
                            <input
                              type="checkbox"
                              checked={sel.has(r.domain)}
                              onChange={() => toggle(r.domain)}
                            />
                          ) : r.status === 'monitored' ? (
                            r.checkId > 0 && onOpenCheck ? (
                              <button
                                className="adopt-mark up"
                                title={t('Уже в мониторинге — открыть монитор')}
                                onClick={() => onOpenCheck(r.checkId)}
                              >
                                ✓
                              </button>
                            ) : (
                              <span className="adopt-mark up" title={t('Уже в мониторинге')}>✓</span>
                            )
                          ) : (
                            <span
                              className="adopt-mark"
                              title={t('Маска или regexp — монитору нужен конкретный адрес')}
                            >
                              —
                            </span>
                          )}
                          <span className="adopt-dom mono">{r.domain}</span>
                          {r.servers.length > 0 && (
                            <span className="adopt-where muted small">{r.servers.join(', ')}</span>
                          )}
                          {r.status === 'new' && (
                            <button
                              className="adopt-skip"
                              title={t('Мониторить не нужно — убрать из предложений')}
                              onClick={() => mark([r.domain], true)}
                            >
                              ✕
                            </button>
                          )}
                          {r.status === 'skipped' && (
                            <button
                              className="adopt-skip back"
                              title={t('Вернуть в предложения')}
                              onClick={() => mark([r.domain], false)}
                            >
                              ↺
                            </button>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })
          )}
        </div>

        {err && <div className="form-error small">{err}</div>}
        {done && <div className="adopt-done small">{done}</div>}

        <div className="adopt-foot">
          <label className="adopt-grp">
            {t('группа')}
            <input
              list="kervax-site-groups"
              placeholder={t('без группы')}
              value={group}
              onChange={(e) => setGroup(e.target.value)}
            />
            <datalist id="kervax-site-groups">
              {groups.map((g) => (
                <option key={g} value={g} />
              ))}
            </datalist>
          </label>
          <span className="muted small">
            {picked.length > ADOPT_MAX
              ? t('выбрано {n}, за раз добавим {m}', { n: picked.length, m: ADOPT_MAX })
              : t('будет создано мониторов: {n}', { n: picked.length })}
          </span>
          <button className="primary" disabled={busy || picked.length === 0} onClick={submit}>
            {busy ? t('добавляем…') : t('Поставить на мониторинг')}
          </button>
        </div>
        <div className="muted small adopt-hint">
          {t('Создаётся HTTPS-монитор на каждый домен. Первая проверка — на ближайшем тике планировщика.')}
        </div>
      </div>
    </div>,
    document.body,
  )
}
