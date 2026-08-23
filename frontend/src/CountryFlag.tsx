/** Флаг страны сервера по ISO-коду, который бэкенд определил ПО IP (см. app/geoip.py).
 * Имя сервера в определении не участвует — совпадение с префиксом вроде «fi-hz-…» это
 * следствие вашего нейминга, а не источник данных.
 *
 * Флаг — SVG-картинка из flag-icons (MIT), лежит в public/flags и отдаётся статикой:
 * браузер качает ровно тот файл, который показывает. Эмодзи-флаг (🇫🇮) не годится —
 * системные шрифты Windows рисуют его как две буквы «FI».
 *
 * Название страны берём у Intl.DisplayNames: браузер знает их на языке интерфейса,
 * свой словарь держать незачем.
 */

import { useState } from 'react'

type Props = { code?: string; title?: string }

const names = new Map<string, Intl.DisplayNames>()

function countryName(code: string, lang: string): string {
  try {
    let dn = names.get(lang)
    if (!dn) {
      dn = new Intl.DisplayNames([lang], { type: 'region' })
      names.set(lang, dn)
    }
    return dn.of(code) || code
  } catch {
    return code // старый браузер / неизвестный регион — покажем сам код
  }
}

export function CountryFlag({ code, title }: Props) {
  const [broken, setBroken] = useState(false)
  const cc = (code || '').trim().toUpperCase()
  if (!/^[A-Z]{2}$/.test(cc)) return null
  const lang = typeof document !== 'undefined' ? document.documentElement.lang || 'ru' : 'ru'
  const hint = title || `${countryName(cc, lang)} (${cc})`
  // нет такого файла (экзотический код) — не показываем битую картинку, отдаём код текстом
  if (broken) {
    return <span className="country-flag country-flag-txt" title={hint}>{cc}</span>
  }
  return (
    <img
      className="country-flag"
      src={`/flags/${cc.toLowerCase()}.svg`}
      alt={cc}
      title={hint}
      loading="lazy"
      onError={() => setBroken(true)}
    />
  )
}
