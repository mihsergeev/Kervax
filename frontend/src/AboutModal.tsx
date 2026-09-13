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
import { analyzeLogo, lightenSvg, needsPlate, needsPlateLight, svgDataUrl } from './BrandLogo'
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
type Analysis = { transparentEdges: boolean; dark: boolean }

function BrandingSection({ onChanged }: { onChanged: () => void }) {
  const { t } = useI18n()
  const [st, setSt] = useState<Branding | null>(null)
  const [open, setOpen] = useState(false)
  // основной логотип: новый файл (data-URL), его разбор и текст, если это SVG
  const [data, setData] = useState('')
  const [main, setMain] = useState<Analysis | null>(null)
  const [mainSvg, setMainSvg] = useState<string | null>(null)
  // вариант для тёмной темы
  const [dark, setDark] = useState('')
  const [darkA, setDarkA] = useState<Analysis | null>(null)
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

  // Сохранённый логотип разбираем заново при открытии: превью и подсказки должны
  // знать, нужна ли плашка на каждой из тем, а у старых заливок светлая тема не
  // размечена. Текст SVG нужен для кнопки «светлый вариант».
  const storedVersion = st?.logo ? st.version : 0
  useEffect(() => {
    if (!open || !storedVersion || data) return
    let alive = true
    const url = brandingLogoUrl(storedVersion)
    analyzeLogo(url).then((a) => alive && setMain(a)).catch(() => undefined)
    fetch(url)
      .then(async (r) =>
        r.ok && (r.headers.get('content-type') ?? '').startsWith('image/svg') ? r.text() : null,
      )
      .then((txt) => alive && setMainSvg(txt))
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [open, storedVersion, data])

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
    const svg =
      file.type === 'image/svg+xml' || /\.svg$/i.test(file.name)
        ? await file.text().catch(() => null)
        : null
    return { url, svg, analysis: await analyzeLogo(url) }
  }

  const pickMain = async (file: File | undefined) => {
    const got = await readFile(file)
    if (!got) return
    setData(got.url)
    setMain(got.analysis)
    setMainSvg(got.svg)
  }

  const pickDark = async (file: File | undefined) => {
    const got = await readFile(file)
    if (!got) return
    setDark(got.url)
    setDarkA(got.analysis)
    setDarkRemove(false)
  }

  const makeLight = async () => {
    setErr('')
    const out = mainSvg ? lightenSvg(mainSvg) : null
    if (!out) {
      setErr(t('SVG не удалось перекрасить — загрузите светлый вариант файлом.'))
      return
    }
    const url = svgDataUrl(out)
    setDark(url)
    setDarkA(await analyzeLogo(url))
    setDarkRemove(false)
  }

  // что решил разбор; пока разбора нет — то, что сохранено
  const autoDark = main ? needsPlate(main) : !!st?.plate_auto
  const autoLight = main ? needsPlateLight(main) : !!st?.plate_light
  const hasDark = !!dark || (!!st?.dark_logo && !darkRemove)
  const autoDarkVariant = darkA ? needsPlateLight(darkA) : !!st?.dark_plate
  const on = (auto: boolean) => plate === 'always' || (plate === 'auto' && auto)

  const save = async () => {
    setBusy(true)
    setErr('')
    setNote('')
    try {
      const b = await putBranding({
        data: data || undefined,
        plate,
        plate_auto: autoDark,
        plate_light: main ? autoLight : (st?.plate_light ?? null),
        title: title.trim(),
        dark: dark || undefined,
        dark_plate: autoDarkVariant,
        dark_remove: darkRemove && !dark,
      })
      setSt(b)
      setData('')
      setDark('')
      setDarkA(null)
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
      setMain(null)
      setMainSvg(null)
      setDark('')
      setDarkA(null)
      setDarkRemove(false)
      setNote(t('Вернули стандартный логотип'))
      onChanged()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const src = data || (st?.logo ? brandingLogoUrl(st.version) : '')
  const darkSrc =
    dark || (st?.dark_logo && !darkRemove && st ? brandingLogoUrl(st.version, true) : src)
  const previews = [
    {
      theme: 'dark' as const,
      src: darkSrc,
      plate: on(hasDark ? autoDarkVariant : autoDark),
      label: t('тёмная тема'),
    },
    { theme: 'light' as const, src, plate: on(autoLight), label: t('светлая тема') },
  ]
  // светлый вариант из SVG предлагаем, когда без него на тёмной теме нужна плашка
  const canLighten = !!mainSvg && !hasDark && !!main && main.transparentEdges && main.dark

  const hints: string[] = []
  if (src && plate === 'auto') {
    if (hasDark) hints.push(t('На тёмной теме — отдельный вариант логотипа.'))
    else if (main && !main.transparentEdges)
      hints.push(t('У логотипа свой фон — на тёмной теме он показан на плашке.'))
    else if (autoDark)
      hints.push(
        t('Логотип тёмный: на тёмной теме без плашки его не видно. Загрузите светлый вариант — и плашка не понадобится.'),
      )
    else hints.push(t('Фон прозрачный, логотип светлый — плашка не нужна.'))
    hints.push(
      autoLight
        ? t('У логотипа свой фон — на светлой теме он тоже на плашке.')
        : t('На светлой теме логотип показан как есть.'),
    )
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

          {src && (
            <>
              <div className="brand-preview-row">
                {previews.map((pv) => (
                  <div key={pv.theme} className={`brand-preview brand-preview-${pv.theme}`}>
                    <div className="brand-preview-lbl muted small">{pv.label}</div>
                    <span className={`brand-custom${pv.plate ? ' brand-plate' : ''}`}>
                      <img src={pv.src} alt="" style={{ height: 38 }} />
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
              </label>
              {(canLighten || hasDark) && (
                <div className="brand-dark-actions">
                  {canLighten && (
                    <button className="ghost small" onClick={makeLight}>
                      {t('Сделать светлый вариант из SVG')}
                    </button>
                  )}
                  {hasDark && (
                    <button
                      className="ghost small"
                      onClick={() => {
                        setDark('')
                        setDarkA(null)
                        setDarkRemove(true)
                      }}
                    >
                      {t('Убрать вариант для тёмной темы')}
                    </button>
                  )}
                </div>
              )}
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
            {/* файл заново выбирать не нужно: у залитого логотипа можно поменять
                подложку, подпись и вариант для тёмной темы */}
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
