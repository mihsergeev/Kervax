// ВНИМАНИЕ к неймингу кита: «-dark» = ТЁМНЫЙ (navy) логотип для светлой темы,
// «-light» = БЕЛЫЙ логотип для тёмной темы (т.е. наоборот от имени темы).
// Знак и надпись — отдельные обрезки горизонтального лого (по одному viewBox),
// чтобы управлять отступом между ними и не тянуть пустое поле в hover.
import markLightNavy from './assets/kervax-mark-dark.svg'
import markDarkWhite from './assets/kervax-mark-light.svg'
import wordLightNavy from './assets/kervax-word-dark.svg'
import wordDarkWhite from './assets/kervax-word-light.svg'
import lockupOnLightNavy from './assets/kervax-lockup-dark.svg'
import lockupOnDarkWhite from './assets/kervax-lockup-light.svg'

// Бренд Kervax — знак (сокол) + надпись KERVAX из фирменного кита.
// Тёмная/светлая версия переключаются темой через CSS (.brand-when-*).

type Props = {
  height?: number
  className?: string
}

// Горизонтальный логотип (знак + надпись справа) — шапка после входа.
// Надпись ниже знака (в оригинале высота текста 153 vs знака 334) — берём
// пропорцию, чтобы совпадало с макетом. Отступ знак↔надпись — в CSS (.brand-gap).
export function BrandHorizontal({ height = 40, className }: Props) {
  const wordH = Math.round((height * 153) / 334)
  return (
    <span className={`brand-img brand-lockup-h${className ? ` ${className}` : ''}`}>
      <img src={markDarkWhite} className="brand-when-dark" style={{ height }} alt="" />
      <img src={markLightNavy} className="brand-when-light" style={{ height }} alt="" />
      <span className="brand-gap" />
      <img
        src={wordDarkWhite}
        className="brand-when-dark"
        style={{ height: wordH }}
        alt="Kervax"
      />
      <img
        src={wordLightNavy}
        className="brand-when-light"
        style={{ height: wordH }}
        alt="Kervax"
      />
    </span>
  )
}

// Вертикальный локап (знак, надпись снизу) — экран входа. Файл квадратный.
// Только знак (сокол), без надписи — для мест, где рядом уже есть слово «Kervax»:
// строка «Работает на Kervax» и модалка «О панели».
export function BrandMark({ height = 20 }: { height?: number }) {
  return (
    <span className="brand-img">
      <img src={markDarkWhite} className="brand-when-dark" style={{ height }} alt="" />
      <img src={markLightNavy} className="brand-when-light" style={{ height }} alt="" />
    </span>
  )
}

export function BrandLockup({ width = 170 }: { width?: number }) {
  return (
    <span className="brand-img">
      <img
        src={lockupOnDarkWhite}
        className="brand-when-dark"
        style={{ width }}
        alt="Kervax"
      />
      <img
        src={lockupOnLightNavy}
        className="brand-when-light"
        style={{ width }}
        alt="Kervax"
      />
    </span>
  )
}
