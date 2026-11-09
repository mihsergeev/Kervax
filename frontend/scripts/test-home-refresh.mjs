// Run against the local frontend. API responses are intercepted and use synthetic fixtures.
import assert from 'node:assert/strict'
import { chromium } from 'playwright-core'

const base = new URL(process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5334').origin
assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Use a local frontend')
const browser = await chromium.launch({ headless: true, executablePath: process.env.PW_CHROME })
const check = {
  id: 10, name: 'Demo monitor', type: 'http', target: 'https://monitor.example.test',
  enabled: true, group_name: '', last_status: 'down', last_message: 'HTTP 503',
  check_ssl: false, check_domain: false,
}
const overview = { total: 1, up: 0, down: 1, open_incidents: 0, checks: [check] }
const defaults = {
  '/api/auth/me': { id: 1, username: 'fixture', role: 'admin' },
  '/api/checks/overview': overview,
  '/api/alert-rechecks': [],
  '/api/maintenance': [],
  '/api/servers': [],
  '/api/checks/discovered': { domains: [], hosts: {}, groups: [], ignored: [] },
  '/api/checks/local-probe-suggestions': { items: [] },
  '/api/alerts/coverage': { items: [] },
}

async function fixture(handler, verify) {
  const page = await browser.newPage({ viewport: { width: 1400, height: 950 } })
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  try {
    await page.clock.install()
    await page.addInitScript(() => {
      localStorage.setItem('kervax_token', 'synthetic-ui-fixture')
      localStorage.setItem('kervax_lang', 'ru')
    })
    await page.route('**/*', (route) => {
      const url = new URL(route.request().url())
      if (url.origin !== base) return route.abort()
      if (!url.pathname.startsWith('/api/')) return route.continue()
      return handler(route, url.pathname, () => route.fulfill({ json: defaults[url.pathname] ?? {} }))
    })
    await page.goto(base)
    await verify(page, page.locator('#home-issue-queue'))
    assert.deepEqual(errors, [], 'Browser runtime errors')
  } finally { await page.close() }
}

async function staleRecheck() {
  const ref = { object_type: 'check', object_id: check.id, kind: 'status' }
  const old = { ...ref, id: '1'.repeat(32), status: 'remains', message: '', created_at: '2026-01-01T10:00:00Z' }
  const fresh = { ...old, id: '2'.repeat(32), status: 'checking', created_at: '2026-01-01T10:01:00Z' }
  const staleStarted = Promise.withResolvers(), finishStale = Promise.withResolvers()
  const nextStarted = Promise.withResolvers(), finishNext = Promise.withResolvers()
  let checking = false, polls = 0, overviewReads = 0, fixed = false
  await fixture(async (route, pathname, fallback) => {
    if (pathname === '/api/checks/overview') {
      overviewReads++
      if (fixed) return route.fulfill({ json: { ...overview, up: 1, down: 0, checks: [{ ...check, last_status: 'up' }] } })
    }
    if (pathname !== '/api/alert-rechecks') return fallback()
    if (route.request().method() === 'POST') {
      assert.deepEqual(route.request().postDataJSON(), ref)
      return route.fulfill({ json: fresh })
    }
    if (!checking) return route.fulfill({ json: [old] })
    if (++polls === 1) {
      staleStarted.resolve()
      await finishStale.promise
      return route.fulfill({ json: [old] })
    }
    nextStarted.resolve()
    await finishNext.promise
    fixed = true
    return route.fulfill({ json: [{ ...fresh, status: 'fixed' }] })
  }, async (page, queue) => {
    const button = queue.getByRole('button', { name: 'Исправил: перепроверить', exact: true })
    const summary = page.locator('.home-recheck-summary')
    await button.getByText('Проблема осталась').waitFor()
    const readsBefore = overviewReads
    checking = true
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/api/alert-rechecks') && r.request().method() === 'POST'),
      button.click(), staleStarted.promise,
    ])
    const staleResponse = page.waitForResponse((r) => r.url().endsWith('/api/alert-rechecks') && r.request().method() === 'GET')
    finishStale.resolve()
    await (await staleResponse).finished()
    await page.clock.runFor(2100)
    await Promise.race([nextStarted.promise, page.waitForTimeout(3000).then(() => assert.fail('The current job stopped polling'))])
    assert(await button.isDisabled(), 'The previous job must not complete the current check')
    assert.equal(overviewReads, readsBefore, 'The previous job must not refresh the queue')
    finishNext.resolve()
    await summary.getByText('подтверждено исправление: 1', { exact: false }).waitFor()
    await queue.getByText('Нет активных проблем').waitFor()
  })
  console.log('PASS stale recheck result discarded and current job confirmed')
}

