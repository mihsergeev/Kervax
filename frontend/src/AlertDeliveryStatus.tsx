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

type Translate = (s: string, v?: Record<string, string | number>) => string

// коды причин - notification_store._safe_error: что случилось и что делать оператору
function reasonGroup(code: string) {
  if (code === 'PrepareError') return 'prepare'
  if (code === 'LeaseExpired' || code === 'CancelledError') return 'interrupted'
  if (['ReadTimeout', 'ReadError', 'WriteTimeout', 'WriteError', 'RemoteProtocolError'].includes(code)) return 'no_answer'
  if (code === 'DestinationChanged') return 'destination'
  if (code === 'DeliveryError') return 'unknown'
  return 'refused'
}

function reasonCause(item: AlertDeliveryItem, t: Translate) {
  const group = reasonGroup(item.reason)
  if (group === 'prepare') return t('Причина: сообщение не удалось подготовить (текст, правила или получатель). В этот раз в канал оно не отправлялось.')
  if (group === 'interrupted') return t('Причина: отправка прервалась до записи ответа канала (перезапуск или зависание панели). Сообщение могло дойти.')
  if (group === 'no_answer') return t('Причина: канал не ответил после отправки. Сообщение могло дойти.')
  // DeliveryError + rejected - строки до кода DestinationChanged: тогда так писалась смена получателя
  if (group === 'destination' || (group === 'unknown' && item.status === 'rejected')) return t('Причина: получатель изменился в настройках до отправки. В этот раз старому получателю сообщение не отправлялось.')
  if (group === 'unknown') return t('Причина не распознана.')
  return t(item.status === 'rejected' ? 'Причина: канал отказал в приеме (например, бота нет в чате или токен отозван). Сообщение не принято.' : 'Причина: канал вернул ошибку или был недоступен. Сообщение не принято.')
}

function reasonAction(item: AlertDeliveryItem, t: Translate, prepareLimit: number) {
  const group = reasonGroup(item.reason)
  if (item.status === 'rejected') return group === 'refused' ? t('Исправьте бота или чат в настройках алертов, затем повторите доставку.') : ''
  if (item.status !== 'attention') return ''
  if (group === 'prepare') return t('Подготовка подряд не удалась несколько раз (предел {n}), автоматические повторы остановлены. Посмотрите «Ошибки панели» за это время и настройки получателя, затем повторите доставку.', { n: prepareLimit })
  if (group === 'interrupted' || group === 'no_answer') return t('Автоматические повторы остановлены. Проверьте чат: если сообщения нет, повторите доставку (может прийти копия), если есть - отметьте рассмотренным.')
  return t('Автоматические повторы остановлены. Проверьте связь панели с Telegram или вебхуком и настройки бота, затем повторите доставку.')
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
        {item.reason && item.status !== 'sending' && <p className="small">{reasonCause(item, t)}</p>}
        {item.reason && !item.reviewed_at && reasonAction(item, t, data.prepare_limit) && <p className="small">{reasonAction(item, t, data.prepare_limit)}</p>}
        {item.status === 'attention' && !item.reviewed_at && !item.reason && <p className="small">{t('Автоматические повторы остановлены. Проверьте канал доставки перед ручным повтором.')}</p>}
        {item.reason && item.status !== 'sending' && <p className="muted small">{t('Код причины: {code}', { code: item.reason })}</p>}
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
