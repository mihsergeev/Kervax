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

async function missingRecheck() {
  const job = {
    object_type: 'check', object_id: check.id, kind: 'status', id: '3'.repeat(32),
    status: 'checking', message: '', created_at: '2026-01-01T10:00:00Z',
  }
  let exactPolls = 0
  await fixture((route, pathname, fallback) => {
    if (pathname !== '/api/alert-rechecks') return fallback()
    if (route.request().method() === 'POST') return route.fulfill({ json: job })
    const ids = new URL(route.request().url()).searchParams.getAll('id')
    if (ids.length) {
      assert.deepEqual(ids, [job.id])
      exactPolls++
    }
    return route.fulfill({ json: [] })
  }, async (page, queue) => {
    const button = queue.getByRole('button', { name: 'Исправил: перепроверить', exact: true })
    await button.click()
    await page.locator('.home-recheck-summary').getByText('Проверяю свежие данные…', { exact: false }).waitFor()
    await page.clock.runFor(2100)
    await button.getByText('Не удалось проверить').waitFor()
    assert(await button.isEnabled(), 'An inaccessible job must not block retries')
    assert(await queue.getByText(check.name, { exact: true }).isVisible(), 'No result cannot clear the alert')
    await page.clock.runFor(4500)
    assert.equal(exactPolls, 1, 'The terminal unavailable job must stop polling')
  })
  console.log('PASS inaccessible recheck stops polling and leaves the alert visible')
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

async function parallelMutes() {
  const id = 42
  const problems = [
    { kind: 'docker_restarts', level: 1, text: 'Demo container restarts', mute: 'restarts:docker:demo' },
    { kind: 'mem_leak', level: 1, text: 'Demo memory leak', mute: 'leak:proc:demo' },
    { kind: 'disk_health', level: 1, text: 'Demo disk read errors', mute: 'disk_health' },
    { kind: 'disk_health', level: 1, text: 'Demo disk write errors', mute: 'disk_health' },
  ]
  const host = { id, name: 'Demo host', online: true, last_report: {}, problems }
  const firstStarted = Promise.withResolvers(), finishFirst = Promise.withResolvers()
  const writes = []
  let mutes = ['offline']
  await fixture(async (route, pathname, fallback) => {
    if (pathname === '/api/servers') return route.fulfill({ json: [{ ...host, alert_mutes: mutes }] })
    if (pathname !== `/api/servers/${id}/alert-mutes` && pathname !== `/api/servers/${id}`) return fallback()
    const body = route.request().postDataJSON()
    writes.push(body)
    if (writes.length === 1) {
      firstStarted.resolve()
      await finishFirst.promise
    }
    mutes = body.alert_mutes ?? [...mutes.filter((key) => !body.remove.includes(key)), ...body.add]
    return route.fulfill({ json: { ...host, alert_mutes: mutes } })
  }, async (page, queue) => {
    const problem = (i) => queue.locator('.home-attention-row').filter({ hasText: problems[i].text })
    const mute = (i) => problem(i).getByTitle('Не алертить', { exact: true })
    await mute(0).click()
    await firstStarted.promise
    await mute(1).click()
    await problem(1).waitFor({ state: 'detached' })
    await mute(2).click()
    assert.equal(await problem(3).getByText('постоянно', { exact: true }).count(), 0, 'One mute key may be shared by separate rows')
    finishFirst.resolve()
    await problem(0).waitFor({ state: 'detached' })
    assert.equal(await problem(1).count(), 0, 'A completed mute must survive the other response')
    assert(await problem(2).getByText('постоянно', { exact: true }).isVisible(), 'A refresh must keep the mute picker on the same problem')
    assert.deepEqual(mutes, ['offline', problems[1].mute, problems[0].mute])
    assert.deepEqual(writes, problems.slice(0, 2).map(({ mute }) => ({ add: [mute], remove: [], replace_same_group: false })))
  })
  console.log('PASS parallel mute actions preserve both keys and the open picker')
}

async function servicesScope() {
  const problem = { kind: 'queue_stuck', level: 2, text: 'Demo queue stalled', section: 'services', sec: 'queues' }
  const hidden = { kind: 'mem', level: 2, text: 'Server-only alert' }
  let siteReads = 0
  await fixture((route, pathname, fallback) => {
    if (pathname === '/api/auth/me') return route.fulfill({ json: { ...defaults[pathname], role: 'viewer', sections: ['services'] } })
    if (pathname === '/api/servers') {
      return route.fulfill({ json: [{ id: 42, name: 'Demo host', online: true, last_report: {}, problems: [problem, hidden] }] })
    }
    if (pathname === '/api/checks/overview' || pathname === '/api/checks/local-probe-suggestions') siteReads++
    return fallback()
  }, async (page, queue) => {
    await queue.getByText(problem.text, { exact: false }).waitFor()
    assert.equal(await queue.getByText(hidden.text, { exact: false }).count(), 0)
    const filter = page.locator('.home-queue-filter').getByText('Сервисы', { exact: true })
    await filter.click()
    await queue.getByRole('button', { name: /Открыть раздел/ }).click()
    await queue.waitFor({ state: 'detached' })
    assert.equal(siteReads, 0, 'Home must not fetch forbidden site data')
  })
  console.log('PASS services-only account sees its queue and opens the Services section')
}

try {
  const results = await Promise.allSettled([staleRecheck(), missingRecheck(), overviewErrors(), retireRace(), parallelMutes(), servicesScope()])
  const failures = results.flatMap((result) => result.status === 'rejected' ? [result.reason] : [])
  if (failures.length) throw new AggregateError(failures, 'Home refresh regressions')
} finally { await browser.close() }
