import { useEffect, useState } from 'react'
import { BrandHorizontal, BrandLockup } from './Logo'
import { brandingLogoUrl, getBranding, type Branding } from './api'

// Свой логотип вместо стандартного.
//
// Главная сложность не в загрузке, а в том, чтобы чужая картинка не выглядела
// «наклейкой». Три типовые беды:
//   1) JPEG/PNG с непрозрачным белым фоном на тёмной теме — белый прямоугольник;
//   2) тёмный логотип на прозрачном фоне — на тёмной теме почти не виден;
//   3) любые пропорции: от узкой полоски до квадрата.
// Первые две решает ПОДЛОЖКА (светлая карточка со скруглением и отступами):
// логотип на ней смотрится намеренно, а не вырезанным. Нужна ли она, решает
// анализ пикселей при загрузке (см. analyzeLogo), но админ может и переопределить.
// Третью — жёсткая высота и object-fit: contain, ширина ограничена сверху.

// Разбор картинки: прозрачные ли края и насколько логотип тёмный.
// Делается в момент ЗАГРУЗКИ (в модалке настроек), а не на каждый показ.
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
    // тёмный логотип: на тёмной теме утонет без подложки
    dark: lumaCount > 0 && lumaSum / lumaCount < 110,
  }
}

export function needsPlate(a: { transparentEdges: boolean; dark: boolean }): boolean {
  return !a.transparentEdges || a.dark
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

  if (!branding?.logo || broken) {
    return header ? <BrandHorizontal height={40} /> : <BrandLockup width={170} />
  }
  const plate = branding.plate === 'always' || (branding.plate === 'auto' && branding.plate_auto)
  return (
    <span className={`brand-custom${plate ? ' brand-plate' : ''} brand-${where}`}>
      <img
        src={brandingLogoUrl(branding.version)}
        alt={branding.title || 'logo'}
        onError={() => setBroken(true)}
      />
      {branding.title && !header && <span className="brand-title">{branding.title}</span>}
    </span>
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
