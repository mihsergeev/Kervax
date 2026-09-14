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
import { useLogoViews } from './BrandLogo'
import type { LogoSource } from './logoAdapt'
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
// когда панель откроет коллега с другой темой. Превью считается тем же logoView,
// что и шапка: что видно здесь, то и будет в панели.
function BrandingSection({ onChanged }: { onChanged: () => void }) {
  const { t } = useI18n()
  const [st, setSt] = useState<Branding | null>(null)
  const [open, setOpen] = useState(false)
  // новый основной файл (data-URL) и его текст, если это SVG
  const [data, setData] = useState('')
  const [dataSvg, setDataSvg] = useState<string | null>(null)
  // вариант для тёмной темы
  const [dark, setDark] = useState('')
  const [darkSvg, setDarkSvg] = useState<string | null>(null)
  const [darkRemove, setDarkRemove] = useState(false)
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
      })
      .catch(() => setSt(null))
  }, [])

  const readFile = async (file: File | undefined) => {
    setErr('')
    setNote('')
    if (!file) return null
    if (file.size > MAX_KB * 1024) {
      setErr(t('Файл больше {n} КБ — уменьшите логотип', { n: MAX_KB }))
      return null
    }
    const url = await new Promise<string>((res, rej) => {
      const fr = new FileReader()
      fr.onload = () => res(String(fr.result))
      fr.onerror = () => rej(fr.error)
      fr.readAsDataURL(file)
    }).catch(() => '')
    if (!url) {
      setErr(t('Не удалось прочитать файл'))
      return null
    }
    // текст SVG — для перекраски по цветам; у растровой картинки его нет
    const svg =
      file.type === 'image/svg+xml' || /\.svg$/i.test(file.name)
        ? await file.text().catch(() => null)
        : null
    return { url, svg }
  }

  const pickMain = async (file: File | undefined) => {
    const got = await readFile(file)
    if (!got) return
    setData(got.url)
    setDataSvg(got.svg)
  }

  const pickDark = async (file: File | undefined) => {
    const got = await readFile(file)
    if (!got) return
    setDark(got.url)
    setDarkSvg(got.svg)
    setDarkRemove(false)
  }

  const storedDark = !!st?.dark_logo && !darkRemove
  const hasDark = !!dark || storedDark
  const mainSrc: LogoSource | null = data
    ? { url: data, svg: dataSvg }
    : st?.logo
      ? { url: brandingLogoUrl(st.version) }
      : null
  const darkSrc: LogoSource | null = dark
    ? { url: dark, svg: darkSvg }
    : storedDark && st
      ? { url: brandingLogoUrl(st.version, true) }
      : null
  const views = useLogoViews(mainSrc, darkSrc, plate)

  const save = async () => {
    setBusy(true)
    setErr('')
    setNote('')
    try {
      const b = await putBranding({
        data: data || undefined,
        plate,
        // флаги хранятся для совместимости; как показывать, решает перекраска при показе
        plate_auto: views.dark?.plate ?? !!st?.plate_auto,
        plate_light: views.light ? views.light.plate : (st?.plate_light ?? null),
        title: title.trim(),
        dark: dark || undefined,
        dark_plate: hasDark ? (views.dark?.plate ?? false) : false,
        dark_remove: darkRemove && !dark,
      })
      setSt(b)
      setData('')
      setDataSvg(null)
      setDark('')
      setDarkSvg(null)
      setDarkRemove(false)
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
      setDataSvg(null)
      setDark('')
      setDarkSvg(null)
      setDarkRemove(false)
      setNote(t('Вернули стандартный логотип'))
      onChanged()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const previews = [
    {
      theme: 'dark' as const,
      view: views.dark,
      fallback: (darkSrc ?? mainSrc)?.url ?? '',
      label: t('тёмная тема'),
    },
    { theme: 'light' as const, view: views.light, fallback: mainSrc?.url ?? '', label: t('светлая тема') },
  ]

  const hints: string[] = []
  if (views.dark && views.light) {
    const d = views.dark
    if (hasDark)
      hints.push(
        d.adapted
          ? t('На тёмной теме — отдельный вариант логотипа, его цвета подстроены под тему.')
          : t('На тёмной теме — отдельный вариант логотипа.'),
      )
    else if (d.adapted) hints.push(t('На тёмной теме цвета логотипа подстроены под тему.'))
    else if (d.plate) hints.push(t('На тёмной теме логотип на плашке: у картинки свой фон или её нельзя перекрасить.'))
    else hints.push(t('На тёмной теме логотип показан как есть.'))
    const l = views.light
    if (l.adapted) hints.push(t('На светлой теме цвета логотипа подстроены под тему.'))
    else if (l.plate) hints.push(t('На светлой теме логотип на плашке: у картинки свой фон.'))
    else hints.push(t('На светлой теме логотип показан как есть.'))
  }

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
              onChange={(e) => pickMain(e.target.files?.[0])}
            />
          </label>

          {mainSrc && (
            <>
              <div className="brand-preview-row">
                {previews.map((pv) => (
                  <div key={pv.theme} className={`brand-preview brand-preview-${pv.theme}`}>
                    <div className="brand-preview-lbl muted small">{pv.label}</div>
                    <span className={`brand-custom${pv.view?.plate ? ' brand-plate' : ''}`}>
                      <img
                        src={pv.view?.src ?? pv.fallback}
                        alt=""
                        style={{ height: 38, visibility: pv.view ? undefined : 'hidden' }}
                      />
                    </span>
                  </div>
                ))}
              </div>
              {hints.map((h) => (
                <div key={h} className="muted small">
                  {h}
                </div>
              ))}

              <label className="brand-file">
                {t('Вариант для тёмной темы (необязательно)')}
                <input
                  type="file"
                  accept="image/png,image/svg+xml,image/webp,image/jpeg,image/gif"
                  onChange={(e) => pickDark(e.target.files?.[0])}
                />
                <span className="muted small">
                  {t('Нужен, только если для тёмного фона у логотипа есть своя версия: цвета основного панель подстраивает под тему сама.')}
                </span>
              </label>
              {hasDark && (
                <div className="brand-dark-actions">
                  <button
                    className="ghost small"
                    onClick={() => {
                      setDark('')
                      setDarkSvg(null)
                      setDarkRemove(true)
                    }}
                  >
                    {t('Убрать вариант для тёмной темы')}
                  </button>
                </div>
              )}
            </>
          )}

          <label className="brand-row">
            {t('Оформление')}
            <select value={plate} onChange={(e) => setPlate(e.target.value as typeof plate)}>
              <option value="auto">{t('авто: подстроить цвета под тему')}</option>
              <option value="always">{t('всегда на плашке')}</option>
              <option value="never">{t('как есть')}</option>
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
            {/* файл заново выбирать не нужно: у залитого логотипа можно поменять
                оформление, подпись и вариант для тёмной темы */}
            <button className="primary" disabled={busy || !(data || st?.logo)} onClick={save}>
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