async function overviewErrors() {
  let failing = '/api/checks/overview'
  await fixture((route, pathname, fallback) => pathname === failing
    ? route.fulfill({ status: 503, json: { detail: 'Synthetic upstream failure' } })
    : fallback(), async (page, queue) => {
    const sites = page.getByRole('button', { name: /Сайты.*Данные недоступны/ })
    const siteError = queue.getByRole('status').filter({ hasText: 'Не удалось обновить данные о сайтах' })
    await siteError.waitFor()
    await sites.waitFor()
    assert.equal(await queue.getByText('Нет активных проблем').count(), 0)
    failing = ''
    await queue.getByRole('button', { name: 'Повторить запрос' }).click()
    await queue.getByText(check.name, { exact: true }).waitFor()
    await siteError.waitFor({ state: 'detached' })

    failing = '/api/checks/overview'
    await page.clock.fastForward(15_000)
    await siteError.waitFor()
    assert.match(await siteError.innerText(), /Данные на /)
    assert(await queue.getByText(check.name, { exact: true }).isVisible(), 'The last good result must stay visible')
    await page.getByRole('button', { name: /Сайты.*данные устарели/ }).waitFor()

    failing = '/api/servers'
    await page.clock.fastForward(15_000)
    await siteError.waitFor({ state: 'detached' })
    const serverError = queue.getByRole('status').filter({ hasText: 'Не удалось обновить данные о серверах' })
    await serverError.waitFor()
    assert(await queue.getByText(check.name, { exact: true }).isVisible(), 'Server failure must preserve the site queue')
    failing = ''
    await queue.getByRole('button', { name: 'Повторить запрос' }).click()
    await serverError.waitFor({ state: 'detached' })
  })
  console.log('PASS initial failure, stale data with a timestamp, independent sources and retry')
}

async function retireRace() {
  const id = 42, port = 1443, since = '2026-01-01T10:00:00Z'
  const problem = { kind: 'listen_gone', level: 1, text: 'Demo listener missing', sec: 'exposure', since, listen_port: port }
  const other = { ...problem, text: 'Second demo listener missing', listen_port: 2443 }
  const old = {
    id, name: 'Demo TCP host', online: true, group_name: '', agent_version: '9.9.0',
    last_seen: new Date().toISOString(), last_report: {}, problems: [problem, other],
  }
  const recurred = { ...old, problems: [{ ...problem, since: '2026-01-01T11:00:00Z', text: 'Demo listener stopped again' }] }
  const staleStarted = Promise.withResolvers(), finishStale = Promise.withResolvers()
  const retireStarted = Promise.withResolvers(), finishRetire = Promise.withResolvers()
  let phase = 'initial'
  await fixture(async (route, pathname, fallback) => {
    if (pathname === `/api/servers/${id}/listen/${port}/retire`) {
      assert.equal(route.request().method(), 'POST')
      assert.deepEqual(route.request().postDataJSON(), { missed_since: since })
      retireStarted.resolve()
      await finishRetire.promise
      return route.fulfill({ json: { ...old, problems: [other] } })
    }
    if (pathname === `/api/servers/${id}/listen/${other.listen_port}/retire`)
      return route.fulfill({ json: { ...old, problems: [] } })
    if (pathname !== '/api/servers') return fallback()
    if (phase === 'recurred') return route.fulfill({ json: [recurred] })
    if (phase === 'stale') {
      staleStarted.resolve()
      await finishStale.promise
    }
    return route.fulfill({ json: [old] })
  }, async (page, queue) => {
    const button = queue.getByRole('button', { name: 'Порт больше не нужен', exact: true })
    await button.first().waitFor()
    phase = 'stale'
    await page.clock.fastForward(15_000)
    await staleStarted.promise
    await button.first().click()
    await retireStarted.promise
    await button.last().click()
    await queue.getByText(other.text, { exact: false }).waitFor({ state: 'detached' })
    finishRetire.resolve()
    await button.first().waitFor({ state: 'detached' })
    const staleResponse = page.waitForResponse((r) => r.url().endsWith('/api/servers'))
    finishStale.resolve()
    await (await staleResponse).finished()
    await page.clock.runFor(500)
    assert.equal(await button.count(), 0, 'Neither a preceding refresh nor an out-of-order retirement may restore a retired alert')
    phase = 'recurred'
    await page.clock.fastForward(15_000)
    await queue.getByText('Demo listener stopped again', { exact: false }).waitFor()
  })
  console.log('PASS retirement with a stale refresh, out-of-order responses and a later incident')
}

try {
  const results = await Promise.allSettled([staleRecheck(), overviewErrors(), retireRace()])
  const failures = results.flatMap((result) => result.status === 'rejected' ? [result.reason] : [])
  if (failures.length) throw new AggregateError(failures, 'Home refresh regressions')
} finally { await browser.close() }
