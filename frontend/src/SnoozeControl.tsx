import { useI18n } from './i18n'

// Быстрое приглушение алертов сущности (монитор/сервер) на 1ч/1д/1нед — без
// захода в настройки. Если уже заглушено — показывает «до …» и кнопку «Снять».
export function SnoozeControl({
  snoozeUntil,
  onSnooze,
  busy,
}: {
  snoozeUntil: string | null
  onSnooze: (hours: number) => void
  busy?: boolean
}) {
  const { t } = useI18n()
  const until = snoozeUntil ? new Date(snoozeUntil) : null
  const active = until != null && until.getTime() > Date.now()

  if (active) {
    const hh = until!.toLocaleString([], {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
    return (
      <div className="snooze-ctl snooze-on">
        <span>🔕 {t('алерты заглушены до {t}', { t: hh })}</span>
        <button className="ghost" disabled={busy} onClick={() => onSnooze(0)}>
          {t('Снять')}
        </button>
      </div>
    )
  }
  return (
    <div className="snooze-ctl">
      <span className="muted small">🔕 {t('Заглушить алерты:')}</span>
      <button className="ghost" disabled={busy} onClick={() => onSnooze(1)}>
        {t('1 час')}
      </button>
      <button className="ghost" disabled={busy} onClick={() => onSnooze(24)}>
        {t('1 день')}
      </button>
      <button className="ghost" disabled={busy} onClick={() => onSnooze(24 * 7)}>
        {t('1 неделя')}
      </button>
    </div>
  )
}
