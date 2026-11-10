// Run against the local frontend. All API responses are synthetic.
import assert from 'node:assert/strict'
import { chromium } from 'playwright-core'

const base = new URL(process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5334').origin
assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Use a local frontend')
const browser = await chromium.launch({ headless: true, executablePath: process.env.PW_CHROME })
const context = await browser.newContext()
const errors = []
context.on('page', (page) => page.on('pageerror', (e) => errors.push(e.message)))
const defaults = {
  '/api/checks/overview': { total: 0, up: 0, down: 0, open_incidents: 0, checks: [] },
  '/api/alert-rechecks': [],
  '/api/maintenance': [],
  '/api/servers': [],
  '/api/checks/discovered': { domains: [], hosts: {}, groups: [], ignored: [] },
  '/api/checks/local-probe-suggestions': { items: [] },
  '/api/alerts/coverage': { items: [] },
}
const accounts = {
  'fixture-first': { id: 1, username: 'fixture-first', role: 'admin' },
  // SQLite can reuse the deleted user's numeric id.
  'fixture-second': { id: 1, username: 'fixture-second', role: 'viewer' },
  'fixture-view': { id: 1, username: 'fixture-first', role: 'editor', impersonator: 'fixture-admin',
    imp_expires: new Date(Date.now() + 60000).toISOString() },
}
const staleStarted = Promise.withResolvers()
const finishStale = Promise.withResolvers()
const issued = []
let logoutCount = 0
await context.route('**/*', async (route) => {
  const request = route.request()
  const url = new URL(request.url())
  if (url.origin !== base) return route.abort()
  if (!url.pathname.startsWith('/api/')) return route.continue()
  const who = (request.headers().authorization ?? '').replace(/^Bearer /, '')
  if (url.pathname === '/api/auth/login') {
    const { username } = request.postDataJSON()
    assert(accounts[username])
    return route.fulfill({ json: { access_token: username } })
  }
  if (url.pathname === '/api/auth/logout') {
    assert.equal(who, 'fixture-first')
    logoutCount++
    return route.fulfill({ status: 204 })
  }
  if (url.pathname === '/api/auth/me') {
    return accounts[who]
      ? route.fulfill({ json: accounts[who] })
      : route.fulfill({ status: 401, json: { detail: 'Missing fixture session' } })
  }
  if (url.pathname === '/api/search') {
    const q = url.searchParams.get('q')
    issued.push({ q, who })
    if (q === 'older-query') {
      staleStarted.resolve()
      await finishStale.promise
      return route.fulfill({ status: 401, json: { detail: 'Synthetic old refusal' } })
    }
    if (q === 'failing-query') return route.fulfill({ status: 500, json: { detail: 'Synthetic search failure' } })
    return route.fulfill({ json: { q, groups: [{
      kind: 'server', total: 1,
      items: [{ title: `Result for ${q}`, id: 1, section: 'home', sub: [], sec: '', tag: '', hint_k: '', hint_v: '' }],
    }] } })
  }
  return route.fulfill({ json: defaults[url.pathname] ?? {} })
})

const searchDialog = (page) => page.getByRole('dialog', { name: 'Поиск' })
async function openSearch(page) {
  await page.getByRole('button', { name: 'Поиск', exact: true }).click()
  return searchDialog(page).getByRole('textbox', { name: 'Поиск' })
}
async function login(page, username) {
  await page.getByLabel('Логин').fill(username)
  await page.getByLabel('Пароль', { exact: true }).fill('synthetic fixture value')
  await page.getByRole('button', { name: 'Войти', exact: true }).click()
  await page.getByRole('button', { name: 'Поиск', exact: true }).waitFor()
}

try {
  const first = await context.newPage()
  await first.goto(base)
  await login(first, 'fixture-first')
  const input = await openSearch(first)
  const dialog = searchDialog(first)
  await input.fill('failing-query')
  await dialog.getByRole('alert').getByText('Synthetic search failure').waitFor()
  await input.fill('')
  assert.equal(await dialog.getByRole('alert').count(), 0, 'An error is only shown for its query')
  await input.fill('older-query')
  await staleStarted.promise
  await input.fill('private-first.example.test')
  const result = dialog.getByRole('button', { name: 'Result for private-first.example.test' })
  await result.waitFor()
  const oldResponse = first.waitForResponse((response) => response.url().includes('older-query'))
  finishStale.resolve()
  await (await oldResponse).finished()
  await result.click()
  await dialog.waitFor({ state: 'detached' })

  const second = await context.newPage()
  await second.goto(base)
  await openSearch(second)
  const recent = searchDialog(second).getByRole('button', { name: 'private-first.example.test' })
  await recent.focus()
  await recent.press('Escape')
  await searchDialog(second).waitFor({ state: 'detached' })
  await openSearch(second)
  await recent.waitFor()
  await first.getByRole('button', { name: 'Меню' }).click()
  await first.getByRole('button', { name: 'Выйти', exact: true }).click()
  await first.getByRole('heading', { name: 'Вход в панель' }).waitFor()
  await second.getByRole('heading', { name: 'Вход в панель' }).waitFor()
  assert.equal(logoutCount, 1)
  assert.equal(await recent.count(), 0, 'Logging out clears the other tab without waiting for a poll')

  await login(first, 'fixture-second')
  await openSearch(second)
  assert.equal(await recent.count(), 0, 'A different account must not see old queries')
  await second.evaluate(() => {
    localStorage.setItem('kervax_admin_token', 'fixture-second')
    localStorage.setItem('kervax_imp_id', '1')
    localStorage.setItem('kervax_token', 'fixture-view')
  })
  await first.getByRole('button', { name: 'Вернуться к себе' }).waitFor()
  await openSearch(first)
  assert.equal(await searchDialog(first).getByRole('button', { name: 'private-first.example.test' }).count(), 0,
    'Viewing as a different user must not expose that account’s browser history')
  assert.deepEqual(issued, ['failing-query', 'older-query', 'private-first.example.test'].map((q) => ({ q, who: 'fixture-first' })))
  assert.deepEqual(errors, [], 'Browser runtime errors')
  console.log('PASS search error scope, stale rejection, Escape, cross-tab logout and account history isolation')
} finally {
  await browser.close()
}
