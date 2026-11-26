import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { CountryFlag } from './CountryFlag'
import { useI18n } from './i18n'
import './HomeIssueMember.css'

export function HomeIssueMember({ name, country, detail, extra, age, select, muted, actions, onOpen }: {
  name?: string; country?: string; detail?: string; extra?: string; age?: string
  select?: ReactNode; muted?: boolean; actions?: ReactNode; onOpen: () => void
}) {
  const { t } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const [clipped, setClipped] = useState(false)
  const id = useId()
  const toggle = useRef<HTMLButtonElement>(null)
  const nameText = useRef<HTMLSpanElement>(null)
  const detailText = useRef<HTMLSpanElement>(null)
  const label = t(actions ? 'Действия' : 'Подробности')
  const target = name || t('Открыть объект')
  const hasPanel = !!(actions || extra)

  useEffect(() => {
    if (expanded || hasPanel || !nameText.current) return
    const nameEl = nameText.current
    const detailEl = detailText.current
    const observer = new ResizeObserver(() => {
      if (nameEl.clientWidth === 0) return
      setClipped(nameEl.scrollWidth > nameEl.clientWidth || !!detailEl && detailEl.scrollWidth > detailEl.clientWidth)
    })
    observer.observe(nameEl)
    if (detailEl) observer.observe(detailEl)
    return () => observer.disconnect()
  }, [detail, expanded, hasPanel, target])

  return (
    <li className="home-queue-inspector-member" data-muted={muted || undefined}
      data-expanded={expanded || undefined} onKeyDownCapture={(event) => {
      if (event.key === 'Escape' && expanded) {
        event.preventDefault()
        event.stopPropagation()
        setExpanded(false)
        toggle.current?.focus()
      }
    }}>
      <div className="home-queue-member-line">
        {select && <label className="home-queue-member-select">{select}</label>}
        <div id={id} className="home-queue-member-summary">
          <div className="home-queue-member-ident">
            <button type="button" className="home-queue-member-target" aria-label={target} title={target} onClick={onOpen}>
              <CountryFlag code={country} />
              <span ref={nameText} className="home-queue-member-name">{target}</span>
              <span aria-hidden="true" className="home-queue-member-arrow">→</span>
            </button>
            {age && <span className="home-queue-member-age">{age}</span>}
          </div>
          {detail && <span ref={detailText} className="home-queue-member-detail home-queue-member-preview" title={detail}>{detail}</span>}
        </div>
        {(hasPanel || clipped) && <button ref={toggle} type="button" className="ghost small home-queue-member-toggle"
          aria-label={`${label} — ${target}`} aria-expanded={expanded} aria-controls={hasPanel ? `${id} ${id}-panel` : id}
          onClick={() => setExpanded((open) => !open)}>
          {label}<span className="home-queue-member-chevron" aria-hidden="true" />
        </button>}
      </div>
      {hasPanel && <div id={`${id}-panel`} className="home-queue-member-panel" hidden={!expanded}>
        {extra && <div className="home-queue-member-detail">{extra}</div>}
        {actions && <div className="home-queue-inspector-actions">{actions}</div>}
      </div>}
    </li>
  )
}
