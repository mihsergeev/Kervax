import { useState } from 'react'
import { updateServer, type Server } from './api'
import { useI18n } from './i18n'

export function WindowsServicesCard({ server, isViewer, onChanged }: { server: Server; isViewer: boolean; onChanged: () => void }) {
  const { t } = useI18n()
  const [selected, setSelected] = useState<string[]>(server.windows_service_names ?? [])
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const chosen = new Set(selected.map((n) => n.toLowerCase()))
  const inventory = server.windows_services
  const items = [...(inventory?.items ?? [])].sort((a, b) => Number(chosen.has(b.name.toLowerCase())) - Number(chosen.has(a.name.toLowerCase())) || a.name.localeCompare(b.name))
  const filtered = items.filter((r) => `${r.name} ${r.display_name}`.toLowerCase().includes(query.toLowerCase()))
  const changed = JSON.stringify([...chosen].sort()) !== JSON.stringify((server.windows_service_names ?? []).map((n) => n.toLowerCase()).sort())
  const save = async () => {
    setBusy(true); setError('')
    try { await updateServer(server.id, { windows_service_names: selected }); onChanged() }
    catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }
  const stateLabel = (state: string) => ({ Running: 'Работает', Stopped: 'Остановлена', Paused: 'Приостановлена', 'Start Pending': 'Запускается', 'Stop Pending': 'Останавливается', 'Pause Pending': 'Приостанавливается', 'Continue Pending': 'Возобновляется' }[state] ?? 'Нет данных')
  return <div className="chart-card net-card net-card-wide" id="mcard-units">
    <h3>{t('Службы Windows')}</h3>
    <p className="small muted">{t('Выберите службы для контроля. Панель только читает их состояние. Один алерт при остановке, один отбой после подтвержденного запуска всех выбранных служб.')}</p>
    {!inventory?.complete && <p className="small muted">{t('Нет полного свежего списка служб. Обновите сборщик Windows и дождитесь отчета. Пропавшие данные не означают восстановление.')}</p>}
    <label className="field"><span>{t('Найти службу')}</span><input value={query} onChange={(e) => setQuery(e.target.value)} /></label>
    <p className="small">{t('Выбрано служб: {n}', { n: selected.length })}</p>
    <div style={{ maxHeight: '26rem', overflowY: 'auto' }}>
      {filtered.map((r) => <label className="windows-service-row" key={r.name.toLowerCase()}>
        <input type="checkbox" checked={chosen.has(r.name.toLowerCase())} disabled={isViewer || busy} onChange={(e) => setSelected((old) => e.target.checked ? [...old, r.name] : old.filter((n) => n.toLowerCase() !== r.name.toLowerCase()))} />
        <span className="windows-service-name"><strong>{r.name}</strong><br /><span className="small muted">{r.display_name}</span></span>
        <span className={`windows-service-state small ${inventory?.complete && (r.state === 'Stopped' || r.state === 'Paused') && chosen.has(r.name.toLowerCase()) ? 'form-error' : 'muted'}`}>{t(inventory?.complete ? stateLabel(r.state) : 'Нет данных')}</span>
      </label>)}
    </div>
    {!items.length && <p className="muted small">{t('Список служб еще не получен.')}</p>}
    {error && <p className="form-error">{error}</p>}
    {!isViewer && <button type="button" disabled={busy || !changed} onClick={save}>{t('Сохранить')}</button>}
  </div>
}
