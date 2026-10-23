import type { ReactNode } from 'react'
import { useI18n } from './i18n'

// Логи контейнеров, подов и журнал systemd учетке только для просмотра закрыты: в логах бывают
// пароли и токены. Открывает их админ галкой в окне "Пользователи" (canLogs в auth.tsx). Без
// галки кнопка логов видна, но приглушена, и подсказка говорит, к кому идти. Тот же текст
// отказа у бэкенда (LOGS_CLOSED в deps.py)
export const LOGS_LOCKED = 'Логи закрыты для этой учетки, может открыть админ'

export function LockedLogs({ className, children }: { className: string; children: ReactNode }) {
  const { t } = useI18n()
  // у выключенной кнопки браузер подсказку не показывает - она на обертке
  return (
    <span className="logs-locked" title={t(LOGS_LOCKED)}>
      <button type="button" className={className} disabled>
        {children}
      </button>
    </span>
  )
}
