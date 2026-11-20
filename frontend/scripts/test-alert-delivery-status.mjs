// Local browser regression. API fixtures are synthetic and every external request is blocked.
import assert from 'node:assert/strict'
import { chromium } from 'playwright-core'

const base = new URL(process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5354')
assert(['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname))
const browser = await chromium.launch({ executablePath: process.env.PW_CHROME, headless: true })
const failures = []
let role = 'admin', queued = false, reviewed = false, retryCalls = 0, reviewCalls = 0, deliveryReads = 0
let currentPage
const counts = () => ({ pending: Number(queued), sending: 0, confirmed: 0, uncertain: Number(!queued),
  rejected: 0, superseded: 0, attention: 1 })
const config = { telegram_token: '', telegram_chat: '', telegram_api: '', webhook: '', flood_threshold: 6,
  enabled: false, muted: false, tg_buttons: false, tz: 'UTC', tg_poll: { status: 'off' },
  escalation: { enabled: false, after_min: 15, repeat: 2, kinds: [], users: [], chat: '' },
  esc_candidates: [], routes: { main: 'all', chats: [] }, route_groups: { servers: [], sites: [] },
  routes_state: {}, dup_pause: { on: false } }

async function pageFor(currentRole) {
  role = currentRole
  const page = await browser.newPage()
  currentPage = page
  page.on('pageerror', error => failures.push(error.message))
  await page.addInitScript(() => {
    localStorage.setItem('kervax_token', 'synthetic-ui-fixture')
    localStorage.setItem('kervax_lang', 'ru')
  })
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== base.origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (url.pathname === '/api/alerts/deliveries/retry') {
      retryCalls++
      assert.deepEqual(route.request().postDataJSON(), { ids: [1], acknowledge_uncertain: true })
      queued = true
      return route.fulfill({ json: { queued_ids: [1], skipped_ids: [], delivery_confirmed: false } })
    }
    if (url.pathname === '/api/alerts/deliveries/review') {
      reviewCalls++
      assert.deepEqual(route.request().postDataJSON(), { ids: [2] })
      reviewed = true
      return route.fulfill({ json: { reviewed_ids: [2], skipped_ids: [] } })
    }
    if (url.pathname === '/api/alerts/deliveries') {
      deliveryReads++
      const filter = url.searchParams.get('state')
      const uncertain = {
        id: 1, event_id: 1, kind: 'queue', target: 'Example queue', phase: 'fire', status: 'uncertain',
        attempts: 1, uncertain_count: 1, created_at: '2026-01-01T11:00:00Z', occurred_at: '2026-01-01T11:00:00Z',
        next_attempt_at: '2026-01-01T11:15:00Z', lease_until: null, can_retry: true, needs_confirmation: true,
        reviewed_at: null, reviewed_by: null, can_review: false,
      }
      const terminal = {
        ...uncertain, id: 2, target: 'Example terminal', status: 'attention', attempts: 3, uncertain_count: 3,
        can_review: !reviewed, can_retry: !reviewed,
        reviewed_at: reviewed ? '2026-01-01T12:00:00Z' : null, reviewed_by: reviewed ? 'fixture' : null,
      }
      let items = []
      if (url.searchParams.get('summary') !== 'true') {
        if (!queued && filter !== 'reviewed') items.push(uncertain)
        if (filter === 'all' || reviewed === (filter === 'reviewed')) items.push(terminal)
      }
      return route.fulfill({ json: { counts: counts(), bad_count: Number(!queued) + Number(!reviewed), items, more: false } })
    }
    const fixtures = {
      '/api/auth/me': { id: 1, username: 'fixture', role, sections: [] },
      '/api/servers': [], '/api/maintenance': [], '/api/alert-rechecks': [],
      '/api/checks/overview': { total: 0, checks: [], up: 0, down: 0, open_incidents: 0 },
      '/api/checks/discovered': { domains: [], hosts: {}, groups: [], ignored: [] },
      '/api/checks/local-probe-suggestions': { items: [] },
      '/api/alerts/coverage': { items: [] }, '/api/panel-errors/count': { open: 0, reports: 0 },
      '/api/alerts': config, '/api/alerts/server-rules': { rules: {}, kinds: [] },
      '/api/alerts/site-rules': { rules: {}, kinds: [] },
    }
    return route.fulfill({ json: fixtures[url.pathname] ?? {} })
  })
  await page.goto(base.href)
  return page
}

try {
  const page = await pageFor('admin')
  await page.getByText('Проблемы доставки уведомлений: 2', { exact: true }).waitFor()
  await page.getByRole('button', { name: 'Открыть доставку', exact: true }).click()
  const dialog = page.locator('.alerts-modal')
  const terminal = dialog.locator('.card').filter({ hasText: 'Example terminal' })
  await terminal.getByRole('button', { name: 'Отметить рассмотренным', exact: true }).click()
  await dialog.getByText('Рассмотрение записано.', { exact: true }).waitFor()
  await terminal.waitFor({ state: 'hidden' })
  assert.equal(reviewCalls, 1)
  assert.equal(retryCalls, 0)
  await dialog.getByRole('button', { name: 'Рассмотренные доставки', exact: true }).click()
  await terminal.getByText('Доставка не подтверждена', { exact: true }).waitFor()
  await terminal.getByText('Попыток: 3, без подтверждения: 3', { exact: true }).waitFor()
  await terminal.getByText(/Рассмотрено пользователем fixture/).waitFor()
  assert.equal(await terminal.getByRole('button', { name: 'Повторить доставку', exact: true }).count(), 0)
  await dialog.getByRole('button', { name: 'Проблемы доставки (1)', exact: true }).click()
  await dialog.getByText('Example queue', { exact: true }).waitFor()
  await dialog.getByRole('button', { name: 'Повторить доставку', exact: true }).click()
  await dialog.getByText('Предыдущая доставка не подтверждена, повтор может прислать копию', { exact: true }).waitFor()
  assert.equal(retryCalls, 0)
  await page.setViewportSize({ width: 390, height: 844 })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false)
  if (process.env.KERVAX_TEST_SCREENSHOT) await page.screenshot({ path: process.env.KERVAX_TEST_SCREENSHOT })
  await dialog.getByRole('button', { name: 'Поставить повтор в очередь', exact: true }).click()
  await dialog.getByText('Повтор поставлен в очередь. Доставка еще не подтверждена.', { exact: true }).waitFor()
  assert.equal(retryCalls, 1)
  await dialog.getByText('Нет проблем с доставкой', { exact: true }).waitFor()
  await page.close()
  const previousReads = deliveryReads
  const viewer = await pageFor('viewer')
  await viewer.getByRole('button', { name: 'Меню', exact: true }).click()
  await viewer.getByText('Только просмотр', { exact: true }).waitFor()
  assert.equal(await viewer.getByText('Проблемы доставки уведомлений: 1', { exact: true }).count(), 0)
  assert.equal(await viewer.getByRole('button', { name: 'Алерты', exact: true }).count(), 0)
  assert.equal(deliveryReads, previousReads)
  assert.deepEqual(failures, [])
  console.log('delivery status: review without retry, preserved outcome, explicit retry, mobile layout and viewer privacy passed')
} catch (error) {
  console.error('page errors', failures)
  console.error((await currentPage?.locator('body').innerText())?.slice(0, 3000))
  throw error
} finally {
  await browser.close()
}
