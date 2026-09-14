// Свой логотип «как родной» на обеих темах.
//
// Логотип рисуют под один фон, обычно белый. На тёмной теме его тёмная надпись тонет,
// а светлая плашка под ней выглядит наклейкой. Поэтому цвета, которые на фоне темы
// читаются плохо, подтягиваем: нейтральные (надпись, контур) — в тон тексту панели,
// фирменные — тот же оттенок и насыщенность, светлота ровно до читаемости. Остальное не
// трогаем: тёмный логотип на светлой теме остаётся нетронутым, синий знак остаётся синим.
//
// Картинку со своим фоном и объёмную (фото, градиенты) не перекрашиваем — это её
// испортит; для них остаётся плашка. Всё считается в браузере при показе: файл
// хранится один, и перекраска не отстаёт от него, когда логотип заменят.
import { analyzeLogo } from './BrandLogo'

export type LogoTheme = 'dark' | 'light'
export type LogoMode = 'auto' | 'always' | 'never'
// url — картинка (свой origin или data:), svg — её текст, если это SVG и он уже известен
export type LogoSource = { url: string; svg?: string | null }
export type LogoView = { src: string; plate: boolean; adapted: boolean }

type RGB = [number, number, number]

// Фоны, на которых логотип показывается в каждой теме: шапка (--bg) и карточка входа
// (--bg-card). Читаемость проверяем по худшему из двух.
const BGS: Record<LogoTheme, RGB[]> = {
  dark: [[15, 17, 23], [23, 26, 35]],
  light: [[228, 232, 239], [255, 255, 255]],
}
// текст панели (--text): в его тон уходят нейтральные цвета логотипа
const TEXT: Record<LogoTheme, RGB> = { dark: [230, 233, 240], light: [20, 24, 36] }
const MIN_CONTRAST = 3 // ниже — на фоне темы цвет читается плохо
const TARGET = 3.3 // до этой контрастности подтягиваем фирменный цвет

const lin = (c: number) => {
  const v = c / 255
  return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
}
const luminance = ([r, g, b]: RGB) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
const contrast = (a: RGB, b: RGB) => {
  const la = luminance(a)
  const lb = luminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}
const readability = (c: RGB, theme: LogoTheme) =>
  Math.min(...BGS[theme].map((bg) => contrast(c, bg)))

function toHsl([r, g, b]: RGB): [number, number, number] {
  const rn = r / 255
  const gn = g / 255
  const bn = b / 255
  const max = Math.max(rn, gn, bn)
  const min = Math.min(rn, gn, bn)
  const l = (max + min) / 2
  if (max === min) return [0, 0, l]
  const d = max - min
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
  const h =
    max === rn ? (gn - bn) / d + (gn < bn ? 6 : 0) : max === gn ? (bn - rn) / d + 2 : (rn - gn) / d + 4
  return [h / 6, s, l]
}

function fromHsl(h: number, s: number, l: number): RGB {
  if (s === 0) {
    const v = Math.round(l * 255)
    return [v, v, v]
  }
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s
  const p = 2 * l - q
  const ch = (t: number) => {
    if (t < 0) t += 1
    if (t > 1) t -= 1
    if (t < 1 / 6) return p + (q - p) * 6 * t
    if (t < 1 / 2) return q
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6
    return p
  }
  return [Math.round(ch(h + 1 / 3) * 255), Math.round(ch(h) * 255), Math.round(ch(h - 1 / 3) * 255)]
}

/** Новый цвет для темы или null, если этот и так читается. */
export function adaptRgb(c: RGB, theme: LogoTheme): RGB | null {
  if (readability(c, theme) >= MIN_CONTRAST) return null
  const [h, s, l] = toHsl(c)
  const chroma = (Math.max(...c) - Math.min(...c)) / 255
  if (chroma < 0.15) {
    // Нейтральный — надпись или контур: в тон тексту панели. Иерархию тонов сохраняем:
    // чёрное станет самым светлым, тёмно-серое — чуть приглушённее.
    const tl = toHsl(TEXT[theme])[2]
    const nl = theme === 'dark' ? tl - l * 0.35 : tl + (1 - l) * 0.35
    return fromHsl(h, s, Math.min(1, Math.max(0, nl)))
  }
  // Фирменный: оттенок и насыщенность те же, светлоту двигаем ровно до читаемости.
  let lo = l
  let hi = theme === 'dark' ? 1 : 0
  for (let i = 0; i < 18; i++) {
    const mid = (lo + hi) / 2
    if (readability(fromHsl(h, s, mid), theme) >= TARGET) hi = mid
    else lo = mid
  }
  return fromHsl(h, s, hi)
}

