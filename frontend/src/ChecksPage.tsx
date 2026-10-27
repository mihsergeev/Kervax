import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  bulkDeleteChecks,
  checksOverview,
  createCheck,
  deleteCheck,
  discoveredDomains,
  reorderChecks,
  type Check,
  type CheckForm,
  type CheckStatus,
  type ChecksOverview,
  type Discovered,
} from './api'
import { BulkModal } from './BulkModal'
import { CheckDetail } from './CheckDetail'
import { EXPIRY_WARN_DAYS, expiryText, expiryTone } from './checkUtils'
import { ExpirySnoozePick, expEntry, expHidden, expSnoozeText, expSnoozeTip, type ExpKind } from './ExpirySnooze'
import { useUrlCard } from './deeplink'
import { AdoptSitesModal, adoptable } from './AdoptSitesModal'
import { AddMonitor } from './AddMonitor'
import { StatusBar } from './charts/StatusBar'
import { ImportModal } from './ImportModal'
import { useI18n } from './i18n'
import { MaintChip } from './MaintModal'
import { useAuth } from './auth'
import { knownActive, knownMeta, knownText } from './serverUtils'
import { HomeTile, tileIssues } from './HomeTiles'
import { byTone, siteIssueRows } from './sectionIssues'

type Props = {
  onUnauthorized: () => void
  openCheckId?: number | null
  openSec?: string | null // раздел карточки монитора из ссылки: ?check=id&sec=exp
  onConsumed?: () => void
}

const EMPTY: CheckForm = {
  name: '',
  type: 'http',
  target: '',
  port: 0,
  interval_seconds: 60,
  timeout_ms: 10000,
  degraded_ms: 2000,
  retries: 3,
  alert_after_failures: 3,
  degraded_after_failures: 10,
  check_locations: false, // локации — опционально; по умолчанию не гоняем через прокси
  expected_status: '200-399',
  keyword_up: '',
  keyword_down: '',
  http_headers: '',
  ignore_tls: false,
  probe_local: false,
  check_ssl: true,
  check_domain: true,
  ssl_warn_days: EXPIRY_WARN_DAYS,
  domain_warn_days: EXPIRY_WARN_DAYS,
  enabled: true,
  extra_paths: [],
}

function StatusDot({ status }: { status: CheckStatus }) {
  return <span className={`sdot sdot-${status}`} />
}

function uptimeTone(u: number): CheckStatus {
  return u >= 99 ? 'up' : u >= 95 ? 'degraded' : 'down'
}

// класс бейджа истечения: '' = не показывать, иначе тон по порогам
// срок в строке списка: с месяца до истечения, алертный порог - цветом (expiryTone)
const warnBadge = expiryTone

