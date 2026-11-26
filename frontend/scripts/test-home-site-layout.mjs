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
checks.push(check(11, sslName, { target: 'https://ssl.example.test', check_ssl: true, probe_local: true, ssl_days: 14, ssl_warn_days: [14] }))
const now = Date.now()
const hour = 3600_000
const time = (offset) => new Date(now + offset * hour).toISOString()
const maintenance = [
  { id: 1, title: 'Demo maintenance', active: true, starts_at: time(-1), ends_at: time(1), cur_end: time(1), next_start: null },
  { id: 2, title: 'Demo upcoming window', active: false, starts_at: time(2), ends_at: time(3), cur_end: null, next_start: time(2) },
].map((window) => ({
  server_ids: [42], check_ids: [1], finished: false, repeat: '', weekdays: [], verify: true, note: '', created_by: 'fixture',
  ...window,
}))
const servers = [{
  id: 42, name: 'Demo server', hostname: 'legacy-node', online: true, enabled: true, group_name: '',
  agent_version: '2.10', agent_can_update: true, helper_rollout: true, skip_kervax_updates: false,
  last_seen: time(0), last_report: {}, problems: [],
}]

const browser = await chromium.launch({ headless: true, executablePath: process.env.PW_CHROME })
try {
  const page = await browser.newPage({ viewport: { width: 850, height: 900 } })
  page.setDefaultTimeout(8000)
  const errors = [], posts = [], jobs = [], changes = []
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
      if (pathname === '/api/panel-errors/front') errors.push(body.message)
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
    if (pathname === '/api/servers/42' && request.method() === 'PATCH') {
      const body = request.postDataJSON()
      changes.push(body.skip_kervax_updates)
      servers[0] = { ...servers[0], ...body, helper_rollout: !body.skip_kervax_updates }
      return route.fulfill({ json: servers[0] })
    }
    const data = {
      '/api/servers/42': servers[0],
      '/api/servers/42/metrics': [],
      '/api/servers/42/timeline': { since: time(-24), until: time(0), events: [], cats: {} },
      '/api/servers/agent-release': { version: '2.71' },
      '/api/servers/fleet-rollout': servers[0].skip_kervax_updates ? {} : {
        helpers: { hosts: ['legacy-node'], at: time(-1) },
      },
      '/api/auth/me': { id: 1, username: 'fixture', role: 'admin' },
      '/api/checks': checks,
      '/api/checks/overview': { total: checks.length, up: checks.length - 1, down: 1, open_incidents: 0, checks },
      '/api/alert-rechecks': jobs,
      '/api/maintenance': maintenance,
      '/api/servers': servers,
      '/api/checks/discovered': { domains: [], hosts: {}, groups: [], ignored: [] },
      '/api/checks/local-probe-suggestions': { items: [] },
      '/api/alerts/coverage': { items: [] },
    }[pathname] ?? {}
    return route.fulfill({ json: data })
  })

  await page.goto(base)
  const queue = page.locator('#home-issue-queue')
  const card = queue.locator('.home-queue-inspector')
  const row = (name) => queue.locator('button.home-queue-row').filter({ hasText: name })
  const grouped = row(domain)
  const status = row(statusName)
  const ssl = row(sslName)
  const snapshot = async (name) => { if (out) await queue.screenshot({ path: path.join(out, `${name}.png`) }) }
  const recheck = card.getByRole('button', { name: 'Исправил: перепроверить', exact: true })
  const snooze = card.getByRole('button', { name: 'Напомнить позже или не следить за сертификатом', exact: true })

  await queue.locator('details.home-queue-warnings > summary').click()
  await grouped.waitFor()
  await ssl.waitFor()
  for (const width of [850, 1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 })
    await grouped.click()
    await card.getByText('доступно 1 из 9').waitFor()
    await card.getByText(domain, { exact: true }).waitFor()
    await snapshot(`sites-${width}`)
    const name = await grouped.locator('.home-queue-row-meta').evaluate((element) => {
      const range = document.createRange()
      range.selectNodeContents(element)
      return new Set([...range.getClientRects()].map(rect => rect.y)).size
    })
    assert.equal(name, 1, `The queue must keep the domain metadata on one line at ${width}px`)
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Page overflow at ${width}px`)
    assert(await queue.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), `Site queue overflow at ${width}px`)
    assert(await card.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), `Site inspector overflow at ${width}px`)
    for (const item of [grouped, status, ssl]) {
      assert(await item.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), `Site alert overflow at ${width}px`)
    }
  }
  await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
  await snapshot('sites-320-light')
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })

  await recheck.click()
  await card.getByText('Проблема осталась').waitFor()
  assert.deepEqual(posts[0], { pathname: '/api/alert-rechecks', body: { object_type: 'check', object_id: 1, kind: 'domain' } })
  await status.click()
  await recheck.click()
  await card.getByText('Проблема осталась').waitFor()
  assert.deepEqual(posts[1], { pathname: '/api/alert-rechecks', body: { object_type: 'check', object_id: 10, kind: 'status' } })

  await ssl.click()
  await snooze.waitFor()
  assert.equal(await recheck.count(), 0, 'Unavailable recheck button must be absent')
  await snooze.press('Enter')
  await snapshot('sites-320-snooze')
  assert(await card.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), 'Open snooze options overflow')
  await card.getByRole('button', { name: 'напомнить за 7 дней', exact: true }).click()
  await ssl.waitFor({ state: 'detached' })
  await card.waitFor({ state: 'hidden' })
  const snoozed = page.locator('.home-disclosure').filter({ has: page.locator('summary', { hasText: 'Заглушенные сроки сайтов' }) })
  await snoozed.locator('summary').click()
  await snoozed.locator('.home-attention-row').filter({ hasText: sslName }).getByRole('button', { name: 'вернуть', exact: true }).click()
  await ssl.click()
  await snooze.waitFor()
  assert.deepEqual(posts.slice(2), [7, null].map(mode => ({ pathname: '/api/checks/11/expiry-snooze', body: { kind: 'ssl', mode } })))

  await grouped.click()
  await card.getByRole('button', { name: 'Открыть объект', exact: true }).click()
  await page.waitForURL((url) => url.searchParams.get('check') === '1')
  assert.deepEqual(errors, [], 'Browser runtime errors')

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(base)
  await page.getByRole('button', { name: 'Меню', exact: true }).click()
  await page.getByRole('button', { name: 'Окна обслуживания', exact: true }).click()
  await page.getByRole('heading', { name: 'Окна обслуживания', exact: true }).waitFor()
  for (const { title } of maintenance) {
    await page.getByRole('row').filter({ hasText: title }).waitFor()
  }
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click()
  const activeChip = page.locator('.note-chip[title*="Demo maintenance"]').filter({ hasText: '🚧' })
  for (const section of ['Сайты', 'Серверы']) {
    await page.locator('.topnav').getByRole('button', { name: section, exact: true }).click()
    await activeChip.waitFor()
    assert.equal(await page.locator('.note-chip[title*="Demo upcoming window"]').count(), 0)
  }
  await page.getByTitle('На главную', { exact: true }).click()
  await queue.waitFor()
  for (const { title } of maintenance) {
    assert.equal(await page.getByText(title, { exact: false }).count(), 0, 'Maintenance windows must stay off the home page')
  }
  assert.equal(await activeChip.count(), 0)
  assert.deepEqual(errors, [], 'Browser runtime errors after opening maintenance and lists')
  console.log('PASS grouped domain, status and SSL alerts, queue layout down to 320px, recheck, snooze, restore and navigation')
  console.log('PASS maintenance stays off home, active and upcoming windows remain in the schedule, both lists show active maintenance')

  const updates = page.locator('.home-disclosure').filter({ has: page.locator('summary', { hasText: 'Обновления' }) })
  await updates.locator('summary').click()
  const host = updates.getByRole('button', { name: 'legacy-node', exact: true })
  const editSettings = async () => {
    await page.getByTitle('Изменить', { exact: true }).click()
    await option.waitFor()
  }
  const option = page.getByRole('checkbox', { name: 'Пропускать обновления Kervax', exact: true })
  const save = page.getByRole('button', { name: 'Сохранить', exact: true })
  await host.click()
  await page.getByTitle('Обновить агент этой ноды (проверит подпись и хеш)').waitFor()
  await editSettings()
  assert.equal(await option.isChecked(), false)
  await option.check()
  await save.click()
  await option.waitFor({ state: 'hidden' })
  await page.getByText('Обновления пропускаются', { exact: true }).waitFor()
  assert.equal(await page.getByTitle('Обновить агент этой ноды (проверит подпись и хеш)').count(), 0)
  await editSettings()
  assert.equal(await option.isChecked(), true, 'The opt-out is saved on the server')
  await page.locator('.alerts-modal').getByRole('button', { name: 'Закрыть', exact: true }).click()
  await page.locator('.detail-server').getByTitle('Закрыть', { exact: true }).click()
  await page.getByTitle('На главную', { exact: true }).click()
  await queue.waitFor()
  assert.equal(await updates.count(), 0, 'An excluded server must not keep a pending update')
  await page.goto(new URL('/?server=42', base).href)
  await editSettings()
  await option.uncheck()
  await save.click()
  await option.waitFor({ state: 'hidden' })
  await page.locator('.detail-server').getByTitle('Закрыть', { exact: true }).click()
  await page.getByTitle('На главную', { exact: true }).click()
  await updates.locator('summary').click()
  await host.waitFor()
  assert.deepEqual(changes, [true, false])
  assert.deepEqual(errors, [], 'Browser runtime errors while changing update policy')
  console.log('PASS skipped updates leave the home queue, persist across navigation, and return when enabled')
} finally {
  await browser.close()
}