const hex = ([r, g, b]: RGB) => `#${[r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')}`

// Итог перекраски: новая картинка, «и так хорошо» или «перекрашивать нельзя».
type Adapted = { out: string } | 'ok' | 'skip'

function adaptSvg(svg: string, theme: LogoTheme): Adapted {
  const doc = new DOMParser().parseFromString(svg, 'image/svg+xml')
  const root = doc.documentElement
  if (!root || root.localName !== 'svg' || doc.getElementsByTagName('parsererror').length > 0)
    return 'skip'
  // вложенную растровую картинку в SVG перекрасить нельзя — лучше не трогать вовсе
  if (root.getElementsByTagName('image').length > 0) return 'skip'
  const ctx = document.createElement('canvas').getContext('2d')
  if (!ctx) return 'skip'
  // цвет разбирает сам браузер: так понимаются и имена, и rgb(), и #abc
  const recolor = (raw: string): string | null => {
    const v = raw.trim().replace(/\s*!important$/i, '')
    if (!v || /^(none|transparent|inherit|currentcolor|url\()/i.test(v)) return null
    ctx.fillStyle = '#000001'
    ctx.fillStyle = v
    const got = String(ctx.fillStyle)
    if (got === '#000001' && v.toLowerCase() !== '#000001') return null // не цвет
    let rgb: RGB
    let alpha = ''
    if (got.startsWith('#')) {
      rgb = [parseInt(got.slice(1, 3), 16), parseInt(got.slice(3, 5), 16), parseInt(got.slice(5, 7), 16)]
    } else {
      const m = got.match(/[\d.]+/g)
      if (!m || m.length < 3) return null
      rgb = [Number(m[0]), Number(m[1]), Number(m[2])]
      if (m.length > 3) alpha = m[3]
    }
    const to = adaptRgb(rgb, theme)
    if (!to) return null
    return alpha ? `rgba(${to[0]}, ${to[1]}, ${to[2]}, ${alpha})` : hex(to)
  }
  let changed = false
  const props = ['fill', 'stroke', 'stop-color', 'flood-color', 'color']
  for (const el of [root, ...Array.from(root.getElementsByTagName('*'))]) {
    // маска и контур обрезки сами не рисуются: их цвета задают форму, а не вид
    if (el.closest('mask, clipPath')) continue
    for (const prop of props) {
      const v = el.getAttribute(prop)
      const to = v ? recolor(v) : null
      if (to) {
        el.setAttribute(prop, to)
        changed = true
      }
    }
    const style = el.getAttribute('style')
    if (style) {
      const next = style.replace(
        /(^|;)(\s*)(fill|stroke|stop-color|flood-color|color)(\s*:\s*)([^;]+)/gi,
        (m: string, pre: string, sp: string, prop: string, colon: string, val: string) => {
          const to = recolor(val)
          return to ? `${pre}${sp}${prop}${colon}${to}` : m
        },
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
      /(fill|stroke|stop-color|flood-color|color)(\s*:\s*)([^;}]+)/gi,
      (m: string, prop: string, colon: string, val: string) => {
        const to = recolor(val)
        return to ? `${prop}${colon}${to}` : m
      },
    )
    if (next !== css) {
      st.textContent = next
      changed = true
    }
  }
  // Фигуры без заливки по умолчанию чёрные — это тоже надпись, которую надо подтянуть.
  if (!root.hasAttribute('fill') && !/(^|;)\s*fill\s*:/i.test(root.getAttribute('style') ?? '')) {
    const to = adaptRgb([0, 0, 0], theme)
    if (to) {
      root.setAttribute('fill', hex(to))
      changed = true
    }
  }
  return changed ? { out: svgDataUrl(new XMLSerializer().serializeToString(doc)) } : 'ok'
}

async function loadImage(src: string): Promise<HTMLImageElement | null> {
  const img = new Image()
  img.src = src
  try {
    await img.decode()
  } catch {
    return null
  }
  return img.naturalWidth ? img : null
}

function adaptPixels(img: HTMLImageElement, theme: LogoTheme): Adapted {
  const scale = Math.min(1, 1600 / img.naturalWidth)
  const w = Math.max(1, Math.round(img.naturalWidth * scale))
  const h = Math.max(1, Math.round(img.naturalHeight * scale))
  const cv = document.createElement('canvas')
  cv.width = w
  cv.height = h
  const ctx = cv.getContext('2d', { willReadFrequently: true })
  if (!ctx) return 'skip'
  ctx.drawImage(img, 0, 0, w, h)
  let data: ImageData
  try {
    data = ctx.getImageData(0, 0, w, h)
  } catch {
    return 'skip'
  }
  const px = data.data
  // Плоский ли логотип: у надписи и знака цветов единицы, а у фото или объёмной эмблемы
  // их сотни — перекраска по цветам такую картинку только испортит.
  const hist = new Map<number, number>()
  let opaque = 0
  for (let i = 0; i < px.length; i += 4) {
    if (px[i + 3] < 200) continue
    opaque++
    const key = ((px[i] >> 3) << 10) | ((px[i + 1] >> 3) << 5) | (px[i + 2] >> 3)
    hist.set(key, (hist.get(key) ?? 0) + 1)
  }
  if (!opaque) return 'skip'
  const top = [...hist.values()]
    .sort((a, b) => b - a)
    .slice(0, 12)
    .reduce((a, b) => a + b, 0)
  if (top / opaque < 0.9) return 'skip'
  const cache = new Map<number, RGB | null>()
  let changed = false
  for (let i = 0; i < px.length; i += 4) {
    if (px[i + 3] === 0) continue
    const key = (px[i] << 16) | (px[i + 1] << 8) | px[i + 2]
    let to = cache.get(key)
    if (to === undefined) {
      to = adaptRgb([px[i], px[i + 1], px[i + 2]], theme)
      cache.set(key, to)
    }
    if (to) {
      px[i] = to[0]
      px[i + 1] = to[1]
      px[i + 2] = to[2]
      changed = true
    }
  }
  if (!changed) return 'ok'
  ctx.putImageData(data, 0, 0)
  return { out: cv.toDataURL('image/png') }
}

export function svgDataUrl(svg: string): string {
  let bin = ''
  for (const byte of new TextEncoder().encode(svg)) bin += String.fromCharCode(byte)
  return `data:image/svg+xml;base64,${btoa(bin)}`
}

// Текст SVG для своего origin. data:-адреса fetch'ем не читаем: их не пускает CSP
// панели (connect-src 'self'), а для только что выбранного файла текст и так есть.
async function svgText(source: LogoSource): Promise<string | null> {
  if (source.svg !== undefined) return source.svg
  if (source.url.startsWith('data:')) return null
  try {
    const r = await fetch(source.url)
    if (!r.ok || !(r.headers.get('content-type') ?? '').startsWith('image/svg')) return null
    return await r.text()
  } catch {
    return null
  }
}

const cache = new Map<string, Promise<LogoView>>()

/** Как показать логотип на этой теме: перекрашенным, на плашке или как есть. */
export function logoView(source: LogoSource, theme: LogoTheme, mode: LogoMode): Promise<LogoView> {
  const key = `${theme}|${mode}|${source.url}`
  let got = cache.get(key)
  if (!got) {
    got = compute(source, theme, mode)
    cache.set(key, got)
  }
  return got
}

async function compute(source: LogoSource, theme: LogoTheme, mode: LogoMode): Promise<LogoView> {
  const plain = { src: source.url, adapted: false }
  if (mode === 'always') return { ...plain, plate: true }
  if (mode === 'never') return { ...plain, plate: false }
  const a = await analyzeLogo(source.url)
  // своя подложка у картинки — перекрашивать нечего, её фон и есть фон
  if (!a.transparentEdges) return { ...plain, plate: true }
  const text = await svgText(source)
  let res: Adapted = 'skip'
  if (text) {
    res = adaptSvg(text, theme)
  } else {
    const img = await loadImage(source.url)
    if (img) res = adaptPixels(img, theme)
  }
  if (typeof res === 'object') return { src: res.out, plate: false, adapted: true }
  // перекрасить нельзя, а тёмный логотип на тёмной теме не виден — тогда плашка
  return { ...plain, plate: res === 'skip' && theme === 'dark' && a.dark }
}
