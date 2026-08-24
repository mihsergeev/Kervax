import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  exportBackup,
  getBackupConfig,
  getBackupFile,
  listBackups,
  putBackupConfig,
  restoreBackup,
  runBackup,
  type BackupFileInfo,
} from './api'
import { useI18n } from './i18n'
import { byteUnits } from './units'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

function saveJson(data: unknown, name: string) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
}

function fmtSize(n: number): string {
  const [b, kb, mb] = byteUnits()
  if (n < 1024) return `${n} ${b}`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} ${kb}`
  return `${(n / 1024 / 1024).toFixed(1)} ${mb}`
}

export function BackupModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [interval, setIntervalH] = useState(24)
  const [keep, setKeep] = useState(14)
  const [files, setFiles] = useState<BackupFileInfo[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onUnauthorized()
      setErr(e instanceof Error ? e.message : t('Ошибка'))
    },
    [onUnauthorized, t],
  )

  const loadFiles = useCallback(() => {
    listBackups().then(setFiles).catch(fail)
  }, [fail])

  useEffect(() => {
    getBackupConfig()
      .then((c) => {
        setIntervalH(c.interval_hours)
        setKeep(c.keep)
      })
      .catch(fail)
    loadFiles()
  }, [fail, loadFiles])

  async function saveConfig() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const c = await putBackupConfig({ interval_hours: interval, keep })
      setIntervalH(c.interval_hours)
      setKeep(c.keep)
      setMsg(t('Сохранено.'))
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  async function download() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      const data = await exportBackup()
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
      saveJson(data, `kervax-backup-${stamp}.json`)
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  async function serverBackup() {
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      await runBackup()
      loadFiles()
      setMsg(t('Бэкап создан на сервере.'))
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  async function downloadFile(name: string) {
    try {
      const data = await getBackupFile(name)
      saveJson(data, name)
    } catch (e) {
      fail(e)
    }
  }

  async function onRestoreFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (fileRef.current) fileRef.current.value = ''
    if (!file) return
    setErr(null)
    setMsg(null)
    let data: unknown
    try {
      data = JSON.parse(await file.text())
    } catch {
      setErr(t('Файл не является корректным JSON.'))
      return
    }
    if (
      !window.confirm(
        t('Восстановить из «{name}»? Текущие мониторы, серверы и настройки будут заменены. Учётные записи в копию не входят и не восстановятся.', {
          name: file.name,
        }),
      )
    )
      return
    setBusy(true)
    try {
      const r = await restoreBackup(data)
      const n = Object.values(r.restored).reduce((a, b) => a + b, 0)
      setMsg(t('Восстановлено записей: {n}. Обновляю…', { n }))
      setTimeout(() => window.location.reload(), 1200)
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Бэкап')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <p className="muted small">
          {t('Бэкап содержит мониторы, серверы, локации и настройки — без метрик (тайм-серий) и без учётных записей: пользователей, их роли и доступы после восстановления придётся завести заново.')}
        </p>

        <div className="settings-group">
          <div className="modal-actions" style={{ justifyContent: 'flex-start' }}>
            <button onClick={download} disabled={busy}>
              {t('Скачать бэкап')}
            </button>
            <button className="ghost" onClick={() => fileRef.current?.click()} disabled={busy}>
              {t('Восстановить из файла')}
            </button>
            <input
              ref={fileRef}
              type="file"
              accept="application/json,.json"
              hidden
              onChange={onRestoreFile}
            />
          </div>
        </div>

        <div className="settings-group">
          <h4>{t('Автобэкап на сервере')}</h4>
          <p className="muted small">
            {t('Панель сама сохраняет бэкапы на диск. Интервал 0 = выключить.')}
          </p>
          <div className="form-row">
            <label className="field">
              <span>{t('Раз в, часов')}</span>
              <input
                type="number"
                min={0}
                max={720}
                value={interval}
                onChange={(e) => setIntervalH(e.target.value === '' ? 0 : Number(e.target.value))}
              />
            </label>
            <label className="field">
              <span>{t('Хранить файлов')}</span>
              <input
                type="number"
                min={1}
                max={365}
                value={keep}
                onChange={(e) => setKeep(e.target.value === '' ? 1 : Number(e.target.value))}
              />
            </label>
          </div>
          <div className="modal-actions" style={{ justifyContent: 'flex-start' }}>
            <button onClick={saveConfig} disabled={busy}>
              {t('Сохранить')}
            </button>
            <button className="ghost" onClick={serverBackup} disabled={busy}>
              {t('Создать сейчас')}
            </button>
          </div>
        </div>

        <div className="settings-group">
          <h4>{t('Бэкапы на сервере')}</h4>
          {files == null ? (
            <p className="muted small">{t('загрузка…')}</p>
          ) : files.length === 0 ? (
            <p className="muted small">{t('Пока нет файлов.')}</p>
          ) : (
            <div className="loc-results">
              {files.map((f) => (
                <div key={f.name} className="loc-res">
                  <div className="loc-res-name mono">{f.name.replace('kervax-backup-', '').replace('.json', '')}</div>
                  <div className="loc-res-metric muted small">{fmtSize(f.size)}</div>
                  <div className="loc-res-msg" style={{ textAlign: 'right' }}>
                    <button className="ghost small" onClick={() => downloadFile(f.name)}>
                      {t('Скачать')}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {err && <p className="form-error">{err}</p>}
        {msg && <p className="form-ok">{msg}</p>}
      </div>
    </div>
  )
}
