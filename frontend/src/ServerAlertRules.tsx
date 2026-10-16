import { useState } from 'react'
import { type ServerAlertRule as Rule, type ServerAlertRules as Rules } from './api'
import { useI18n } from './i18n'

// Редактор ГЛОБАЛЬНЫХ правил алертов (серверов или сайтов): тумблер вкл/выкл + текст.
// Правила применяются КО ВСЕМ. Пороги и точечные исключения — в карточке сущности.
// Область правила (только группы, серверы, мониторы) здесь больше не задается, но у старых
// установок она могла остаться и действует: ее видно в строке правила, а снять можно кнопкой.
// Загружает и сохраняет правила окно алертов, здесь только правка. Подпись справа - только у
// выключенных и ограниченных: "ко всем" в каждой строке из 65 было шумом.

export const scopedKinds = (d: Rules) => d.kinds.filter((k) => d.rules[k.key] && d.rules[k.key].scope_type !== 'all')

export const unscopeRules = (d: Rules): Rules => ({
  ...d,
  rules: Object.fromEntries(
    Object.entries(d.rules).map(([k, r]) => [k, { ...r, scope_type: 'all' as const, scope: [] }]),
  ),
})

// Старое ограничение правил: алерты остальных по этим видам не уходят никуда
export function ScopedNote({ n, onUnscope }: { n: number; onUnscope: () => void }) {
  const { t } = useI18n()
  return (
    <div className="al-note al-note-bad">
      <span>
        {t('У видов алертов ({n}) осталось старое ограничение по группам или объектам: алерты остальных по ним не уходят никуда, даже в основной чат.', { n })}
      </span>
      <button className="ghost small" onClick={onUnscope}>
        {t('Снять ограничения')}
      </button>
    </div>
  )
}

export function AlertRulesList({
  data,
  onChange,
  hint,
}: {
  data: Rules | null
  onChange: (d: Rules) => void
  hint?: string
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState<string | null>(null)

  if (!data) return <p className="muted small">{t('загрузка…')}</p>

  const setRule = (kind: string, patch: Partial<Rule>) =>
    onChange({ ...data, rules: { ...data.rules, [kind]: { ...data.rules[kind], ...patch } } })
  const scoped = scopedKinds(data)
  const scopeLabel = (r: Rule) =>
    r.scope_type === 'groups'
      ? t('только группы: {list}', { list: r.scope.join(', ') || '-' })
      : r.scope_type === 'servers'
        ? t('только серверов: {n}', { n: r.scope.length })
        : r.scope_type === 'checks'
          ? t('только мониторов: {n}', { n: r.scope.length })
          : ''

  return (
    <>
      {scoped.length > 0 && <ScopedNote n={scoped.length} onUnscope={() => onChange(unscopeRules(data))} />}
      <div className="arule-list">
        {data.kinds.map((k) => {
          const r = data.rules[k.key]
          const expanded = open === k.key
          return (
            <div key={k.key} className={`arule${r.enabled ? '' : ' arule-off'}`}>
              <div
                className="arule-head"
                role="button"
                tabIndex={0}
                onClick={() => setOpen(expanded ? null : k.key)}
                onKeyDown={(e) =>
                  (e.key === 'Enter' || e.key === ' ') && setOpen(expanded ? null : k.key)
                }
              >
                <input
                  type="checkbox"
                  className="arule-check"
                  checked={r.enabled}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => setRule(k.key, { enabled: e.target.checked })}
                />
                <span className="arule-name">{k.label}</span>
                <span className={`arule-sum small${r.enabled && r.scope_type !== 'all' ? ' t-degraded' : ' muted'}`}>
                  {r.enabled ? scopeLabel(r) : t('выключен')}
                </span>
                <span className="arule-caret">{expanded ? '▾' : '▸'}</span>
              </div>

              {expanded && r.enabled && (
                <div className="arule-body">
                  <label className="field">
                    <span className="muted small">{t('Текст сообщения')}</span>
                    <input
                      className="rule-text"
                      value={r.text}
                      placeholder={k.default_text}
                      onChange={(e) => setRule(k.key, { text: e.target.value })}
                    />
                  </label>
                </div>
              )}
            </div>
          )
        })}
      </div>
      {hint && <p className="muted small">{hint}</p>}
    </>
  )
}
