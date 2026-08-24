// Скрины для README: снимает ЖИВУЮ панель на демо-данных (docs/demo-seed.py).
//
// Не моки: в кадре тот же фронт, который уходит в образ, поэтому картинка не
// может разойтись с интерфейсом. Данные вымышлены — домены из RFC 2606, адреса
// из RFC 5737, ни одной настоящей ноды.
//
// Обычно запускается не руками, а оркестратором, который поднимает панель на
// временной базе и убирает её за собой:
//
//   cd frontend && npm run build && npm i playwright-core && cd ..
//   python docs/make-shots.py
//
// Системный Chrome, без скачивания браузеров: PW_CHROME=<путь к chrome.exe>.
// SHOTS_LANG=en|ru снимает один язык (данные в базе тоже языковые).
import { chromium } from 'playwright-core'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'

const BASE = process.env.SHOTS_BASE || 'http://127.0.0.1:5173'
const USER = process.env.SHOTS_USER || 'admin'
const PASS = process.env.SHOTS_PASS || 'DemoPass123!'
const OUT = process.env.SHOTS_OUT ||
  path.join(import.meta.dirname, '..', '..', 'docs', 'img')
const CHROME = process.env.PW_CHROME ||
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'

// nav — подпись пункта меню (кликаем по ней), url — прямая ссылка на деталь.
// h — ПОТОЛОК высоты кадра: реальная считается по нижней границе содержимого,
// иначе половина кадров либо обрывается на полуслове, либо тянет пустой хвост.
// Потолок нужен там, где страница длинная и снимать её целиком незачем.
const SHOTS = [
  { name: 'home', h: 1100 },   // главная — корень, отдельного пункта меню у неё нет
  { name: 'servers', nav: { ru: 'Серверы', en: 'Servers' }, h: 1100 },
  { name: 'server-detail', url: '?server=1', h: 830 },
  { name: 'sites', nav: { ru: 'Сайты', en: 'Sites' }, h: 1180 },
  { name: 'site-detail', url: '?check=1', h: 1098 },
  // карточку ноды раскрываем: сложены в ней как раз домены, ради которых раздел и нужен
  { name: 'services', nav: { ru: 'Сервисы', en: 'Services' }, h: 800, open: 'web-01' },
  { name: 'backups', nav: { ru: 'Бэкапы', en: 'Backups' }, h: 1000 },
]

async function token() {
  const r = await fetch(`${BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: USER, password: PASS }),
  })
  if (!r.ok) throw new Error(`вход не удался: ${r.status}`)
  return (await r.json()).access_token
}

const run = async () => {
  const jwt = await token()
  const browser = await chromium.launch({ executablePath: CHROME })

  const langs = process.env.SHOTS_LANG ? [process.env.SHOTS_LANG] : ['en', 'ru']
  for (const lang of langs) {
    const dir = lang === 'en' ? OUT : path.join(OUT, 'ru')
    await mkdir(dir, { recursive: true })

    for (const shot of SHOTS) {
      const ctx = await browser.newContext({
        viewport: { width: 1440, height: shot.h },
        deviceScaleFactor: 2,
        locale: lang === 'ru' ? 'ru-RU' : 'en-US',
        // UTC, а не «живая» зона: демо-база — SQLite, он отдаёт время без
        // смещения, и в любой другой зоне свежий отчёт выглядел бы «2 ч назад».
        timezoneId: 'UTC',
        colorScheme: 'dark',
      })
      // Язык и тему панель держит в localStorage, токен — там же. Ставим ДО
      // первого рендера, иначе кадр ловится на перерисовке после смены языка.
      await ctx.addInitScript(([l, t]) => {
        localStorage.setItem('kervax_lang', l)
        localStorage.setItem('kervax_theme', 'dark')
        if (t) localStorage.setItem('kervax_token', t)
      }, [lang, shot.anon ? '' : jwt])

      const page = await ctx.newPage()
      // Анимации срезаем: иначе карточки попадают в кадр полупрозрачными.
      await page.addStyleTag({
        content: '*,*::before,*::after{animation:none!important;transition:none!important}',
      }).catch(() => {})
      await page.goto(BASE + (shot.url || ''), { waitUntil: 'networkidle' })
      if (shot.nav) {
        await page.getByRole('button', { name: shot.nav[lang], exact: true }).first().click()
        await page.waitForTimeout(900)
      }
      if (shot.open) {
        // раскрыть карточку ноды; вёрстка карточек меняется чаще всего,
        // поэтому промах здесь не должен ронять всю съёмку
        await page.getByText(shot.open).first().click({ timeout: 5000 })
          .catch(() => console.log(`  (не раскрылось: ${shot.open})`))
        await page.waitForTimeout(700)
      }
      await page.addStyleTag({
        content: '*,*::before,*::after{animation:none!important;transition:none!important}',
      })
      await page.waitForTimeout(1200)     // добор: графики дорисовываются после данных

      // Нижняя граница содержимого. Фон страницы — градиент, поэтому «обрезать
      // по цвету» не работает; спрашиваем саму вёрстку, где кончается контент.
      const bottom = await page.evaluate(() => {
        let max = 0
        for (const el of document.querySelectorAll('body *')) {
          const r = el.getBoundingClientRect()
          if (r.width > 8 && r.height > 8 && r.bottom > max) max = r.bottom
        }
        return Math.ceil(max)
      })
      const file = path.join(dir, `${shot.name}.png`)
      await page.screenshot({
        path: file,
        clip: { x: 0, y: 0, width: 1440, height: Math.min(bottom + 28, shot.h) },
      })
      console.log(`  ${lang}/${shot.name}.png`)
      await ctx.close()
    }
  }
  await browser.close()
}

run().catch((e) => {
  console.error(e.message)
  process.exit(1)
})
