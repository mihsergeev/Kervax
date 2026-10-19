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
// SHOTS_ONLY=kube,home переснимает только эти кадры, остальные файлы не трогает.
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
const ONLY = (process.env.SHOTS_ONLY || '').split(',').map((s) => s.trim()).filter(Boolean)

// nav — подпись пункта меню (кликаем по ней), url — прямая ссылка на деталь.
// h — ПОТОЛОК высоты кадра: реальная считается по нижней границе содержимого,
// иначе половина кадров либо обрывается на полуслове, либо тянет пустой хвост.
// Потолок нужен там, где страница длинная и снимать её целиком незачем.
const SHOTS = [
  { name: 'home', h: 1300 },   // главная - корень, отдельного пункта меню у нее нет
  { name: 'servers', nav: { ru: 'Серверы', en: 'Servers' }, h: 1000 },
  // карточка сервера: сводка с картой разделов, диски, безопасность, веб, GPU
  { name: 'server', url: '?server=2', h: 1150 },
  { name: 'disks', url: '?server=3&sec=disk', h: 1500 },
  // крупный план: только эти карточки раздела (окно высокое, чтобы они влезли без прокрутки)
  { name: 'disk-health', url: '?server=3&sec=disk', h: 2000, cards: ['mcard-diskhealth', 'mcard-diskwrite'] },
  { name: 'security-ports', url: '?server=2&sec=ports', h: 1000 },
  { name: 'security-access', url: '?server=2&sec=access', h: 1100 },
  { name: 'web', url: '?server=1&sec=web', h: 1250 },
  { name: 'gpu', url: '?server=9&sec=gpu', h: 1250 },
  { name: 'llm', url: '?services=9&sec=llm', h: 1000 },
  // окна из меню
  { name: 'vulns', menu: { ru: 'Уязвимости', en: 'Vulnerabilities' }, h: 1000 },
  { name: 'alerts', menu: { ru: 'История алертов', en: 'Alert history' }, h: 1000 },
  { name: 'services', url: '?services=3&sec=db', h: 1100 },
  { name: 'services-app', url: '?services=5', h: 1000 },
  { name: 'kube', url: '?kube=6&sec=expiry', h: 1100 },
  { name: 'sites', nav: { ru: 'Сайты', en: 'Sites' }, h: 1180 },
  { name: 'site-detail', url: '?check=1', h: 1098 },
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

    for (const shot of SHOTS.filter((s) => !ONLY.length || ONLY.includes(s.name))) {
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
      if (shot.menu) {
        // окно из меню в шапке: сначала само меню, потом пункт
        await page.getByRole('button', { name: lang === 'ru' ? 'Меню' : 'Menu', exact: true }).first().click()
        await page.waitForTimeout(300)
        await page.getByRole('button', { name: shot.menu[lang], exact: true }).first().click()
        await page.waitForTimeout(1200)
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
      let clip = { x: 0, y: 0, width: 1440, height: Math.min(bottom + 28, shot.h) }
      if (shot.cards) {
        // общий прямоугольник карточек плюс поле вокруг
        const r = await page.evaluate((ids) => {
          const rs = ids.map((id) => document.getElementById(id)?.getBoundingClientRect()).filter(Boolean)
          if (!rs.length) return null
          const x = Math.min(...rs.map((b) => b.left)), y = Math.min(...rs.map((b) => b.top))
          return { x, y, w: Math.max(...rs.map((b) => b.right)) - x, h: Math.max(...rs.map((b) => b.bottom)) - y }
        }, shot.cards)
        if (r) clip = { x: Math.max(0, r.x - 6), y: Math.max(0, r.y - 6), width: r.w + 12, height: r.h + 12 }
        else console.log(`  (нет карточек: ${shot.cards.join(', ')})`)
      }
      const file = path.join(dir, `${shot.name}.png`)
      await page.screenshot({ path: file, clip })
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
