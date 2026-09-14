import { useEffect, useState } from 'react'
import { BrandHorizontal, BrandLockup } from './Logo'
import { brandingLogoUrl, getBranding, type Branding } from './api'
import { logoView, type LogoMode, type LogoSource, type LogoView } from './logoAdapt'

// Свой логотип вместо стандартного.
//
// Главная сложность не в загрузке, а в том, чтобы чужая картинка не выглядела
// «наклейкой». Три типовые беды:
//   1) JPEG/PNG с непрозрачным белым фоном на тёмной теме — белый прямоугольник;
//   2) тёмный логотип на прозрачном фоне — на тёмной теме почти не виден;
//   3) любые пропорции: от узкой полоски до квадрата.
// Первую решает ПОДЛОЖКА (светлая карточка со скруглением и отступами): логотип на
// ней смотрится намеренно, а не вырезанным. Вторую — перекраска под тему (см.
// logoAdapt): плашка под тёмной надписью на тёмной теме тоже выглядела наклейкой.
// Третью — жёсткая высота и object-fit: contain, ширина ограничена сверху.

// Разбор картинки: прозрачные ли края и насколько логотип тёмный.
export async function analyzeLogo(dataUrl: string): Promise<{
  transparentEdges: boolean
  dark: boolean
}> {
  const img = new Image()
  img.src = dataUrl
  await img.decode().catch(() => undefined)
  const S = 48
  const cv = document.createElement('canvas')
  cv.width = S
  cv.height = S
  const ctx = cv.getContext('2d', { willReadFrequently: true })
  if (!ctx || !img.width) return { transparentEdges: true, dark: false }
  ctx.drawImage(img, 0, 0, S, S)
  let px: Uint8ClampedArray
  try {
    px = ctx.getImageData(0, 0, S, S).data
  } catch {
    return { transparentEdges: true, dark: false } // tainted canvas — не гадаем
  }
  let edge = 0
  let edgeOpaque = 0
  let lumaSum = 0
  let lumaCount = 0
  for (let y = 0; y < S; y++) {
    for (let x = 0; x < S; x++) {
      const i = (y * S + x) * 4
      const a = px[i + 3]
      const onEdge = x < 2 || y < 2 || x >= S - 2 || y >= S - 2
      if (onEdge) {
        edge++
        if (a > 24) edgeOpaque++
      }
      if (a > 128) {
        lumaSum += 0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2]
        lumaCount++
      }
    }
  }
  return {
    // края в основном непрозрачные → у картинки есть свой фон
    transparentEdges: edge > 0 && edgeOpaque / edge < 0.35,
    // тёмный логотип: на тёмной теме утонет без обработки
    dark: lumaCount > 0 && lumaSum / lumaCount < 110,
  }
}

// Плашка на ТЁМНОЙ теме, если перекрасить нельзя: свой фон либо сам логотип тёмный.
// Эти флаги бэкенд хранит для совместимости; показ решает logoView.
export function needsPlate(a: { transparentEdges: boolean; dark: boolean }): boolean {
  return !a.transparentEdges || a.dark
}

// На СВЕТЛОЙ теме — только картинке со своим фоном.
export function needsPlateLight(a: { transparentEdges: boolean }): boolean {
  return !a.transparentEdges
}

/** Как показать логотип на обеих темах; null — ещё считается. */
export function useLogoViews(
  main: LogoSource | null,
  dark: LogoSource | null,
  mode: LogoMode,
): { dark: LogoView | null; light: LogoView | null } {
  const [views, setViews] = useState<{ key: string; dark: LogoView; light: LogoView } | null>(null)
  const key = main ? `${mode}|${main.url}|${dark?.url ?? ''}` : ''
  useEffect(() => {
    if (!main) return
    let alive = true
    Promise.all([logoView(dark ?? main, 'dark', mode), logoView(main, 'light', mode)])
      .then(([d, l]) => alive && setViews({ key, dark: d, light: l }))
      .catch(() => undefined)
    return () => {
      alive = false
    }
    // источники меняются вместе с ключом; сами объекты пересоздаются на каждой отрисовке
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  return views && views.key === key ? views : { dark: null, light: null }
}

type Props = {
  where: 'header' | 'login'
  branding: Branding | null
}

// Логотип панели: свой, если загружен, иначе штатный Kervax.
export function BrandLogo({ where, branding }: Props) {
  const header = where === 'header'
  // Файл мог пропасть (перенесли панель без каталога data/branding, чистили диск).
  // Показывать битую картинку в шапке хуже, чем честный логотип Kervax.
  const [broken, setBroken] = useState(false)
  useEffect(() => setBroken(false), [branding?.version])
  const has = !!branding?.logo
  const main = has && branding ? { url: brandingLogoUrl(branding.version) } : null
  const dark =
    has && branding?.dark_logo ? { url: brandingLogoUrl(branding.version, true) } : null
  const views = useLogoViews(main, dark, branding?.plate ?? 'auto')

  if (!branding?.logo || broken) {
    return header ? <BrandHorizontal height={40} /> : <BrandLockup width={170} />
  }
  // Две версии, тему выбирает CSS (brand-when-dark / brand-when-light) — как у штатного
  // логотипа: переключение темы не ждёт перерисовки. Пока перекраска считается, место
  // держим пустым: мелькнуть тёмной надписью на тёмной шапке хуже, чем появиться чуть позже.
  const variants = [
    { cls: 'brand-when-dark', view: views.dark, fallback: (dark ?? main)?.url ?? '' },
    { cls: 'brand-when-light', view: views.light, fallback: main?.url ?? '' },
  ]
  return (
    <>
      {variants.map((v) => (
        <span
          key={v.cls}
          className={`brand-custom ${v.cls}${v.view?.plate ? ' brand-plate' : ''} brand-${where}`}
        >
          <img
            src={v.view?.src ?? v.fallback}
            alt={branding.title || 'logo'}
            style={v.view ? undefined : { visibility: 'hidden' }}
            onError={() => setBroken(true)}
          />
          {branding.title && !header && <span className="brand-title">{branding.title}</span>}
        </span>
      ))}
    </>
  )
}

// Загружает состояние брендирования один раз на монтирование.
export function useBranding(): [Branding | null, () => void] {
  const [b, setB] = useState<Branding | null>(null)
  const [tick, setTick] = useState(0)
  useEffect(() => {
    let alive = true
    getBranding()
      .then((r) => alive && setB(r))
      .catch(() => alive && setB(null))
    return () => {
      alive = false
    }
  }, [tick])
  return [b, () => setTick((n) => n + 1)]
}
