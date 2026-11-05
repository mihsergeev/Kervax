// Run against the local frontend only. API responses are synthetic and intercepted in the browser.
// Usage: npm run dev -- --host 127.0.0.1 --port 5182, then node scripts/test-services-matrix.mjs
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright-core'

const base = process.env.MATRIX_BASE || 'http://127.0.0.1:5182'
assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Use a local frontend')
const out = process.env.MATRIX_SHOTS
if (out) await mkdir(out, { recursive: true })
const storage = ['PostgreSQL', 'MySQL/MariaDB', 'Qdrant', 'Weaviate', 'Chroma', 'Milvus', 'Redis', 'Valkey', 'MinIO', 'Memcached', 'MS SQL Server', 'ZooKeeper']
const node = (id, name, group, kinds) => ({
  id, name, group_name: group, access_ip: `192.0.2.${id}`, online: true, os: 'Ubuntu 24.04', country: 'de',
  backup_audit: kinds.map(subject => ({ kind: 'db', subject, container: `fixture-${subject.toLowerCase().replace(/\W/g, '-')}` })),
  last_report: { services: [], db_stats: [], web_services: id === 1
    ? ['nginx', 'HAProxy', 'Envoy', 'Traefik'].map(kind => ({ kind, source: 'контейнер fixture-proxy', sites: ['fixture.example'] }))
    : [], extras: {}, disks: [] },
  llm: id === 1 ? ['vLLM', 'SGLang', 'TEI', 'LiteLLM', 'vLLM'].map(engine_name => ({ engine_name, where: 'контейнер fixture-llm', engine: engine_name.toLowerCase(), ok: true })) : [],
  queue_brokers: id === 1 ? ['Celery', 'Sidekiq', 'BullMQ', 'RQ', 'Symfony Messenger'].map(label => ({ kind: label.toLowerCase(), label, engine: 'redis', source: `fixture-${label}`, queues: [], total: 0 })) : [],
})
const servers = [node(1, 'alpha-with-a-very-long-server-name-for-overflow', 'Application', storage),
  node(2, 'beta', 'Application', ['PostgreSQL']), node(3, 'gamma', 'Storage', ['Qdrant'])]
