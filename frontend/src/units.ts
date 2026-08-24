// Единицы измерения в одном месте.
//
// Таблица «Б/КБ/МБ/ГБ» жила копиями в пяти файлах, и локализована была только
// одна из них: английская панель показывала «41 ГБ» и «2ч 30м». Формулы
// округления у копий разные и на глаз заметные, поэтому здесь не один
// универсальный форматтер, а общие ЕДИНИЦЫ плюс два самых ходовых формата.
import { currentLang } from './i18n'

const BYTES = { ru: ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'], en: ['B', 'KB', 'MB', 'GB', 'TB'] }
const RATES = { ru: ['Б/с', 'КБ/с', 'МБ/с', 'ГБ/с'], en: ['B/s', 'KB/s', 'MB/s', 'GB/s'] }
const TIME = { ru: ['д', 'ч', 'м', 'с'], en: ['d', 'h', 'm', 's'] }

export const byteUnits = (): string[] => BYTES[currentLang()]
export const rateUnits = (): string[] => RATES[currentLang()]
export const timeUnits = (): string[] => TIME[currentLang()]

/** Байты по 1024: «512 Б», «4.2 МБ», «41 ГБ» (дробь только у малых значений). */
export function fmtBytes(n?: number, zero = '0'): string {
  if (!n) return zero
  const u = byteUnits()
  let v = n
  let i = 0
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`
}

/** Длительность из секунд: «2ч 30м», «45м 10с», «8с». */
export function fmtDur(s?: number): string {
  if (!s) return '—'
  const [, h, m, sec] = timeUnits()
  const hh = Math.floor(s / 3600)
  const mm = Math.floor((s % 3600) / 60)
  return hh > 0 ? `${hh}${h} ${mm}${m}` : mm > 0 ? `${mm}${m} ${s % 60}${sec}` : `${s}${sec}`
}
