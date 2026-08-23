import { useEffect, useState } from 'react'
import {
  ApiError,
  createLocation,
  deleteLocation,
  listLocations,
  testProxy,
  updateLocation,
  type Location,
  type ProxyTestResult,
} from './api'
import { useI18n } from './i18n'

type Props = {
  onClose: () => void
  onUnauthorized: () => void
}

export function LocationsModal({ onClose, onUnauthorized }: Props) {
  const { t } = useI18n()
  const [locs, setLocs] = useState<Location[]>([])
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [origUrl, setOrigUrl] = useState('') // url на момент входа в редактирование
  const [editId, setEditId] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // результат проверки прокси; сбрасывается при смене адреса → требует пере-проверки
  const [test, setTest] = useState<ProxyTestResult | null>(null)
  const [testing, setTesting] = useState(false)

  const fail = (e: unknown) => {
    if (e instanceof ApiError && e.status === 401) return onUnauthorized()
    setErr(e instanceof Error ? e.message : t('Ошибка'))
  }

  const load = () => {
    listLocations().then(setLocs).catch(fail)
  }
  useEffect(load, []) // eslint-disable-line react-hooks/exhaustive-deps

  const reset = () => {
    setName('')
    setUrl('')
    setOrigUrl('')
    setEditId(null)
    setTest(null)
  }

  const changeUrl = (v: string) => {
    setUrl(v)
    setTest(null) // адрес изменился → прошлая проверка неактуальна
  }

  async function testNow() {
    setTesting(true)
    setErr(null)
    try {
      setTest(await testProxy(url.trim()))
    } catch (e) {
      fail(e)
    } finally {
      setTesting(false)
    }
  }

  async function submit() {
    setBusy(true)
    setErr(null)
    try {
      if (editId != null) await updateLocation(editId, { name, url })
      else await createLocation({ name, url })
      reset()
      load()
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  async function toggle(loc: Location) {
    try {
      await updateLocation(loc.id, { enabled: !loc.enabled })
      load()
    } catch (e) {
      fail(e)
    }
  }

  async function remove(loc: Location) {
    if (!window.confirm(t('Удалить локацию «{name}»?', { name: loc.name }))) return
    try {
      await deleteLocation(loc.id)
      if (editId === loc.id) reset()
      load()
    } catch (e) {
      fail(e)
    }
  }

  // сохраняем прокси только если он ДОСТУПЕН: непустой адрес → нужна успешная
  // проверка. Пустой (напрямую) — можно. При редактировании неизменённый адрес
  // считаем уже проверенным (был сохранён рабочим), менять — заново проверять.
  const trimmedUrl = url.trim()
  const urlChanged = editId == null || trimmedUrl !== origUrl.trim()
  const proxyVerified =
    trimmedUrl === '' || !urlChanged || (test != null && test.ok)
  const canSubmit = name.trim() !== '' && !busy && proxyVerified
  const canTest = trimmedUrl !== '' && !testing && !busy

  return (
    <div className="modal-backdrop">
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('Локации')}</h3>
          <button className="ghost" onClick={onClose}>
            {t('Закрыть')}
          </button>
        </div>
        <p className="muted small">
          {t('Прокси, через которые панель проверяет сайты (HTTP/HTTPS/SOCKS5) — чтобы видеть доступность из разных сетей/регионов.')}
        </p>

        <div className="loc-form">
          <label className="field">
            <span>{t('Название')}</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('напр. Германия')}
            />
          </label>
          <label className="field">
            <span>{t('Адрес прокси')}</span>
            <div className="pass-row">
              <input
                value={url}
                onChange={(e) => changeUrl(e.target.value)}
                placeholder={t('socks5://… (пусто = напрямую)')}
              />
              <button className="ghost" onClick={testNow} disabled={!canTest}>
                {testing ? t('Проверяю…') : t('Проверить')}
              </button>
            </div>
          </label>
          {test != null && (
            <p className={`loc-test small ${test.ok ? 'form-ok' : 'form-error'}`}>
              {test.ok ? '✓ ' : '✗ '}
              {test.message}
            </p>
          )}
          {trimmedUrl !== '' && urlChanged && (test == null || !test.ok) && (
            <p className="muted small">
              {t('Проверьте прокси — сохранить можно только доступный.')}
            </p>
          )}
          <div className="loc-form-actions">
            {editId != null && (
              <button className="ghost" onClick={reset}>
                {t('Отмена')}
              </button>
            )}
            <button onClick={submit} disabled={!canSubmit}>
              {busy ? t('…') : editId != null ? t('Сохранить') : t('Добавить')}
            </button>
          </div>
        </div>

        {err && <p className="form-error">{err}</p>}

        {locs.length === 0 ? (
          <p className="muted small">{t('Локаций пока нет.')}</p>
        ) : (
          <div className="loc-list">
            {locs.map((loc) => (
              <div key={loc.id} className="loc-row">
                <label className="loc-toggle" title={t('Включена')}>
                  <input
                    type="checkbox"
                    checked={loc.enabled}
                    onChange={() => toggle(loc)}
                  />
                </label>
                <div className="loc-main">
                  <div className="loc-name">
                    {loc.name}
                    {!loc.enabled && <span className="type-chip off">{t('выкл')}</span>}
                  </div>
                  <div className="loc-url mono">
                    {loc.url || t('напрямую, без прокси')}
                  </div>
                </div>
                <div className="check-actions">
                  <button
                    className="ghost icon-btn"
                    onClick={() => {
                      setEditId(loc.id)
                      setName(loc.name)
                      setUrl(loc.url)
                      setOrigUrl(loc.url)
                      setTest(null)
                    }}
                    title={t('Изменить')}
                  >
                    ✎
                  </button>
                  <button
                    className="ghost icon-btn"
                    onClick={() => remove(loc)}
                    title={t('Удалить')}
                  >
                    ✕
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