const browser = await chromium.launch({ executablePath: process.env.PW_CHROME || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', headless: true })
try {
  for (const theme of ['dark', 'light']) {
    for (const lang of ['ru', 'en']) {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: theme })
      await ctx.addInitScript(({ theme, lang }) => {
        localStorage.setItem('kervax_token', 'synthetic-ui-fixture')
        localStorage.setItem('kervax_theme', theme)
        localStorage.setItem('kervax_lang', lang)
      }, { theme, lang })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', error => errors.push(error.message))
      await page.route('**/api/**', route => {
        const path = new URL(route.request().url()).pathname
        const data = path === '/api/servers' ? servers
          : path === '/api/auth/me' ? { id: 1, username: 'fixture', role: 'admin' }
          : path === '/api/checks/known-hosts' ? { hosts: {}, groups: [], ignored: [] }
          : path === '/api/panel-errors/count' ? { count: 0 }
          : path === '/api/branding' ? { logo: false } : {}
        return route.fulfill({ json: data })
      })
      await page.goto(`${base}/?section=services`)
      await page.locator('.svc-matrix-row').first().waitFor()
      assert.equal(await page.locator('.svc-matrix-row').count(), 3)
      assert.equal(await page.locator('.svc-matrix-header').count(), 2)
      for (const width of [1440, 1024, 801, 800, 390, 320]) {
        await page.setViewportSize({ width, height: 1000 })
        await page.waitForTimeout(150)
        const sizes = await page.evaluate(() => ({
          overflow: document.documentElement.scrollWidth > window.innerWidth,
          outside: [...document.querySelectorAll('body *')].filter(el => getComputedStyle(el).visibility !== 'hidden' && el.getBoundingClientRect().right > innerWidth + 1).slice(0, 12).map(el => [el.className, el.getBoundingClientRect().right]),
          rows: [...document.querySelectorAll('.svc-matrix-row')].map(row => ({
            height: row.getBoundingClientRect().height,
            cells: [...row.querySelectorAll('.svc-matrix-services')].map(cell => ({
              width: cell.clientWidth,
              chips: [...cell.querySelectorAll('.svc-matrix-chip, .svc-matrix-more')].map(el => [el.textContent, el.getBoundingClientRect().width, getComputedStyle(el).fontWeight]),
              used: [...cell.children].filter(el => !el.classList.contains('svc-matrix-measure'))
                .reduce((n, el, i) => n + el.getBoundingClientRect().width + (i ? parseFloat(getComputedStyle(cell).columnGap) : 0), 0),
            })),
          })),
        }))
        // At 320px the existing global nav is wider than the screen; the matrix must still fit.
        if (width >= 390) assert.equal(sizes.overflow, false, `page overflow: ${theme}/${lang}/${width}: ${JSON.stringify(sizes.outside)}`)
        else assert(sizes.outside.every(([cls]) => !String(cls).startsWith('svc-matrix')), 'matrix fits narrow screens')
        for (const row of sizes.rows) {
          if (width > 800) assert(row.height < 65, `desktop row wrapped at ${width}`)
          for (const cell of row.cells) assert(cell.used <= cell.width + 1, `cell overflow at ${width}: ${JSON.stringify(cell)}`)
        }
        assert(await page.locator('.svc-matrix-row').first().getByRole('button', { name: lang === 'ru' ? /Показать еще/ : /Show .* more/ }).count() > 0)
        if (out && [1440, 390].includes(width)) await page.screenshot({ path: `${out}/${theme}-${lang}-${width}.png`, fullPage: true })
      }
      // Hidden services remain reachable by keyboard, opening the unchanged complete server card.
      const more = page.locator('.svc-matrix-row').first().locator('button.svc-matrix-more').first()
      await more.focus()
      await page.keyboard.press('Enter')
      await page.locator('.hs-modal').waitFor()
      assert(new URL(page.url()).searchParams.has('services'), 'modal deep link')
      const inventory = await page.locator('.hs-modal .srv-meta').innerText()
      for (const kind of storage) assert(inventory.includes(kind), `full card retains hidden ${kind}`)
      assert(inventory.includes('vLLM (2)'), 'duplicate instances retained')
      for (const brand of ['Celery', 'Sidekiq', 'BullMQ', 'RQ', 'Symfony Messenger']) {
        assert.equal(await page.locator('.hs-modal .srv-meta').getByRole('img', { name: brand, exact: true }).count(), 1, `service brand instead of Redis for ${brand}`)
      }
      await page.waitForFunction(() => [...document.querySelectorAll('.hs-modal img.engine-ico')].every(img => img.complete && img.naturalWidth > 0))
      await page.locator('.hs-modal').getByTitle(lang === 'ru' ? 'Закрыть' : 'Close', { exact: true }).focus()
      await page.keyboard.press('Enter')
      await page.locator('.hs-modal').waitFor({ state: 'hidden' })
      const search = page.locator('input[name="services-search"]')
      await search.focus()
      await search.fill('Weaviate')
      assert.equal(await page.locator('.svc-matrix-row').count(), 1)
      await search.fill('192.0.2.3')
      assert.equal(await page.locator('.svc-matrix-row').count(), 1)
      assert((await page.locator('.svc-matrix-node').innerText()).includes('gamma'))
      await search.fill('')
      await page.getByRole('button', { name: lang === 'ru' ? 'Без групп' : 'No grouping', exact: true }).click()
      assert.equal(await page.locator('.svc-matrix-header').count(), 1)
      assert.equal(await page.locator('.svc-matrix-row').count(), 3)
      const webTile = page.locator('.htile-head').filter({ hasText: lang === 'ru' ? 'Веб-серверы' : 'Web servers' })
      await webTile.click()
      assert.equal(await page.locator('.svc-matrix-row').count(), 1)
      assert.equal(await page.locator('.svc-matrix-row .svc-matrix-count').innerText(), '4')
      assert.equal(await page.locator('.svc-matrix-row .svc-matrix-empty').count(), 2)
      await webTile.click()
      assert.equal(await page.locator('.svc-matrix-row').count(), 3)
      await page.reload()
      await page.locator('.svc-matrix-row').first().waitFor()
      assert.equal(await page.locator('.svc-matrix-header').count(), 1, 'grouping persists after reload')
      assert.deepEqual(errors, [], 'no browser runtime errors')
      console.log(`${theme}/${lang}: desktop/mobile, one-line rows, overflow, full inventory, instance counts, keyboard modal, deep link, hidden-service/IP search, tile filter, persistent grouping passed`)
      await ctx.close()
    }
  }
} finally { await browser.close() }