export function ChecksPage({ onUnauthorized, openCheckId, openSec = null, onConsumed }: Props) {
  const { t } = useI18n()
  const { isViewer } = useAuth()
  const [ov, setOv] = useState<ChecksOverview | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [moveErr, setMoveErr] = useState<string | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  // Домены, которые агенты видят на веб-серверах парка. Полезны ровно одним: показать,
  // что часть сайтов вообще не под мониторингом — сам по себе раздел «Сайты» об этом
  // знать не может, там только то, что человек когда-то завёл руками.
  const [found, setFound] = useState<Discovered | null>(null)
  const [wizard, setWizard] = useState(false)
  // новый монитор - через форму с пробной проверкой (AddMonitor)
  const [addErr, setAddErr] = useState<string | null>(null)
  const [addErrFields, setAddErrFields] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [detailId, setDetailId] = useState<number | null>(null)
  // раздел из ссылки - до первого открытия карточки этого монитора
  const [linkSec, setLinkSec] = useState<{ id: number | null; sec: string | null }>({ id: null, sec: null })
  const [bulkOpen, setBulkOpen] = useState(false)
  // область bulk-настройки: null = ко всем; массив id = к выбранным
  const [bulkIds, setBulkIds] = useState<number[] | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [statusFilter, setStatusFilter] = useState<CheckStatus | 'all' | 'disabled' | 'partial' | 'flaky' | 'problem'>(
    'all',
  )
  const [query, setQuery] = useState('')
  // режим массового выбора (чекбоксы + удаление выбранных)
  const [selectMode, setSelectMode] = useState(false)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const toggleFilter = (s: CheckStatus | 'disabled' | 'partial' | 'flaky') =>
    setStatusFilter((cur) => (cur === s ? 'all' : s))
  const [groupBy, setGroupBy] = useState<'group' | 'type' | 'none'>(
    () => (localStorage.getItem('kervax_groupby') as 'group' | 'type' | 'none') || 'group',
  )
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const setGrouping = (g: 'group' | 'type' | 'none') => {
    localStorage.setItem('kervax_groupby', g)
    setGroupBy(g)
  }
  const toggleCollapse = (k: string) =>
    setCollapsed((s) => {
      const n = new Set(s)
      if (n.has(k)) n.delete(k)
      else n.add(k)
      return n
    })

  // перетаскивание: монитор (внутри/между группами) и целая группа
  const dragRef = useRef<
    { kind: 'monitor'; id: number } | { kind: 'group'; key: string } | null
  >(null)
  const draggingRef = useRef(false)
  // превью раскладки во время перетаскивания: группы с упорядоченными id
  const [preview, setPreview] = useState<{ key: string; ids: number[] }[] | null>(null)
  const [dragOverId, setDragOverId] = useState<number | null>(null)
  const [dragOverKey, setDragOverKey] = useState<string | null>(null)

  // Пока предыдущий запрос обзора не вернулся, новый по таймеру не шлём: при
  // большом парке ответ шёл дольше интервала обновления, запросы накладывались
  // и добивали и базу, и браузер (в сети было видно 6 одновременных overview).
  const loadingRef = useRef(false)
  const load = useCallback(() => {
    if (loadingRef.current) return
    loadingRef.current = true
    checksOverview()
      .then((o) => {
        setOv(o)
        setErr(null)
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) return onUnauthorized()
        setErr(e instanceof Error ? e.message : t('Ошибка'))
      })
      .finally(() => {
        loadingRef.current = false
      })
  }, [onUnauthorized, t])

  // Найденные домены тянем один раз: это срез отчётов агентов, он меняется не чаще,
  // чем правят конфиг веб-сервера. 403 = учётке закрыты серверы — плашки просто не будет.
  useEffect(() => {
    if (isViewer) return
    discoveredDomains()
      .then(setFound)
      .catch(() => setFound(null))
  }, [isViewer])

  useEffect(() => {
    load()
    // во время перетаскивания не перезагружаем список — иначе порядок «прыгает»
    const id = window.setInterval(() => {
      if (!draggingRef.current) load()
    }, 12000)
    return () => window.clearInterval(id)
  }, [load])

  // диплинк из алерта/главной (?check=id) → открыть деталь и «съесть» id
  useEffect(() => {
    if (openCheckId) {
      setDetailId(openCheckId)
      setLinkSec({ id: openCheckId, sec: openSec })
      onConsumed?.()
    }
  }, [openCheckId, openSec, onConsumed])
  useUrlCard('check', detailId)

  // что реально можно предложить добавить: без масок/regexp, без уже покрытых и без
  // помеченных «мониторить не нужно» — иначе плашка висела бы вечно
  const skipSet = new Set((found?.ignored ?? []).map((x) => x.toLowerCase()))
  const newFound = (found?.domains ?? []).filter(
    (d) => adoptable(d.domain) && found?.hosts[d.domain.toLowerCase()] == null && !skipSet.has(d.domain.toLowerCase()),
  )

  const openAdd = () => {
    setAddErr(null)
    setAddErrFields([])
    setFormOpen(true)
  }

  const submitNew = async (f: CheckForm) => {
    setBusy(true)
    setAddErr(null)
    setAddErrFields([])
    try {
      await createCheck(f)
      setFormOpen(false)
      load()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setAddErr(e instanceof Error ? e.message : t('Ошибка'))
      if (e instanceof ApiError) setAddErrFields(e.fields)
    } finally {
      setBusy(false)
    }
  }

  // null - удален, '' - передумали, иначе текст отказа: его показывает окно монитора
  const remove = async (c: Check): Promise<string | null> => {
    if (!window.confirm(t('Удалить монитор «{name}»?', { name: c.name }))) return ''
    try {
      await deleteCheck(c.id)
      load()
      return null
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized()
      return e instanceof Error ? e.message : t('Ошибка')
    }
  }

  // порядок берём из API (ручной sort_order); статус-сортировку не применяем
  const checks = ov ? ov.checks : []
  const detail = detailId != null ? ov?.checks.find((c) => c.id === detailId) : undefined

  const groupNames = ov
    ? [...new Set(ov.checks.map((c) => c.group_name?.trim()).filter(Boolean))].sort()
    : []
  const probs = byTone(siteIssueRows(ov?.checks ?? [], t))
  const probIds = new Set(probs.map((x) => x.id))
  const probIss = tileIssues(probs, (_sec, id, _srv, sec) => {
    if (id == null) return
    setLinkSec({ id, sec: sec ?? null })
    setDetailId(id)
  })
  const knownN = (ov?.checks ?? []).filter((c) => c.enabled && knownActive(c.known_issue) &&
    (c.last_status === 'down' || c.last_status === 'degraded' || !!c.flaky_since)).length
  const siteFoot = [
    knownN > 0 && t('известных: {n}', { n: knownN }),
    ov && ov.open_incidents > 0 && t('открытых инцидентов: {n}', { n: ov.open_incidents }),
  ].filter(Boolean).join(' · ')
  const subOf = (...parts: (string | false | 0 | undefined)[]) => parts.filter(Boolean).join(' · ')
  const q = query.trim().toLowerCase()
  // перетаскивание доступно только в обычном виде (все статусы, группировка не «по типу»).
  // При активном поиске/выборе выключено: drop отправил бы только видимую
  // (отфильтрованную) часть списка и поломал бы общий порядок
  const canReorder =
    !isViewer && statusFilter === 'all' && groupBy !== 'type' && !q && !selectMode
  const byStatus =
    statusFilter === 'all'
      ? checks
      : statusFilter === 'problem'
        ? checks.filter((c) => probIds.has(c.id))
      : statusFilter === 'partial'
        ? checks.filter(
            (c) => c.enabled && c.last_status !== 'down' && (c.loc_down?.length ?? 0) > 0,
          )
        : statusFilter === 'flaky'
          ? checks.filter((c) => c.enabled && c.last_status !== 'down' && !!c.flaky_since)
        : statusFilter === 'disabled'
          ? checks.filter((c) => !c.enabled)
        : checks.filter((c) => c.enabled && c.last_status === statusFilter)
  const visibleBase = q
    ? byStatus.filter((c) =>
        [c.name, c.target, c.group_name].some((v) =>
          v?.toLowerCase().includes(q),
        ),
      )
    : byStatus
  const byId = new Map(checks.map((c) => [c.id, c] as const))

  const TYPE_LABEL: Record<string, string> = {
    http: 'HTTP',
    tcp_port: 'TCP',
    cert: 'TLS',
    push: 'PUSH',
    dns: 'DNS',
  }
  // базовая группировка. Порядок групп: «по группе» — по первому появлению в
  // sort_order (ручной, двигается перетаскиванием), «Без группы» всегда в конце;
  // «по типу» — по алфавиту (это представление, не переставляется).
  const baseGroups: { key: string; ids: number[] }[] = (() => {
    if (groupBy === 'none') return [{ key: 'all', ids: visibleBase.map((c) => c.id) }]
    const map = new Map<string, number[]>()
    const order: string[] = []
    for (const c of visibleBase) {
      const k =
        groupBy === 'type'
          ? TYPE_LABEL[c.type] || c.type
          : c.group_name?.trim() || ''
      if (!map.has(k)) {
        map.set(k, [])
        order.push(k)
      }
      map.get(k)!.push(c.id)
    }
    let keys = groupBy === 'type' ? [...order].sort() : order
    if (groupBy === 'group' && keys.includes(''))
      keys = [...keys.filter((k) => k !== ''), '']
    return keys.map((k) => ({ key: k, ids: map.get(k)! }))
  })()

  // эффективная раскладка = превью (во время перетаскивания) либо база
  const effGroups = (preview && canReorder ? preview : baseGroups)
    .map((g) => ({
      key: g.key,
      items: g.ids.map((id) => byId.get(id)).filter((c): c is Check => !!c),
    }))
    .filter((g) => g.items.length > 0)
  const grouped = effGroups.map((g) => ({
    key: g.key,
    label: g.key === 'all' ? '' : g.key || t('Без группы'),
    items: g.items,
  }))

  // ——— перетаскивание ———
  const snapshot = (prev: { key: string; ids: number[] }[] | null) =>
    (prev ?? baseGroups).map((g) => ({ key: g.key, ids: [...g.ids] }))

  const moveMonitor = (id: number, dstKey: string, beforeId: number | null) => {
    setPreview((prev) => {
      const groups = snapshot(prev)
      for (const g of groups) {
        const i = g.ids.indexOf(id)
        if (i >= 0) g.ids.splice(i, 1)
      }
      let dst = groups.find((g) => g.key === dstKey)
      if (!dst) {
        dst = { key: dstKey, ids: [] }
        groups.push(dst)
      }
      if (beforeId == null) dst.ids.push(id)
      else {
        const at = dst.ids.indexOf(beforeId)
        if (at < 0) dst.ids.push(id)
        else dst.ids.splice(at, 0, id)
      }
      return groups.filter((g) => g.ids.length > 0)
    })
  }
  const moveGroup = (srcKey: string, dstKey: string) => {
    if (srcKey === dstKey) return
    setPreview((prev) => {
      const groups = snapshot(prev)
      const from = groups.findIndex((g) => g.key === srcKey)
      const to = groups.findIndex((g) => g.key === dstKey)
      if (from < 0 || to < 0) return groups
      const [m] = groups.splice(from, 1)
      groups.splice(to, 0, m)
      return groups
    })
  }

  const startMon = (id: number) => {
    dragRef.current = { kind: 'monitor', id }
    draggingRef.current = true
    setPreview(snapshot(null))
    setDragOverId(id)
  }
  const startGroup = (key: string) => {
    dragRef.current = { kind: 'group', key }
    draggingRef.current = true
    setPreview(snapshot(null))
    setDragOverKey(key)
  }
  const enterRow = (targetId: number, groupKey: string) => {
    const d = dragRef.current
    if (!d) return
    if (d.kind === 'group') {
      setDragOverKey(groupKey)
      moveGroup(d.key, groupKey)
      return
    }
    if (d.id === targetId) return
    setDragOverId(targetId)
    moveMonitor(d.id, groupBy === 'group' ? groupKey : 'all', targetId)
  }
  const enterHeader = (groupKey: string) => {
    const d = dragRef.current
    if (!d) return
    if (d.kind === 'group') {
      setDragOverKey(groupKey)
      moveGroup(d.key, groupKey)
      return
    }
    setDragOverKey(groupKey)
    moveMonitor(d.id, groupKey, null) // в конец группы
  }
  const drop = async () => {
    const d = dragRef.current
    dragRef.current = null
    setDragOverId(null)
    setDragOverKey(null)
    if (!d) {
      draggingRef.current = false
      return
    }
    const groups = preview ?? baseGroups
    const order: { id: number; group_name?: string }[] = []
    for (const g of groups)
      for (const id of g.ids)
        order.push(groupBy === 'group' ? { id, group_name: g.key } : { id })
    setPreview(null)
    // оптимистично фиксируем в ov (порядок + возможная смена группы), чтобы не мигало
    setOv((prev) =>
      prev
        ? {
            ...prev,
            checks: order
              .map((o) => {
                const c = prev.checks.find((x) => x.id === o.id)
                if (!c) return undefined
                return groupBy === 'group'
                  ? { ...c, group_name: o.group_name ?? c.group_name }
                  : c
              })
              .filter((c): c is Check => c !== undefined),
          }
        : prev,
    )
    try {
      await reorderChecks(order)
    } catch (e) {
      draggingRef.current = false
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      // порядок не сохранился: вернуть настоящий и сказать почему
      setMoveErr(e instanceof Error ? e.message : t('Ошибка'))
      load()
      return
    }
    setMoveErr(null)
    draggingRef.current = false
    load()
  }
  const dragEnd = () => {
    // drop уже обработал перестановку — persist ещё в полёте, не мешаем
    if (dragRef.current == null) return
    // бросили мимо — откатываем превью
    dragRef.current = null
    draggingRef.current = false
    setDragOverId(null)
    setDragOverKey(null)
    setPreview(null)
  }

  // ——— массовый выбор / удаление ———
  const toggleSelected = (id: number) =>
    setSelected((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  const exitSelect = () => {
    setSelectMode(false)
    setSelected(new Set())
  }
  const deleteSelected = async () => {
    const ids = [...selected]
    if (ids.length === 0) return
    if (
      !window.confirm(
        t('Удалить выбранные мониторы ({n} шт.) вместе с историей?', {
          n: ids.length,
        }),
      )
    )
      return
    try {
      await bulkDeleteChecks(ids)
      exitSelect()
      load()
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    }
  }

  return (
    <div>
      {ov && ov.total > 0 && (
        <div className="home-tiles sec-tiles">
          {/* беды - те же строки, что у плитки "Сайты" на главной (sectionIssues.siteIssueRows):
              лежащие, деградация, "через раз" и признаки взлома. Плитка показывает их крупно и
              фильтрует список по ним */}
          <HomeTile
            label={t('Сайты')}
            value={String(ov.up)}
            unit={t('из {n}', { n: ov.total })}
            ok={t('все в норме')}
            foot={siteFoot}
            total={subOf(t('работают {n} из {total}', { n: ov.up, total: ov.total }), siteFoot)}
            issues={probIss}
            wide
            active={probIss.length ? statusFilter === 'problem' : statusFilter === 'all'}
            title={probIss.length ? t('показать мониторы с проблемами') : t('показать все')}
            onOpen={() => setStatusFilter((f) => (probIss.length && f !== 'problem' ? 'problem' : 'all'))}
          />
          {/* Сайт может отвечать с основной проверки и НЕ отвечать из локации - по статусу он
              "работает" и в беды не идет, но видно его здесь */}
          {ov.partial > 0 && (
            <HomeTile
              label={t('Частично')}
              value={String(ov.partial)}
              ok={t('не отвечают из части локаций')}
              okTone="warn"
              active={statusFilter === 'partial'}
              title={statusFilter === 'partial' ? t('снять фильтр') : t('показать в списке')}
              onOpen={() => toggleFilter('partial')}
            />
          )}
          {ov.disabled > 0 && (
            <HomeTile
              label={t('Выключены')}
              value={String(ov.disabled)}
              ok={t('не проверяются')}
              okTone="muted"
              active={statusFilter === 'disabled'}
              title={statusFilter === 'disabled' ? t('снять фильтр') : t('показать в списке')}
              onOpen={() => toggleFilter('disabled')}
            />
          )}
        </div>
      )}

      <div className="checks-head">
        <h2>
          {t('Мониторы')}
          {ov && ov.open_incidents > 0 && (
            <span className="inc-badge">
              {t('{n} откр. инцидентов', { n: ov.open_incidents })}
            </span>
          )}
        </h2>
        <div className="checks-head-actions">
          {ov && ov.total > 1 && (
            <div className="win-switch groupby">
              <button
                className={`win-btn${groupBy === 'group' ? ' win-btn-active' : ''}`}
                onClick={() => setGrouping('group')}
              >
                {t('По группе')}
              </button>
              <button
                className={`win-btn${groupBy === 'type' ? ' win-btn-active' : ''}`}
                onClick={() => setGrouping('type')}
              >
                {t('По типу')}
              </button>
              <button
                className={`win-btn${groupBy === 'none' ? ' win-btn-active' : ''}`}
                onClick={() => setGrouping('none')}
              >
                {t('Без групп')}
              </button>
            </div>
          )}
          {selectMode ? (
            <>
              <span className="select-count">
                {t('Выбрано: {n}', { n: selected.size })}
              </span>
              <button
                className="ghost"
                onClick={() =>
                  setSelected(new Set(visibleBase.map((c) => c.id)))
                }
              >
                {t('Все видимые')}
              </button>
              <button
                className="ghost"
                disabled={selected.size === 0}
                onClick={() => {
                  setBulkIds([...selected])
                  setBulkOpen(true)
                }}
              >
                {t('Настроить ({n})', { n: selected.size })}
              </button>
              <button
                className="ghost danger-btn"
                disabled={selected.size === 0}
                onClick={deleteSelected}
              >
                {t('Удалить ({n})', { n: selected.size })}
              </button>
              <button className="ghost" onClick={exitSelect}>
                {t('Готово')}
              </button>
            </>
          ) : (
            <>
              {!isViewer && ov && ov.total > 0 && (
                <button className="ghost" onClick={() => setSelectMode(true)}>
                  {t('Выбрать')}
                </button>
              )}
              {!isViewer && ov && ov.total > 0 && (
                <button
                  className="ghost"
                  onClick={() => {
                    setBulkIds(null)
                    setBulkOpen(true)
                  }}
                >
                  {t('Применить ко всем')}
                </button>
              )}
              {!isViewer && (
                <button className="ghost" onClick={() => setImportOpen(true)}>
                  {t('Массово')}
                </button>
              )}
              {!isViewer && (
                <button onClick={openAdd}>{t('+ Добавить монитор')}</button>
              )}
            </>
          )}
        </div>
      </div>

      {ov && ov.total > 5 && (
        <div className="checks-search-row">
          <input
            className="checks-search"
            placeholder={t('Поиск: имя, адрес, группа…')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button className="ghost search-clear" onClick={() => setQuery('')}>
              {t('Сбросить')}
            </button>
          )}
        </div>
      )}

      {err && <p className="form-error">{err}</p>}
      {moveErr && <p className="form-error">{t('Порядок не сохранен: {err}', { err: moveErr })}</p>}

      {/* Когда из одной точки «падает» заметная доля мониторов — сломана почти
          наверняка она сама, а не все эти сайты разом. Живой случай: из Алматы
          не отвечали шесть сайтов с РАЗНЫМИ ошибками (403, 520, ReadTimeout,
          ConnectError), а с основной проверки все отдавали 200. */}
      {(ov?.loc_summary ?? [])
        .filter((l) => l.down >= 3 || (l.total > 0 && l.down / l.total >= 0.5))
        .map((l) => (
          <div key={l.id} className="loc-warn">
            <span className="loc-warn-ico">🌍</span>
            <span>
              {t('Точка проверки «{name}»: не отвечают {down} из {total} мониторов — похоже на проблему самой точки, а не сайтов.', {
                name: l.name,
                down: l.down,
                total: l.total,
              })}
            </span>
          </div>
        ))}

      {!isViewer && newFound.length > 0 && (
        <div className="discover-bar">
          <span className="discover-ico">🧭</span>
          <span>
            {t('На серверах найдено доменов вне мониторинга: {n}', { n: newFound.length })}
          </span>
          <button className="primary" onClick={() => setWizard(true)}>
            {t('Посмотреть и добавить')}
          </button>
        </div>
      )}

      {wizard && found && (
        <AdoptSitesModal
          title={t('Домены, найденные на серверах')}
          items={found.domains}
          hosts={found.hosts}
          groups={found.groups}
          ignored={found.ignored}
          onClose={() => setWizard(false)}
          onDone={(hosts, created) => {
            setFound((f) => (f ? { ...f, hosts } : f))
            if (created > 0) load()
          }}
          onIgnored={(domains, ignore) =>
            setFound((f) => {
              if (!f) return f
              const next = new Set(f.ignored)
              for (const d of domains) {
                if (ignore) next.add(d)
                else next.delete(d)
              }
              return { ...f, ignored: [...next] }
            })
          }
          onOpenCheck={(id) => {
            setWizard(false)
            setDetailId(id)
          }}
        />
      )}

      {formOpen && (
        <AddMonitor
          base={EMPTY}
          groups={groupNames}
          busy={busy}
          error={addErr}
          errorFields={addErrFields}
          onSubmit={submitNew}
          onCancel={() => setFormOpen(false)}
          onOpenCheck={(id) => {
            setFormOpen(false)
            setDetailId(id)
          }}
          onUnauthorized={onUnauthorized}
        />
      )}

      {ov && checks.length === 0 && !formOpen && (
        <div className="card muted">
          {t('Пока нет мониторов. Нажмите «Добавить монитор».')}
        </div>
      )}

      {ov && visibleBase.length === 0 && checks.length > 0 && (
        <div className="card muted">
          {q ? <>
            {t('По запросу «{q}» ничего нет.', { q: query.trim() })}
            <button className="ghost search-clear" onClick={() => setQuery('')}>{t('Сбросить')}</button>
          </> : t('Нет мониторов с этим статусом.')}
        </div>
      )}

      {grouped.map((g) => {
        const groupDrag = canReorder && groupBy === 'group'
        const dragMonId =
          dragRef.current?.kind === 'monitor' ? dragRef.current.id : null
        return (
          <div
            key={g.key}
            className={`mon-group${
              dragOverKey === g.key && dragRef.current?.kind === 'group'
                ? ' group-drag-over'
                : ''
            }`}
          >
            {groupBy !== 'none' && (
              <div
                className="mon-group-head"
                onDragEnter={
                  canReorder
                    ? (e) => {
                        e.preventDefault()
                        enterHeader(g.key)
                      }
                    : undefined
                }
                onDragOver={canReorder ? (e) => e.preventDefault() : undefined}
                onDrop={
                  canReorder
                    ? (e) => {
                        e.preventDefault()
                        drop()
                      }
                    : undefined
                }
              >
                {groupDrag && (
                  <span
                    className="mon-grip mon-group-grip"
                    draggable
                    onDragStart={() => startGroup(g.key)}
                    onDragEnd={dragEnd}
                    onClick={(e) => e.stopPropagation()}
                    title={t('Перетащите группу')}
                    aria-label={t('Перетащите группу')}
                  >
                    ⠿
                  </span>
                )}
                <button
                  className="mon-group-toggle"
                  onClick={() => toggleCollapse(g.key)}
                >
                  <span className="mon-group-caret">
                    {collapsed.has(g.key) ? '▸' : '▾'}
                  </span>
                  <span className="mon-group-name">{g.label}</span>
                  <span className="mon-group-n" title={t('Мониторов в группе')}>
                    {g.items.length}
                  </span>
                </button>
                <GroupSummary items={g.items} />
              </div>
            )}
            {!collapsed.has(g.key) && (
              <div className="check-list">
                {g.items.map((c) => (
                  <CheckRow
                    key={c.id}
                    check={c}
                    showGroup={groupBy !== 'group'}
                    canEdit={!isViewer && !selectMode}
                    onChanged={load}
                    onOpen={() =>
                      selectMode ? toggleSelected(c.id) : setDetailId(c.id)
                    }
                    selecting={selectMode}
                    checked={selected.has(c.id)}
                    dragging={canReorder && dragMonId === c.id}
                    dragOver={
                      canReorder && dragOverId === c.id && dragMonId !== c.id
                    }
                    reorder={
                      canReorder
                        ? {
                            onDragStart: () => startMon(c.id),
                            onDragEnter: () => enterRow(c.id, g.key),
                            onDrop: () => drop(),
                            onDragEnd: () => dragEnd(),
                          }
                        : undefined
                    }
                  />
                ))}
              </div>
            )}
          </div>
        )
      })}

      {detail && (
        <CheckDetail
          key={detail.id}
          check={detail}
          groups={groupNames}
          initialSec={linkSec.id === detail.id ? linkSec.sec : null}
          onClose={() => {
            setDetailId(null)
            setLinkSec({ id: null, sec: null })
          }}
          onSaved={load}
          onRun={load}
          onDelete={async () => {
            const r = await remove(detail)
            if (r === null) setDetailId(null)
            return r
          }}
          onUnauthorized={onUnauthorized}
        />
      )}

      {bulkOpen && (
        <BulkModal
          ids={bulkIds ?? undefined}
          onClose={() => setBulkOpen(false)}
          onApplied={() => {
            load()
            if (bulkIds) exitSelect() // применили к выбранным → выходим из режима выбора
          }}
          onUnauthorized={onUnauthorized}
        />
      )}

      {importOpen && (
        <ImportModal
          groups={groupNames}
          onClose={() => setImportOpen(false)}
          onImported={load}
          onUnauthorized={onUnauthorized}
        />
      )}
    </div>
  )
}

