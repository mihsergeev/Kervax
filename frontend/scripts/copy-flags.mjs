// Кладёт SVG-флаги из flag-icons в public/flags — оттуда их отдаёт статика, и браузер
// качает ровно тот файл, который показывает (у нас на экране максимум десяток стран).
//
// Почему не эмодзи-флаг (🇫🇮): системные шрифты Windows их не рисуют — вместо флага
// видны две буквы «FI», и панель выглядит так, будто страна взята из имени сервера.
//
// Почему не import CSS пакета: он описывает и 1x1, и 4x3, поэтому сборщик утащил бы в
// образ обе раскладки — 4.8 МБ вместо 2.4.
import { cp, mkdir } from 'node:fs/promises'

const src = 'node_modules/flag-icons/flags/4x3'
const dst = 'public/flags'
await mkdir(dst, { recursive: true })
await cp(src, dst, { recursive: true })
console.log(`flags: ${src} -> ${dst}`)
