import { useCallback, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { CountryFlag } from './CountryFlag'
import { useI18n } from './i18n'
import type { TallyTile } from './serverUtils'

const QUEUE_LIMIT = 5

export type QueueSeverity = 'all' | 'critical' | 'warnings'

export type QueueIssue = {
  icon: string
  title: string
  name?: string
  country?: string
  age?: string
  context?: ReactNode
  details?: string
  note?: string
  actions?: () => ReactNode
  onOpen?: () => void
}

export type QueueRow = QueueIssue & {
  key: string
  area: TallyTile
  critical: boolean
  sectionLabel: string
  members?: () => ReactNode
}

function QueueRows({ rows, selectedKey, inspectorId, onSelect, onCollapse }: {
  rows: QueueRow[]
  selectedKey?: string
  inspectorId: string
  onSelect: (key: string) => void
  onCollapse: (key: string) => void
}) {
  const { t } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const id = useId()
  if (!rows.length) return null
  // A refresh may move the selected issue past the preview. Keep it reachable until the user folds the list.
  const selectedPastPreview = rows.findIndex((row) => row.key === selectedKey) >= QUEUE_LIMIT
  const showAll = expanded || selectedPastPreview
  return (
    <>
      <div id={id} className="home-queue-rows">
        {(showAll ? rows : rows.slice(0, QUEUE_LIMIT)).map((row) => (
          <button key={row.key} type="button" className="home-queue-row"
            data-severity={row.critical ? 'critical' : 'warnings'} aria-controls={inspectorId}
            aria-current={row.key === selectedKey ? true : undefined} onClick={() => onSelect(row.key)}>
            <span className="home-queue-row-icon" aria-hidden="true">{row.icon}</span>
            <span className="home-queue-row-content">
              <strong className="home-queue-row-title">{row.title}</strong>
              {(row.name || row.context) && <span className="home-queue-row-meta muted">
                {row.country && <><CountryFlag code={row.country} />{' '}</>}
                {row.name}{row.context}
              </span>}
            </span>
            <span className="home-queue-row-end">
              {row.age && <span className="home-queue-row-age muted">{row.age}</span>}
              <span aria-hidden="true">›</span>
            </span>
          </button>
        ))}
      </div>
      {rows.length > QUEUE_LIMIT && (
        <button type="button" className="ghost small home-queue-more" aria-expanded={showAll} aria-controls={id}
          onClick={() => {
            if (showAll && selectedPastPreview) onCollapse(rows[0].key)
            setExpanded(!showAll)
          }}>
          {showAll ? t('свернуть') : t('Показать еще {n}', { n: rows.length - QUEUE_LIMIT })}
        </button>
      )}
    </>
  )
}

type Selection = { key: string; opened: boolean }

export function HomeIssueQueue({ rows, criticalRows, warningRows, severity, loading, empty, focusEmpty }: {
  rows: QueueRow[]; criticalRows: QueueRow[]; warningRows: QueueRow[]; severity: QueueSeverity
  loading: boolean; empty: ReactNode; focusEmpty: () => void
}) {
  const { t } = useI18n()
  const id = useId()
  const [warningsOpen, setWarningsOpen] = useState(false)
  const [selection, setSelection] = useState<Selection | null>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const mobileWasOpen = useRef(false)
  const restoreFocus = useRef(false)
  const visible = severity === 'all' ? criticalRows.concat(warningsOpen ? warningRows : []) : rows
  const selected = visible.find((row) => row.key === selection?.key) ?? visible[0]
  const mobileOpen = selection?.opened ?? false
  if (!warningRows.length && warningsOpen) setWarningsOpen(false)
  // Apply the default once the initial sources settle. An explicit click while loading still takes precedence.
  if ((!loading || selection) && selected?.key !== selection?.key) {
    setSelection(selected ? { key: selected.key, opened: false } : null)
  }

  const focusRow = useCallback(() => {
    const list = listRef.current
    const row = list?.querySelector<HTMLButtonElement>('.home-queue-row[aria-current="true"]')
    const target = row ?? list?.querySelector('summary')
    if (target) target.focus()
    else focusEmpty()
  }, [focusEmpty])
  const close = () => {
    if (!selection) return
    restoreFocus.current = true
    setSelection({ ...selection, opened: false })
  }
  useLayoutEffect(() => {
    const button = closeRef.current
    const open = !!selection?.opened && !!button?.getClientRects().length
    if (open) button?.focus()
    else if (restoreFocus.current || (mobileWasOpen.current && document.activeElement === document.body)) focusRow()
    mobileWasOpen.current = open
    restoreFocus.current = false
  }, [focusRow, selection])

  if (!rows.length) return empty

  const rowProps = {
    selectedKey: selected?.key,
    inspectorId: id,
    onSelect: (key: string) => setSelection({ key, opened: true }),
    onCollapse: (key: string) => setSelection({ key, opened: false }),
  }
  const actions = selected?.actions?.()
  return (
    <div className="home-queue-split">
      <div className="home-queue-list" ref={listRef}>
        <QueueRows rows={severity === 'all' ? criticalRows : rows} {...rowProps} />
        {severity === 'all' && warningRows.length > 0 && (
          <details className="home-queue-warnings" onToggle={(event) => setWarningsOpen(event.currentTarget.open)}>
            <summary><span>{t('Предупреждения')}</span><span className="net-n">{warningRows.length}</span></summary>
            <QueueRows rows={warningRows} {...rowProps} />
          </details>
        )}
      </div>
      <aside id={id} className="home-queue-inspector" aria-label={t('Карточка проблемы')}
        data-mobile-open={mobileOpen} onKeyDownCapture={(event) => {
          if (event.key === 'Escape' && closeRef.current?.getClientRects().length) {
            event.preventDefault()
            event.stopPropagation()
            close()
          }
        }}>
        {selected ? (
          <>
            <div className="home-queue-inspector-head">
              <span className="home-queue-inspector-severity" data-severity={selected.critical ? 'critical' : 'warnings'}>
                {t(selected.critical ? 'Критичная' : 'Предупреждение')}
              </span>
              <button ref={closeRef} type="button" className="ghost small home-queue-inspector-close"
                aria-label={t('Закрыть подробности')} onClick={close}>×</button>
            </div>
            <div key={selected.key}>
              <h3 className="home-queue-inspector-title">{selected.title}</h3>
              {selected.details && <div className="home-queue-inspector-body">{selected.details}</div>}
              <dl className="home-queue-inspector-meta">
                <div><dt>{t('Раздел')}</dt><dd>{selected.sectionLabel}</dd></div>
                {selected.name && <div>
                  <dt>{t(selected.members ? 'Группа' : 'Объект')}</dt>
                  <dd>{selected.country && <><CountryFlag code={selected.country} />{' '}</>}{selected.name}</dd>
                </div>}
                {selected.age && <div><dt>{t('Длительность')}</dt><dd>{selected.age}</dd></div>}
                {selected.note && <div><dt>{t('Заметка')}</dt><dd>{selected.note}</dd></div>}
              </dl>
              {(selected.onOpen || actions) && <div className="home-queue-inspector-actions">
                {selected.onOpen && <button type="button" className="ghost small" onClick={selected.onOpen}>
                  {t('Открыть объект')} <span aria-hidden="true">→</span>
                </button>}
                {actions}
              </div>}
              {selected.members?.()}
            </div>
          </>
        ) : <p className="home-queue-inspector-empty muted">{t('Выберите проблему в очереди')}</p>}
      </aside>
    </div>
  )
}