function GroupSummary({ items }: { items: Check[] }) {
  // счётчик мониторов вынесен к названию группы (.mon-group-n); тут — только здоровье
  // выключенный монитор не проверяется: его последний статус в шапке группы не считаем, как и в плитках
  const down = items.filter((c) => c.enabled && c.last_status === 'down').length
  const degraded = items.filter((c) => c.enabled && c.last_status === 'degraded').length
  return (
    <span className="mon-group-sum">
      {down > 0 && <span className="uptime-badge t-down">{down} ▼</span>}
      {degraded > 0 && <span className="uptime-badge t-degraded">{degraded} ▼</span>}
      {down === 0 && degraded === 0 && <span className="t-up mon-group-ok">✓</span>}
    </span>
  )
}

// Срок сертификата или домена в строке списка. На пороге алерта нажатие - напомнить за 7/1 дней
// или не следить (ExpirySnooze.tsx); отложенный приглушен со 🔕, вернуть - в карточке
function ExpBadge({ c, kind, canEdit, onChanged }: { c: Check; kind: ExpKind; canEdit: boolean; onChanged?: () => void }) {
  const { t } = useI18n()
  const [pick, setPick] = useState(false)
  const on = kind === 'ssl' ? c.check_ssl : c.check_domain
  const days = kind === 'ssl' ? c.ssl_days : c.domain_days
  const warn = kind === 'ssl' ? c.ssl_warn_days : c.domain_warn_days
  if (!on || days == null || !warnBadge(days, warn)) return null
  const e = expEntry(c, kind)
  const hidden = expHidden(c, kind)
  const canPick = canEdit && !hidden && warn.length > 0 && days <= Math.max(...warn)
  if (pick && canPick)
    return <ExpirySnoozePick check={c} kind={kind} short onDone={() => onChanged?.()} onClose={() => setPick(false)} />
  const what = kind === 'ssl' ? t('SSL истекает') : t('Домен истекает')
  return (
    <span
      className={`uptime-badge ${hidden ? 'muted exp-badge-off' : warnBadge(days, warn)}${canPick ? ' exp-badge-pick' : ''}`}
      title={hidden && e
        ? `${what} · ${expSnoozeText(e, t)}
${expSnoozeTip(e, t)}`
        : canPick ? `${what}
${t('Нажмите: напомнить позже или не следить')}` : what}
      onClick={canPick ? (ev) => { ev.stopPropagation(); setPick(true) } : undefined}
    >
      {kind === 'ssl' ? '🔐' : '🌐'} {expiryText(days, t)}{hidden ? ' 🔕' : ''}
    </span>
  )
}

