import { useEffect, useState } from 'react'
import {
  ApiError,
  brandingLogoUrl,
  deleteBranding,
  getBranding,
  getHealth,
  putBranding,
  type Branding,
} from './api'
import { analyzeLogo, needsPlate } from './BrandLogo'
import { BrandMark } from './Logo'
import { useAuth } from './auth'
import { useI18n } from './i18n'

export const KERVAX_URL = 'https://github.com/mihsergeev/Kervax'
const MAX_KB = 512

// «О панели» из меню: что это, куда идти за исходниками — и здесь же админ меняет
// логотип. Отдельным пунктом меню настройка логотипа выглядела как что-то важное
// и частое, хотя её трогают один раз за всё время жизни установки.
export function AboutModal({ onClose, onBrandingChanged }: {
  onClose: () => void
  onBrandingChanged: () => void
}) {
  const { t } = useI18n()
  const { isAdmin } = useAuth()
  // версия — с бэкенда: см. getHealth в api.ts
  const [version, setVersion] = useState('')
  useEffect(() => {
    getHealth().then((h) => setVersion(h.version)).catch(() => setVersion(''))
  }, [])
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal about-modal" onClick={(e) => e.stopPropagation()}>
        <div className="clients-head">
          <h3>{t('О панели Kervax')}</h3>
          <button className="ghost" onClick={onClose}>{t('Закрыть')}</button>
        </div>

        <div className="about-head">
          <BrandMark height={48} />
          <div>
            <div className="about-name">Kervax</div>
            <div className="muted small">
              {version ? `${t('версия')} ${version} · ` : ''}AGPL-3.0
            </div>
          </div>
        </div>

        <p className="about-lead">
          {t('Следит за сайтами, серверами, Docker, Kubernetes и бэкапами — и пишет в Telegram, когда что-то ломается.')}
        </p>

        <div className="about-links">
          <a href={KERVAX_URL} target="_blank" rel="noopener noreferrer">
            {t('Исходный код на GitHub')}
          </a>
          <a href={`${KERVAX_URL}/issues`} target="_blank" rel="noopener noreferrer">
            {t('Сообщить о проблеме')}
          </a>
          <a href={`${KERVAX_URL}/blob/main/LICENSE`} target="_blank" rel="noopener noreferrer">
            {t('Лицензия')}
          </a>
        </div>

        {isAdmin && <BrandingSection onChanged={onBrandingChanged} />}
      </div>
    </div>
  )
}

// Настройка логотипа. Превью сразу на обеих темах: типовая ошибка — залить
// логотип, который хорош на белом и исчезает на тёмном, а заметить это вечером,
// когда панель откроет коллега с другой темой.
function BrandingSection({ onChanged }: { onChanged: () => void }) {
  const { t } = useI18n()
  const [st, setSt] = useState<Branding | null>(null)
  const [open, setOpen] = useState(false)
  const [data, setData] = useState('')
  const [auto, setAuto] = useState(false)
  const [plate, setPlate] = useState<'auto' | 'always' | 'never'>('auto')
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [note, setNote] = useState('')

  useEffect(() => {
    getBranding()
      .then((b) => {
        setSt(b)
        setPlate(b.plate)
        setTitle(b.title)
        setAuto(b.plate_auto)
      })
      .catch(() => setSt(null))
  }, [])

  const pick = async (file: File | undefined) => {
    setErr('')
    setNote('')
    if (!file) return
    if (file.size > MAX_KB * 1024) {
      setErr(t('Файл больше {n} КБ — уменьшите логотип', { n: MAX_KB }))
      return
    }
    const url = await new Promise<string>((res, rej) => {
      const fr = new FileReader()
      fr.onload = () => res(String(fr.result))
      fr.onerror = () => rej(fr.error)
      fr.readAsDataURL(file)
    }).catch(() => '')
    if (!url) {
      setErr(t('Не удалось прочитать файл'))
      return
    }
    setData(url)
    setAuto(needsPlate(await analyzeLogo(url)))
  }

  const save = async () => {
    setBusy(true)
    setErr('')
    setNote('')
    try {
      const b = await putBranding({ data, plate, plate_auto: auto, title: title.trim() })
      setSt(b)
      setData('')
      setNote(t('Логотип сохранён'))
      onChanged()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const clear = async () => {
    setBusy(true)
    setErr('')
    try {
      const b = await deleteBranding()
      setSt(b)
      setData('')
      setNote(t('Вернули стандартный логотип'))
      onChanged()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const src = data || (st?.logo ? brandingLogoUrl(st.version) : '')
  const withPlate = plate === 'always' || (plate === 'auto' && auto)

  return (
    <div className="about-branding">
      <button className="ghost about-brand-toggle" onClick={() => setOpen(!open)}>
        {open ? t('свернуть настройку логотипа') : t('Поставить свой логотип')}
      </button>

      {open && (
        <>
          <p className="muted small">
            {t('Заменяет логотип в шапке и на экране входа. PNG, SVG, WebP или JPEG до {n} КБ.', { n: MAX_KB })}
          </p>

          <label className="brand-file">
            {t('Файл логотипа')}
            <input
              type="file"
              accept="image/png,image/svg+xml,image/webp,image/jpeg,image/gif"
              onChange={(e) => pick(e.target.files?.[0])}
            />
          </label>

          {src && (
            <>
              <div className="brand-preview-row">
                {(['dark', 'light'] as const).map((theme) => (
                  <div key={theme} className={`brand-preview brand-preview-${theme}`}>
                    <div className="brand-preview-lbl muted small">
                      {theme === 'dark' ? t('тёмная тема') : t('светлая тема')}
                    </div>
                    <span className={`brand-custom${withPlate ? ' brand-plate' : ''}`}>
                      <img src={src} alt="" style={{ height: 38 }} />
                    </span>
                  </div>
                ))}
              </div>
              <div className="muted small">
                {auto
                  ? t('У логотипа свой фон или он тёмный — под него подложена светлая плашка.')
                  : t('Фон прозрачный, логотип светлый — плашка не нужна.')}
              </div>
            </>
          )}

          <label className="brand-row">
            {t('Подложка')}
            <select value={plate} onChange={(e) => setPlate(e.target.value as typeof plate)}>
              <option value="auto">{t('авто (по картинке)')}</option>
              <option value="always">{t('всегда')}</option>
              <option value="never">{t('никогда')}</option>
            </select>
          </label>

          <label className="brand-row">
            {t('Подпись рядом с логотипом')}
            <input
              value={title}
              maxLength={64}
              placeholder={t('например, название компании')}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>

          {err && <p className="form-error">{err}</p>}
          {note && <p className="tg-ok">{note}</p>}

          <div className="tg-actions">
            <button className="primary" disabled={busy || !data} onClick={save}>
              {busy ? t('сохраняем…') : t('Сохранить логотип')}
            </button>
            {st?.logo && (
              <button className="ghost" disabled={busy} onClick={clear}>
                {t('Вернуть стандартный')}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  )
}
