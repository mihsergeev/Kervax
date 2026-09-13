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

// Плашка на ТЁМНОЙ теме: у картинки свой фон либо сам логотип тёмный.
export function needsPlate(a: { transparentEdges: boolean; dark: boolean }): boolean {
  return !a.transparentEdges || a.dark
}

// На СВЕТЛОЙ теме — только картинке со своим фоном. Тёмный логотип на прозрачном
// фоне там и так виден, а белая плашка под ним выглядела наклейкой: так было с
// логотипом corpsoft24 — почти чёрная надпись на белой карточке на светлой шапке.
export function needsPlateLight(a: { transparentEdges: boolean }): boolean {
  return !a.transparentEdges
}

// Светлый вариант SVG-логотипа для тёмной темы: очень тёмные цвета (надпись, контуры)
// становятся белыми, фирменные остаются. Типичный логотип — тёмная надпись и цветной
// знак, и для тёмного фона его перекрашивают именно так. null — перекрашивать нечего
// или файл не разобрался; результат человек видит в превью до сохранения.
export function lightenSvg(svg: string): string | null {
  const doc = new DOMParser().parseFromString(svg, 'image/svg+xml')
  const root = doc.documentElement
  if (!root || root.localName !== 'svg' || doc.getElementsByTagName('parsererror').length > 0)
    return null
  const ctx = document.createElement('canvas').getContext('2d')
  if (!ctx) return null
  const WHITE = '#ffffff'
  const isDark = (raw: string): boolean => {
    const v = raw.trim().replace(/\s*!important$/i, '')
    if (!v || /^(none|transparent|inherit|currentcolor|url\()/i.test(v)) return false
    // цвет разбирает сам браузер: так понимаются и имена, и rgb(), и #abc
    ctx.fillStyle = '#000001'
    ctx.fillStyle = v
    const got = String(ctx.fillStyle)
    if (got === '#000001' && v.toLowerCase() !== '#000001') return false // не цвет
    let r: number
    let g: number
    let b: number
    if (got.startsWith('#')) {
      r = parseInt(got.slice(1, 3), 16)
      g = parseInt(got.slice(3, 5), 16)
      b = parseInt(got.slice(5, 7), 16)
    } else {
      const m = got.match(/[\d.]+/g)
      if (!m || m.length < 3 || (m.length > 3 && Number(m[3]) === 0)) return false
      r = Number(m[0])
      g = Number(m[1])
      b = Number(m[2])
    }
    const max = Math.max(r, g, b)
    const min = Math.min(r, g, b)
    const lightness = (max + min) / 2 / 255
    const chroma = (max - min) / 255
    // почти чёрное любого оттенка или тёмно-серое; насыщенный фирменный синий остаётся
    return lightness < 0.3 || (lightness < 0.45 && chroma < 0.12)
  }
  let changed = false
  const props = ['fill', 'stroke', 'stop-color', 'color']
  for (const el of [root, ...Array.from(root.getElementsByTagName('*'))]) {
    // маска и контур обрезки сами не рисуются: их цвета задают форму, а не вид
    if (el.closest('mask, clipPath')) continue
    for (const prop of props) {
      const v = el.getAttribute(prop)
      if (v && isDark(v)) {
        el.setAttribute(prop, WHITE)
        changed = true
      }
    }
    const style = el.getAttribute('style')
    if (style) {
      const next = style.replace(
        /(^|;)(\s*)(fill|stroke|stop-color|color)(\s*:\s*)([^;]+)/gi,
        (m: string, pre: string, sp: string, prop: string, colon: string, val: string) =>
          isDark(val) ? `${pre}${sp}${prop}${colon}${WHITE}` : m,
      )
      if (next !== style) {
        el.setAttribute('style', next)
        changed = true
      }
    }
  }
  for (const st of Array.from(root.getElementsByTagName('style'))) {
    const css = st.textContent ?? ''
    const next = css.replace(
      /(fill|stroke|stop-color|color)(\s*:\s*)([^;}]+)/gi,
      (m: string, prop: string, colon: string, val: string) =>
        isDark(val) ? `${prop}${colon}${WHITE}` : m,
    )
    if (next !== css) {
      st.textContent = next
      changed = true
    }
  }
  // фигуры без заливки по умолчанию чёрные — это тоже тёмная надпись
  if (!root.hasAttribute('fill') && !/(^|;)\s*fill\s*:/i.test(root.getAttribute('style') ?? '')) {
    root.setAttribute('fill', WHITE)
    changed = true
  }
  return changed ? new XMLSerializer().serializeToString(doc) : null
}

export function svgDataUrl(svg: string): string {
  let bin = ''
  for (const byte of new TextEncoder().encode(svg)) bin += String.fromCharCode(byte)
  return `data:image/svg+xml;base64,${btoa(bin)}`
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
  // Логотип залит до того, как плашку стали решать по темам: анализа для светлой
  // темы у него нет — разбираем картинку сами, один раз.
  const [legacyLight, setLegacyLight] = useState<boolean | null>(null)
  const legacy = !!branding?.logo && branding.plate_light == null
  const version = branding?.version ?? 0
  useEffect(() => {
    if (!legacy) return
    let alive = true
    analyzeLogo(brandingLogoUrl(version))
      .then((a) => alive && setLegacyLight(needsPlateLight(a)))
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [legacy, version])

  if (!branding?.logo || broken) {
    return header ? <BrandHorizontal height={40} /> : <BrandLockup width={170} />
  }
  const on = (auto: boolean) =>
    branding.plate === 'always' || (branding.plate === 'auto' && auto)
  // Две версии, тему выбирает CSS (brand-when-dark / brand-when-light) — как у
  // штатного логотипа: переключение темы не ждёт перерисовки.
  const variants = [
    {
      cls: 'brand-when-dark',
      src: brandingLogoUrl(branding.version, branding.dark_logo),
      plate: on(branding.dark_logo ? branding.dark_plate : branding.plate_auto),
    },
    {
      cls: 'brand-when-light',
      src: brandingLogoUrl(branding.version),
      plate: on(branding.plate_light ?? legacyLight ?? false),
    },
  ]
  return (
    <>
      {variants.map((v) => (
        <span
          key={v.cls}
          className={`brand-custom ${v.cls}${v.plate ? ' brand-plate' : ''} brand-${where}`}
        >
          <img src={v.src} alt={branding.title || 'logo'} onError={() => setBroken(true)} />
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