function CheckRow({
  check: c,
  showGroup,
  onOpen,
  reorder,
  dragging,
  dragOver,
  selecting,
  checked,
  canEdit = false,
  onChanged,
}: {
  check: Check
  showGroup?: boolean
  canEdit?: boolean
  onChanged?: () => void
  onOpen: () => void
  reorder?: {
    onDragStart: () => void
    onDragEnter: () => void
    onDrop: () => void
    onDragEnd: () => void
  }
  dragging?: boolean
  dragOver?: boolean
  selecting?: boolean
  checked?: boolean
}) {
  const { t } = useI18n()
  const typeLabel = { http: 'HTTP', tcp_port: 'TCP', cert: 'TLS', push: 'PUSH', dns: 'DNS' }[c.type]
  const metric =
    c.type === 'cert' && c.last_value != null
      ? t('{n} дн.', { n: Math.round(c.last_value) })
      : c.last_latency_ms != null
        ? `${c.last_latency_ms} ${t('мс')}`
        : ''
  return (
    // вся строка кликабельна → деталь (там пуск/изменить/удалить); компактнее без кнопок
    <div
      className={`check-row check-row-compact clickable${reorder ? ' reorderable' : ''}${
        selecting ? ' selecting' : ''
      }${dragging ? ' dragging' : ''}${dragOver ? ' drag-over' : ''}`}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onOpen()}
      title={selecting ? t('Выбрать монитор') : t('Открыть детали')}
      onDragEnter={reorder && ((e) => { e.preventDefault(); reorder.onDragEnter() })}
      onDragOver={reorder && ((e) => e.preventDefault())}
      onDrop={reorder && ((e) => { e.preventDefault(); reorder.onDrop() })}
    >
      {selecting && (
        <input
          type="checkbox"
          className="row-check"
          checked={!!checked}
          onChange={onOpen}
          onClick={(e) => e.stopPropagation()}
        />
      )}
      {reorder && (
        <span
          className="mon-grip"
          draggable
          onDragStart={reorder.onDragStart}
          onDragEnd={reorder.onDragEnd}
          onClick={(e) => e.stopPropagation()}
          title={t('Перетащите, чтобы изменить порядок')}
          aria-label={t('Перетащите, чтобы изменить порядок')}
        >
          ⠿
        </span>
      )}
      <StatusDot status={c.last_status} />
      <div className="check-main">
        <div className="check-name">
          {c.name}
          {c.note && (
            <span className="note-chip" title={c.note}>
              📝
            </span>
          )}
          {c.known_issue && (
            <span
              className="note-chip"
              title={`${t('Известная проблема')}: ${t('сбой монитора')} - ${knownText(c.known_issue, t)}${
                knownMeta(c.known_issue, t) ? ` (${knownMeta(c.known_issue, t)})` : ''
              }\n${t('Нажмите: открыть монитор, там же снять отметку')}`}
            >
              📌
            </span>
          )}
          <MaintChip kind="check" id={c.id} />
          {/* родитель молчит: монитор лежит из-за него, свой алерт ждет (app/parents.py) */}
          {(c.parents ?? []).filter((p) => p.silent).map((p) => (
            <span
              key={p.id}
              className="type-chip parent-chip"
              title={t('Сервер {srv} не на связи: алерт монитора ждет его, монитор назван в сообщении про сервер', {
                srv: p.name,
              })}
            >
              ⛓ {p.name}
            </span>
          ))}
          <span className="type-chip">{typeLabel}</span>
          {/* Зелёный по локальной проверке — это не то же самое, что «сайт виден
              посетителям»: снаружи он закрыт белым списком. Метка обязательна,
              иначе статус читается как обычная внешняя доступность. */}
          {c.probe_local && (
            <span
              className="type-chip"
              title={
                c.probe_server_name
                  ? t('Проверяется изнутри сервера {srv}: панель к сайту не ходит, снаружи он закрыт', {
                      srv: c.probe_server_name,
                    })
                  : t('Проверять локально включено, но домен не найден ни на одном сервере — проверять некому')
              }
            >
              {c.probe_server_name ? '🏠' : '🏠⚠️'}
            </span>
          )}
          {showGroup && c.group_name && (
            <span className="type-chip group-chip">{c.group_name}</span>
          )}
          {c.uptime_24h != null && (
            <span
              className={`uptime-badge t-${uptimeTone(c.uptime_24h)}`}
              title={t('Аптайм за 24ч')}
            >
              {c.uptime_24h}%
            </span>
          )}
          <ExpBadge c={c} kind="ssl" canEdit={canEdit} onChanged={onChanged} />
          <ExpBadge c={c} kind="domain" canEdit={canEdit} onChanged={onChanged} />
          {/* Основная проверка зелёная, а из локации сайт не отвечает — строка
              выглядела полностью исправной, и проблему было видно только внутри
              монитора. Помечаем прямо в списке. */}
          {c.enabled && c.last_status !== 'down' && (c.loc_down?.length ?? 0) > 0 && (
            <span
              className="uptime-badge t-degraded"
              title={t('Не отвечает из этих точек проверки: {list}', {
                list: (c.loc_down ?? []).join(', '),
              })}
            >
              🌍 {(c.loc_down ?? []).join(', ')}
            </span>
          )}
          {c.enabled && c.last_status !== 'down' && c.flaky_since && (
            <span
              className="uptime-badge t-degraded"
              title={t('Сбои идут вперемешку с успешными проверками, с {time}', {
                time: new Date(c.flaky_since).toLocaleString(),
              })}
            >
              🟠 {t('через раз')}
            </span>
          )}
          {!c.enabled && <span className="type-chip off">{t('выкл')}</span>}
        </div>
        <div className="check-target">
          {c.type === 'push'
            ? t('сигнал не реже, чем раз в {period}', { period: pushPeriodLabel(c.push_period || 3600, t) })
            : c.type === 'dns'
              ? `${c.target} ${c.dns_type || 'A'}`
              : `${c.target}${c.port ? `:${c.port}` : ''}`}
          {(c.extra_paths ?? []).length > 0 && (
            <span
              className="check-target-paths"
              title={t('Дополнительные пути: {list}', { list: c.extra_paths.join(', ') })}
            >
              {' + '}{c.extra_paths.join(' ')}
            </span>
          )}
        </div>
      </div>
      {/* контейнер ленты всегда в DOM — держит grid-колонку ровной даже без истории */}
      <div className="check-beats">
        {c.beats && c.beats.length > 0 && (
          <StatusBar segments={c.beats.map((s) => ({ status: s }))} />
        )}
      </div>
      <div className="check-meta">
        <div className="check-metric">{metric}</div>
        <div className="check-msg muted small" title={c.last_message}>
          {c.last_message || '—'}
        </div>
      </div>
    </div>
  )
}

// Период push-монитора словами для строки списка: 15 мин, 6 ч, 1 сутки
function pushPeriodLabel(sec: number, t: (s: string, v?: Record<string, string | number>) => string): string {
  if (sec % 86400 === 0) return t('{n} сут', { n: sec / 86400 })
  if (sec % 3600 === 0) return t('{n} ч', { n: sec / 3600 })
  return t('{n} мин', { n: Math.round(sec / 60) })
}
