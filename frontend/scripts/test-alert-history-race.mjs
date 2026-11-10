// Run against a local frontend. All API responses are synthetic.
import assert from 'node:assert/strict'
import { chromium } from 'playwright-core'

const base = new URL(process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5334')
assert(['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname))
const browser = await chromium.launch({ executablePath: process.env.PW_CHROME, headless: true })
const page = await browser.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(e.message))

const summary = { total: 1, fires: 1, recoveries: 0, kinds: [], targets: [], items: [], more_items: 0 }
function data(id, text, more = true) {
  return { more, summary, events: [{
    id, text, ts: '2026-01-01T11:00:00Z', kind: 'test', label: 'Demo',
    target: 'Demo target', section: '', recovery: false,
  }] }
}

function hold() {
  return { seen: Promise.withResolvers(), reply: Promise.withResolvers(), sent: Promise.withResolvers() }
}
const oldSuccess = hold(), freshPage = hold(), oldError = hold()
let weekReads = 0, monthPages = 0
try {
  await page.addInitScript(() => {
    localStorage.setItem('kervax_token', 'synthetic-ui-fixture')
    localStorage.setItem('kervax_lang', 'ru')
  })
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== base.origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (url.pathname === '/api/alert-history') {
      const { days, only, before_id: before } = Object.fromEntries(url.searchParams)
      if (!only) return route.fulfill({ json: { summary, events: [], more: false } })
      if (!before) {
        const id = days === '7' ? (++weekReads === 1 ? 10 : 30) : 20
        return route.fulfill({ json: data(id, `First page ${id}`) })
      }
      const pending = before === '10' ? oldSuccess
        : before === '30' ? freshPage : ++monthPages === 1 ? oldError : null
      if (pending) {
        pending.seen.resolve()
        const response = await pending.reply.promise
        // A canceled browser request may disappear before Playwright can fulfill it.
        await route.fulfill(response).catch(() => undefined)
        pending.sent.resolve()
        return
      }
      if (monthPages === 2) return route.fulfill({ status: 500, json: { detail: 'Current page error' } })
      return route.fulfill({ json: data(19, 'Successful retry', false) })
    }
    const defaults = {
      '/api/auth/me': { id: 1, username: 'fixture', role: 'admin' },
      '/api/servers': [], '/api/maintenance': [], '/api/alert-rechecks': [],
      '/api/checks/overview': { total: 0, checks: [], up: 0, down: 0, open_incidents: 0 },
      '/api/alerts/coverage': { items: [] },
    }
    return route.fulfill({ json: defaults[url.pathname] ?? {} })
  })
  await page.goto(base.href)
  await page.getByRole('button', { name: 'Меню', exact: true }).click()
  await page.getByRole('button', { name: 'История алертов', exact: true }).click()
  const dialog = page.locator('.alerts-modal')
  await dialog.getByRole('button', { name: /Все сообщения/ }).click()
  const more = dialog.getByRole('button', { name: 'Показать раньше', exact: true })
  const week = dialog.getByRole('button', { name: '7 дней', exact: true })
  const month = dialog.getByRole('button', { name: '30 дней', exact: true })
  const render = () => page.evaluate(() => new Promise((resolve) =>
    requestAnimationFrame(() => requestAnimationFrame(resolve))))

  await dialog.getByText('First page 10').waitFor()
  await more.click()
  await oldSuccess.seen.promise
  await month.click()
  await dialog.getByText('First page 20').waitFor()
  await week.click()
  await dialog.getByText('First page 30').waitFor()
  await more.click({ timeout: 3000 })
  await freshPage.seen.promise
  oldSuccess.reply.resolve({ json: data(9, 'Discarded old page', false) })
  await oldSuccess.sent.promise
  await render()
  assert.equal(await dialog.getByText('Discarded old page').count(), 0)
  assert(await dialog.getByRole('button', { name: '...', exact: true }).isDisabled())
  freshPage.reply.resolve({ json: data(29, 'Current older page', false) })
  await dialog.getByText('Current older page').waitFor()

  await month.click()
  await dialog.getByText('First page 20').waitFor()
  await more.click()
  await oldError.seen.promise
  await week.click()
  await dialog.getByText('First page 30').waitFor()
  oldError.reply.resolve({ status: 500, json: { detail: 'Discarded old error' } })
  await oldError.sent.promise
  await render()
  assert.equal(await dialog.getByText('Discarded old error').count(), 0)

  await month.click()
  await dialog.getByText('First page 20').waitFor()
  await more.click()
  const err = dialog.getByRole('alert').filter({ hasText: 'Current page error' })
  await err.waitFor()
  await more.click()
  await dialog.getByText('Successful retry').waitFor()
  await err.waitFor({ state: 'detached' })
  assert.deepEqual(errors, [])
  console.log('PASS alert history discards stale pages and errors, then retries a current failure')
} finally {
  for (const pending of [oldSuccess, freshPage, oldError]) pending.reply.resolve({ json: {} })
  await browser.close()
}
