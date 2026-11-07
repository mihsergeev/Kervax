// Run against npm run dev -- --host 127.0.0.1 --port 5334, with PW_CHROME set to a local Chrome executable.
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'
import { chromium } from 'playwright-core'

const base = process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5334'
assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Use a local frontend')
const out = process.env.KERVAX_TEST_SCREENSHOTS
if (out) await mkdir(out, { recursive: true })

const domain = 'example-monitoring-service.test'
const statusName = 'Unavailable demo monitor'
const sslName = 'Certificate demo monitor'
const check = (id, name, fields) => ({
  id, name, type: 'http', target: `https://${name}`, enabled: true, group_name: '',
  last_status: 'up', check_ssl: false, check_domain: false, ...fields,
})
const checks = Array.from({ length: 9 }, (_, i) => check(i + 1, `web${i + 1}.${domain}`, {
  check_domain: true, domain_days: 9 + i, domain_warn_days: [30], probe_local: i > 0,
}))
checks.push(check(10, statusName, { target: 'https://status.example.test', last_status: 'down', last_message: 'HTTP 503' }))
checks.push(check(11, sslName, { target: 'https://ssl.example.test', check_ssl: true, ssl_days: 14, ssl_warn_days: [14] }))
const maintenance = [{ id: 1, title: 'Demo maintenance', active: true, cur_end: '2030-01-01T12:00:00Z', server_ids: [], check_ids: [1] }]

const browser = await chromium.launch({ headless: true, executablePath: process.env.PW_CHROME })
try {
  const page = await browser.newPage({ viewport: { width: 850, height: 900 } })
  const errors = [], posts = [], jobs = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.addInitScript(() => {
    localStorage.setItem('kervax_token', 'synthetic-ui-fixture')
    localStorage.setItem('kervax_theme', 'dark')
    localStorage.setItem('kervax_lang', 'ru')
  })
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (request.method() === 'POST') {
      const body = request.postDataJSON()
      posts.push({ pathname, body })
      if (pathname === '/api/alert-rechecks') {
        const job = { ...body, id: String(jobs.length + 1), status: 'remains', message: '', created_at: '2026-01-01T00:00:00Z' }
        jobs.push(job)
        return route.fulfill({ json: job })
      }
      if (pathname === '/api/checks/11/expiry-snooze') {
        checks[10].expiry_snooze = body.mode === null ? null : { ssl: { mode: body.mode, days: 14 } }
        return route.fulfill({ json: checks[10] })
      }
    }
    const data = {
      '/api/auth/me': { id: 1, username: 'fixture', role: 'admin' },
      '/api/checks/overview': { total: checks.length, up: checks.length - 1, down: 1, open_incidents: 0, checks },
      '/api/alert-rechecks': jobs,
      '/api/maintenance': maintenance,
      '/api/servers': [],
      '/api/checks/discovered': { domains: [], hosts: {}, groups: [], ignored: [] },
      '/api/checks/local-probe-suggestions': { items: [] },
      '/api/alerts/coverage': { items: [] },
    }[pathname] ?? {}
    return route.fulfill({ json: data })
  })

  await page.goto(base)
  const row = (name) => page.locator('.home-block .home-attention-row').filter({ has: page.locator('b', { hasText: name }) })
  const grouped = row(domain)
  const status = row(statusName)
  const ssl = row(sslName)
  const card = grouped.locator('xpath=ancestor::div[contains(@class, "home-block")][1]')
  const snapshot = async (name) => { if (out) await card.screenshot({ path: path.join(out, `${name}.png`) }) }
  const recheck = (item) => item.getByRole('button', { name: 'Исправил: перепроверить', exact: true })

  await grouped.getByText('доступно 1 из 9').waitFor()
  const neighbor = await page.locator('.maint-home-row').boundingBox()
  assert(neighbor.x > (await card.boundingBox()).x, 'Narrow desktop must render two columns')
  for (const width of [850, 1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 })
    await snapshot(`sites-${width}`)
    const name = await grouped.locator('b').evaluate((element) => element.getClientRects().length)
    assert(name <= (width === 320 ? 2 : 1), `Domain name breaks into ${name} lines at ${width}px`)
    if (width > 320) assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Page overflow at ${width}px`)
    assert(await card.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), `Site card overflow at ${width}px`)
    for (const item of [grouped, status, ssl]) {
      assert(await item.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), `Site alert overflow at ${width}px`)
    }
  }
  await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
  await snapshot('sites-320-light')
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })

  await recheck(grouped).click()
  await grouped.getByText('Проблема осталась').waitFor()
  assert.deepEqual(posts[0], { pathname: '/api/alert-rechecks', body: { object_type: 'check', object_id: 1, kind: 'domain' } })
  await recheck(status).click()
  await status.getByText('Проблема осталась').waitFor()
  assert.deepEqual(posts[1], { pathname: '/api/alert-rechecks', body: { object_type: 'check', object_id: 10, kind: 'status' } })

  await ssl.locator('.quick-mute').click()
  await snapshot('sites-320-snooze')
  assert(await ssl.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), 'Open snooze options overflow')
  await ssl.getByRole('button', { name: 'за 7 дн.', exact: true }).click()
  await card.getByRole('button', { name: /отложено: 1/ }).click()
  await ssl.getByRole('button', { name: 'вернуть', exact: true }).click()
  await ssl.locator('.quick-mute').waitFor()
  assert.deepEqual(posts.slice(2), [7, null].map(mode => ({ pathname: '/api/checks/11/expiry-snooze', body: { kind: 'ssl', mode } })))

  await grouped.locator('.home-open').click()
  await page.waitForURL((url) => url.searchParams.get('check') === '1')
  assert.deepEqual(errors, [], 'Browser runtime errors')
  console.log('PASS grouped domain, status and SSL alerts, two-column and mobile layout, recheck, snooze, restore and navigation')
} finally {
  await browser.close()
}
