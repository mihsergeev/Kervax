import type { QdrantInfo as QdrantData } from './api'
import { useI18n } from './i18n'
import './qdrantInfo.css'

// Counts describe the responding node and may include replicas. Do not add
// them across instances, or call points vectors: a point can have several.
export function QdrantInfo({ data }: { data: QdrantData }) {
  const { t, lang } = useI18n()
  const collections = data.collections ?? []
  const count = (n: number | undefined) => n == null ? '—' : new Intl.NumberFormat(lang).format(n)
  const states: Record<string, string> = {
    green: t('Готова'), yellow: t('Индексация'), grey: t('Ожидает оптимизации'), red: t('Ошибка'),
  }
  const hint = data.state === 'auth_required'
    ? t('Метрики Qdrant закрыты авторизацией. Доступность сервера проверяется отдельно.')
    : data.state === 'unavailable'
      ? t('Метрики Qdrant недоступны. Доступность сервера проверяется отдельно.')
      : data.state === 'partial'
        ? t('Получена часть метрик Qdrant. Неизвестные значения отмечены прочерком.')
        : null
  return (
    <div className="qdrant-info">
      <div className="qdrant-summary">
        {data.version && <span>{t('Версия')} <strong>{data.version}</strong></span>}
        {data.collections_total != null && <span>{t('Коллекций')} <strong>{count(data.collections_total)}</strong></span>}
        <span className="muted">{t('Данные этого узла')}</span>
      </div>
      {hint && <p className="muted small">{hint}</p>}
      {collections.length > 0 && (
        <div className="qdrant-collections" tabIndex={0} role="region" aria-label={t('Коллекции Qdrant')}>
          <table>
            <thead><tr>
              <th>{t('Коллекция')}</th>
              <th>{t('Точек, примерно')}</th>
              <th>{t('Индексированных векторов')}</th>
              <th>{t('Состояние')}</th>
            </tr></thead>
            <tbody>{collections.map((c) => (
              <tr key={c.name}>
                <td className="mono">{c.name}</td>
                <td>{c.points_count == null ? '—' : `≈ ${count(c.points_count)}`}</td>
                <td>{count(c.indexed_vectors_count)}</td>
                <td className={c.status === 'red' || c.optimizer_status === 'error' ? 't-down' : undefined}>
                  {c.optimizer_status === 'error' ? t('Ошибка оптимизации') : (c.status ? states[c.status] : '—')}
                </td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <div className="net-foot">
        {data.truncated && <>{t('Показана часть коллекций.')} </>}
        {t('Числа точек приблизительные. Значения разных узлов могут включать реплики и не складываются.')}
      </div>
    </div>
  )
}
