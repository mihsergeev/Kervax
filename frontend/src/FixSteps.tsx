import { useState } from 'react'
import { useI18n } from './i18n'

// Готовые шаги "как исправить": текст словами и команда с кнопкой "Копировать". Команды собирает
// бэкенд под ноду, тексты - у вызывающего по id шага (обновления ОС, версии ПО, SSH и доступы,
// старый образ прокси, pg_stat_statements). alt - другой путь вместо шагов выше (черта "или"),
// undo - откат, выполнять не по порядку, а только если что-то не так (черта "откат")
export type FixStep = { id: string; cmd?: string; alt?: boolean; undo?: boolean }

export function FixSteps<S extends FixStep>({ steps, text, foot }: { steps: S[]; text: (st: S) => string; foot?: string }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(-1)
  return (
    <div className="osupd-fix">
      {steps.map((st, i) => (
        <div key={st.id + i} className="osupd-step">
          {st.alt && <div className="osupd-alt">{t('или')}</div>}
          {st.undo && <div className="osupd-alt">{t('откат')}</div>}
          <div className="muted small">{text(st)}</div>
          {st.cmd && (
            <div className="agent-advice-cmd">
              <pre>{st.cmd}</pre>
              <button className="ghost" onClick={() => {
                navigator.clipboard?.writeText(st.cmd ?? '')
                setCopied(i)
                window.setTimeout(() => setCopied(-1), 1500)
              }}>
                {copied === i ? t('Скопировано') : t('Копировать')}
              </button>
            </div>
          )}
        </div>
      ))}
      {foot && <div className="muted small">{foot}</div>}
    </div>
  )
}
