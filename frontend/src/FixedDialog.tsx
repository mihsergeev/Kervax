import { useState } from 'react'
import { ApiError } from './api'
import { useI18n } from './i18n'

// Виды, у которых есть "Починили" (backend app/fixed.py KINDS, тест сверяет): счет по окну
// (перезапуски, OOM, 5xx, логи, упавшие Job) начинается с отметки, состояние (диск, юниты, поды)
// закрывается, только если его уже нет. Недоступность, сроки, бэкапы и доступы - без кнопки
export const FIX_KINDS = new Set([
  'docker_restarts', 'kube_restarts', 'oom', 'unit_loop', 'docker_loop', 'web_5xx', 'log_errors', 'kube_job',
  'disk_health',
  'cpu', 'mem', 'temp', 'throttle', 'conntrack', 'db_conn', 'disktemp', 'disk', 'inode', 'pressure',
  'net_health', 'link', 'disk_slow', 'dns', 'clock', 'units', 'kube_pod', 'kube_node', 'flux_down',
  'flux_stale', 'kube_cron', 'listen_gone', 'cron_stuck', 'queue_stuck', 'queue_failed', 'workers', 'fpm',
  'mem_limit', 'gpu', 'gpu_heat', 'llm', 'cpu_spin', 'db_health', 'web_config', 'gre_down', 'autoban',
  'vpn_torrent', 'vpn_heavy',
])

// начало отказа бэкенда (app/fixed.py NOT_FIXED); английская панель переводит его, когда переводится вся строка
const NOT_FIXED = 'Еще не починено: '

// Починили: алерт закрывается сейчас, отбой уходит с тем, кто отметил. Что сделали - по желанию.
// Беда еще видна - окно говорит, что именно, и предлагает отметить ее известной
export function FixedDialog({
  title,
  onSave,
  onKnown,
  onClose,
}: {
  title: string
  onSave: (text: string) => Promise<void>
  onKnown?: () => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [still, setStill] = useState('')
  const save = async () => {
    setBusy(true)
    setErr('')
    setStill('')
    try {
      await onSave(text.trim())
    } catch (e) {
      const msg = String((e as Error).message || e)
      const head = [NOT_FIXED, t(NOT_FIXED)].find((h) => msg.startsWith(h))
      if (e instanceof ApiError && e.status === 409 && head) setStill(msg.slice(head.length))
      else setErr(msg)
      setBusy(false)
    }
  }
  return (
    <div className="modal-backdrop">
      <div className="card known-dialog" onClick={(e) => e.stopPropagation()}>
        <h3>✅ {t('Починили')}</h3>
        <div className="muted small">{title}</div>
        <textarea
          rows={2}
          maxLength={500}
          autoFocus
          value={text}
          placeholder={t('Что сделали (необязательно)')}
          onChange={(e) => setText(e.target.value)}
        />
        <div className="muted small">
          {t('Алерт закроется сейчас, счет начнется заново. Повторится - алерт придет как обычно.')}
        </div>
        {still && (
          <div className="fixed-still small">
            <b>{t('Еще не починено')}:</b> {still}
            {onKnown && (
              <div className="muted">{t('Панель все еще это видит. Если чинить будут позже, отметьте известной проблемой.')}</div>
            )}
          </div>
        )}
        <div className="note-bar-actions">
          <button disabled={busy} onClick={save}>
            {t('Починили')}
          </button>
          {still && onKnown && (
            <button className="ghost" onClick={onKnown}>
              📌 {t('Известная проблема')}
            </button>
          )}
          <button className="ghost" disabled={busy && !still} onClick={onClose}>
            {t('Отмена')}
          </button>
        </div>
        {err && <div className="muted small">{err}</div>}
      </div>
    </div>
  )
}
