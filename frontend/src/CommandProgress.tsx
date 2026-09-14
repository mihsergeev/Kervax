import { useEffect, useState } from 'react'
import { useI18n } from './i18n'

// Ход команды на ноде: отправили → сервер выполняет → отчёт сервера подтвердил.
//
// Команда идёт через агента и root-helper, и на крупной базе пробный дамп занимает десятки
// секунд. Раньше всё это время под кнопкой висела одна неподвижная фраза, и было непонятно,
// делается что-то или уже нет, ждать или нажимать снова. Этапы здесь настоящие: их двигают
// статусы команды и появление результата в отчёте. Время внутри этапа неизвестно, поэтому
// полоса заполняется плавно и замедляется к концу этапа, а до конца доходит только тогда,
// когда этап действительно завершён.
export type CmdPhase = 'sent' | 'running' | 'confirm' | 'done' | 'error'

export type CmdProgressState = {
  phase: CmdPhase
  since: number // начало всей операции, мс
  phaseSince: number // начало текущего этапа, мс
}

export function nextPhase(p: CmdProgressState, phase: CmdPhase): CmdProgressState {
  return p.phase === phase ? p : { ...p, phase, phaseSince: Date.now() }
}

export function CommandProgress({ state, runningText, expectSec = 20 }: {
  state: CmdProgressState
  runningText: string // что сервер делает сейчас: «проверяю доступ к базе»
  expectSec?: number // типичная длительность этапа «выполняет»
}) {
  const { t } = useI18n()
  const active = state.phase !== 'done' && state.phase !== 'error'
  const [, tick] = useState(0)
  useEffect(() => {
    if (!active) return
    const id = window.setInterval(() => tick((x) => x + 1), 250)
    return () => window.clearInterval(id)
  }, [active])

  const now = Date.now()
  const inPhase = Math.max(0, (now - state.phaseSince) / 1000)
  const total = Math.max(0, Math.round((now - state.since) / 1000))
  // экспонента: быстро в начале, у конца этапа почти замирает, но не останавливается
  const ease = (sec: number, tau: number) => 1 - Math.exp(-sec / tau)
  const pct =
    state.phase === 'sent' ? 3 + 12 * ease(inPhase, 2)
      : state.phase === 'running' ? 15 + 70 * ease(inPhase, expectSec)
        : state.phase === 'confirm' ? 85 + 13 * ease(inPhase, 12)
          : 100

  const steps: { key: CmdPhase; label: string }[] = [
    { key: 'sent', label: t('Команда отправлена на сервер') },
    { key: 'running', label: runningText },
    { key: 'confirm', label: t('Подтверждение в отчёте сервера') },
  ]
  const order: CmdPhase[] = ['sent', 'running', 'confirm', 'done']
  const at = state.phase === 'error' ? -1 : order.indexOf(state.phase)
  return (
    <div className={`cmd-progress cmd-progress-${state.phase}`} role="status" aria-live="polite">
      <div className="cmd-progress-bar">
        <span style={{ width: `${pct.toFixed(1)}%` }} />
      </div>
      <div className="cmd-progress-steps">
        {steps.map((s, i) => {
          const done = at > i
          const cur = at === i
          return (
            <span key={s.key} className={`cmd-step${done ? ' cmd-step-done' : ''}${cur ? ' cmd-step-cur' : ''}`}>
              <span className="cmd-step-ic">{done ? '✓' : cur ? <span className="run-spin" /> : '○'}</span>
              {s.label}
            </span>
          )
        })}
        {active && <span className="cmd-progress-time muted">{t('{n} с', { n: total })}</span>}
      </div>
    </div>
  )
}
