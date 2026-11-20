import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError, getAlertDeliveries, retryAlertDeliveries, reviewAlertDeliveries,
  type AlertDeliveryFilter, type AlertDeliveryItem, type AlertDeliveryOverview, type AlertDeliveryState,
} from './api'
import { useI18n } from './i18n'
import { kindLabel } from './mutes'

const LABELS: Record<AlertDeliveryState, string> = {
  pending: 'В очереди', sending: 'Отправляется', confirmed: 'Доставка подтверждена',
  uncertain: 'Доставка не подтверждена', rejected: 'Отклонено', superseded: 'Включено в восстановление',
  attention: 'Требует внимания',
}

export function AlertDeliveryStatus({ onUnauthorized, onCount }: {
  onUnauthorized: () => void; onCount?: (count: number) => void
}) {
  const { t, lang } = useI18n()
  const [data, setData] = useState<AlertDeliveryOverview | null>(null)
  const [state, setState] = useState<AlertDeliveryFilter>('problems')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState<{ id: number; action: 'retry' | 'review' } | null>(null)
  const [confirm, setConfirm] = useState<number | null>(null)
  const generation = useRef(0)
  const invalidate = useCallback(() => { generation.current++ }, [])
  const fail = useCallback((error: unknown) => {
    if (error instanceof ApiError && error.status === 401) return onUnauthorized()
    setError(error instanceof Error ? error.message : t('Ошибка'))
  }, [onUnauthorized, t])
  const load = useCallback(async () => {
    const current = ++generation.current
    try {
      const result = await getAlertDeliveries(state)
      if (current !== generation.current) return
      setData(result)
      setError('')
      onCount?.(result.bad_count)
    } catch (error) {
      if (current === generation.current) fail(error)
    }
  }, [state, onCount, fail])
  useEffect(() => {
    queueMicrotask(() => void load())
    const interval = window.setInterval(() => void load(), 30_000)
    return () => { window.clearInterval(interval); invalidate() }
  }, [load, invalidate])
  const when = (value: string) => new Date(value).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-GB')
  async function retry(item: AlertDeliveryItem) {
    if (item.needs_confirmation && confirm !== item.id) {
      setConfirm(item.id)
      return
    }
    setBusy({ id: item.id, action: 'retry' })
    setNotice('')
    setError('')
    try {
      const result = await retryAlertDeliveries([item.id], item.needs_confirmation)
      setNotice(result.queued_ids.length ? t('Повтор поставлен в очередь. Доставка еще не подтверждена.') :
        t('Состояние уже изменилось, повтор не поставлен.'))
      setConfirm(null)
      await load()
    } catch (error) {
      fail(error)
    } finally {
      setBusy(null)
    }
  }
  async function review(item: AlertDeliveryItem) {
    setBusy({ id: item.id, action: 'review' })
    setConfirm(null)
    setNotice('')
    setError('')
    try {
      const result = await reviewAlertDeliveries([item.id])
      setNotice(result.reviewed_ids.length ? t('Рассмотрение записано.') : t('Запись уже изменилась.'))
      await load()
    } catch (error) {
      fail(error)
    } finally {
      setBusy(null)
    }
  }
  return <div>
    <p className="muted small">{t('Очередь хранится после перезапуска. Неподтвержденная попытка не означает доставку. Повторы ограничены, после исчерпания попыток требуется решение администратора.')}</p>
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
      <button className="ghost small" aria-pressed={state === 'problems'} onClick={() => setState('problems')}>{t('Проблемы доставки')}{data ? ` (${data.bad_count})` : ''}</button>
      <button className="ghost small" aria-pressed={state === 'all'} onClick={() => setState('all')}>{t('Все доставки')}</button>
      <button className="ghost small" aria-pressed={state === 'reviewed'} onClick={() => setState('reviewed')}>{t('Рассмотренные доставки')}</button>
      <button className="ghost small" onClick={() => void load()}>{t('Обновить')}</button>
    </div>
    {error && <p className="form-error small" role="alert">{error}</p>}
    {notice && <p className="small" role="status">{notice}</p>}
    {!data && !error && <p className="muted small">{t('загрузка…')}</p>}
    {data && <>
      <p className="muted small">{t('В очереди: {pending}, подтверждено: {confirmed}', { pending: data.counts.pending, confirmed: data.counts.confirmed })}</p>
      <p className="muted small">{t('Закрытые доставки хранятся 90 дней, требующие решения - до 365 дней, тревоги открытых инцидентов - пока инцидент открыт. Счетчики считают только хранимые записи.')}</p>
      {!data.items.length && <p className="muted small">{t(state === 'problems' ? 'Нет проблем с доставкой' :
        state === 'reviewed' ? 'Рассмотренных доставок пока нет' : 'Доставок пока нет')}</p>}
      {data.items.map(item => <div className="card" key={item.id} style={{ padding: 12, marginTop: 10, overflowWrap: 'anywhere' }}>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between', flexWrap: 'wrap' }}>
          <b>{item.target || t('Общий алерт')}</b>
          <span className="small" style={!item.reviewed_at && ['attention', 'rejected', 'uncertain'].includes(item.status) ? { color: 'var(--degraded)' } : undefined}>
            {t(item.reviewed_at ? (item.uncertain_count ? 'Доставка не подтверждена' : 'Автоматические повторы остановлены') : LABELS[item.status])}
          </span>
        </div>
        <p className="muted small">{kindLabel(item.kind, t)} · {t(item.phase === 'recovery' ? 'Отбой' : 'Срабатывание')} · {when(item.occurred_at)}</p>
        <p className="muted small">{t('Попыток: {attempts}, без подтверждения: {uncertain}', { attempts: item.attempts, uncertain: item.uncertain_count })}</p>
        {['pending', 'uncertain'].includes(item.status) && <p className="muted small">{t('Следующая попытка не раньше {time}', { time: when(item.next_attempt_at) })}</p>}
        {item.reviewed_at && <p className="muted small">{t('Рассмотрено пользователем {user} · {time}', { user: item.reviewed_by || '—', time: when(item.reviewed_at) })}</p>}
        {item.status === 'attention' && !item.reviewed_at && <p className="small">{t('Автоматические повторы остановлены. Проверьте канал доставки перед ручным повтором.')}</p>}
        {item.can_retry && <>
          {confirm === item.id && <p className="small" style={{ color: 'var(--degraded)' }} role="alert">{t('Предыдущая доставка не подтверждена, повтор может прислать копию')}</p>}
          <button className="ghost small" disabled={busy !== null} onClick={() => void retry(item)}>
            {busy?.id === item.id && busy.action === 'retry' ? t('…') : t(confirm === item.id ? 'Поставить повтор в очередь' : 'Повторить доставку')}
          </button>
          {confirm === item.id && <button className="ghost small" onClick={() => setConfirm(null)}>{t('Отмена')}</button>}
        </>}
        {item.can_review && <button className="ghost small" disabled={busy !== null} onClick={() => void review(item)}>
          {busy?.id === item.id && busy.action === 'review' ? t('…') : t('Отметить рассмотренным')}
        </button>}
      </div>)}
      {data.more && <p className="muted small">{t('Показаны последние 50 записей выбранного списка.')}</p>}
    </>}
  </div>
}
