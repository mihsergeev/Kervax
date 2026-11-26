// Run against the built frontend on loopback with PW_CHROME set to a local browser executable.
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'
import { chromium } from 'playwright-core'

const base = new URL(process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5334').origin
assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Use a local frontend')
const out = process.env.KERVAX_TEST_SCREENSHOTS
if (out) await mkdir(out, { recursive: true })

const time = (minutesAgo) => new Date(Date.now() - minutesAgo * 60_000).toISOString()
const check = (id, name, fields = {}) => ({
  id, name, type: 'http', target: `https://${name}`, enabled: true, group_name: '',
  last_status: 'up', check_ssl: false, check_domain: false, ...fields,
})
const server = (id, name, fields = {}) => ({
  id, name, hostname: name, online: true, enabled: true, group_name: '',
  last_seen: time(1), last_report: {}, problems: [], backup_not_required: true, ...fields,
})

const checks = [
  check(1, 'checkout.example.test', { last_status: 'down', last_message: 'HTTP 503' }),
  check(2, 'auth.example.test', { check_ssl: true, ssl_days: 2, ssl_warn_days: [14] }),
  check(3, 'portal.example.test', { last_status: 'degraded', last_message: 'p95 2,8 с' }),
  check(4, 'media.example.test', { check_ssl: true, ssl_days: 12, ssl_warn_days: [14] }),
  check(5, 'archive.example.test', { last_status: 'down', last_message: 'HTTP 504',
    known_issue: { text: 'Запланирована миграция хранилища', at: time(180) } }),
  check(6, 'status.example.test'),
]
const servers = [
  server(11, 'edge-01', { online: false, last_seen: time(7), offline_probe: { state: 'silent' } }),
  server(12, 'apps-02', {
    docker_alerts: { 'invoice-worker': 'down' }, docker_since: { 'invoice-worker': time(15) },
    last_report: { docker: { present: true, access: true, containers: [
      { name: 'invoice-worker', state: 'exited' }, { name: 'frontend', state: 'running' },
    ] } },
    problems: [{ kind: 'mem', level: 1, text: 'Память занята на 88%', since: time(24), sec: 'mem' }],
  }),
  server(13, 'cluster-01', {
    last_report: { kube: { present: true, access: true, pods: [{ phase: 'Running' }] } },
    problems: [{ kind: 'flux', level: 1, text: 'Flux не синхронизировал конфигурацию',
      since: time(41), section: 'kuber', sec: 'flux' }],
  }),
  server(14, 'backup-01', {
    backup_growth: { ts: Date.parse(time(125)) / 1000, text: 'Размер бэкапа вырос в 3 раза' },
  }),
  server(15, 'apps-03'),
]

const browser = await chromium.launch({ headless: true, executablePath: process.env.PW_CHROME })
async function fixture(verify, {
  activeChecks = checks, activeServers = servers, role = 'admin', sections, waitForSites, viewport = { width: 1440, height: 960 },
} = {}) {
  const errors = [], calls = [], jobs = [], writes = []
  const fixtureServers = activeServers.map(s => ({ ...s }))
  const page = await browser.newPage({ viewport, colorScheme: 'dark' })
  page.setDefaultTimeout(6000)
  page.on('pageerror', error => errors.push(error.message))
  try {
    await page.addInitScript(() => {
      localStorage.setItem('kervax_token', 'synthetic-queue-fixture')
      localStorage.setItem('kervax_lang', 'ru')
      localStorage.setItem('kervax_theme', 'dark')
    })
    await page.route('**/*', async route => {
      const request = route.request()
      const { origin, pathname } = new URL(request.url())
      if (origin !== base) return route.abort()
      if (!pathname.startsWith('/api/')) return route.continue()
      calls.push({ pathname, method: request.method() })
      if (pathname === '/api/panel-errors/front') errors.push(request.postDataJSON().message)
      if (pathname === '/api/alert-rechecks' && request.method() === 'POST') {
        const job = { ...request.postDataJSON(), id: String(jobs.length + 1), status: 'remains', message: '', created_at: time(0) }
        jobs.push(job)
        return route.fulfill({ json: job })
      }
      const known = /^\/api\/servers\/(\d+)\/known\/(\w+)$/.exec(pathname)
      if (known && request.method() === 'PUT') {
        const target = fixtureServers.find(s => s.id === Number(known[1]))
        const body = request.postDataJSON()
        writes.push({ pathname, body })
        target.known_issues = { ...target.known_issues, [known[2]]: { text: body.text, at: time(0) } }
        return route.fulfill({ json: target })
      }
      const mutes = /^\/api\/servers\/(\d+)\/alert-mutes$/.exec(pathname)
      if (mutes && request.method() === 'POST') {
        const target = fixtureServers.find(s => s.id === Number(mutes[1]))
        const body = request.postDataJSON()
        writes.push({ pathname, body })
        target.alert_mutes = [...(target.alert_mutes ?? []).filter(key => !body.remove.includes(key)), ...body.add]
        return route.fulfill({ json: target })
      }
      if (pathname === '/api/checks/overview' && waitForSites) await waitForSites
      const json = {
        '/api/auth/me': { id: 1, username: 'demo', role, sections },
        '/api/checks/overview': {
          total: activeChecks.length, checks: activeChecks, open_incidents: 0,
          up: activeChecks.filter(c => c.last_status === 'up').length,
          down: activeChecks.filter(c => c.last_status === 'down').length,
          partial: activeChecks.filter(c => c.last_status === 'degraded').length,
        },
        '/api/checks': activeChecks,
        '/api/servers': fixtureServers,
        '/api/alert-rechecks': jobs,
        '/api/checks/discovered': { domains: [], hosts: {}, groups: [], ignored: [] },
        '/api/checks/local-probe-suggestions': { items: [] },
        '/api/alerts/coverage': { items: [] },
        '/api/maintenance': [],
      }[pathname] ?? activeChecks.find(c => pathname === `/api/checks/${c.id}`) ?? {}
      return route.fulfill({ json })
    })
    await page.goto(base)
    await verify(page, page.locator('#home-issue-queue'), { calls, jobs, writes })
    assert.deepEqual(errors, [], 'Browser runtime errors')
  } finally {
    await page.close()
  }
}

const row = (queue, text) => queue.locator('button.home-queue-row').filter({ hasText: text })
const rows = (queue) => queue.locator('button.home-queue-row:visible')
const inspector = (queue) => queue.locator('aside[aria-label="Карточка проблемы"]')
const severity = (page, name) => page.getByRole('group', { name: 'Фильтр по важности' })
  .getByRole('button', { name: new RegExp('^' + name + '(?:\\s|$)') })
const recheck = (scope) => scope.getByRole('button', { name: 'Исправил: перепроверить', exact: true })
const warningDetails = (queue) => queue.locator('details.home-queue-warnings')
const snapshot = async (page, file) => {
  if (!out) return
  await page.mouse.move(0, 0)
  await page.screenshot({ path: path.join(out, file + '.png'), fullPage: true, animations: 'disabled' })
}
const fits = (locator) => locator.evaluate(element => element.scrollWidth <= element.clientWidth + 1)
const selected = (queue) => queue.locator('button.home-queue-row[aria-current="true"]')

async function queueFilters() {
  await fixture(async (page, queue, { jobs }) => {
    const critical = row(queue, 'checkout.example.test')
    const urgentExpiry = row(queue, 'auth.example.test')
    const edge = row(queue, 'edge-01')
    const warning = row(queue, 'portal.example.test')
    const serverWarning = row(queue, 'Память занята на 88%')
    const details = warningDetails(queue)
    const card = inspector(queue)
    const known = page.locator('details').filter({ has: page.locator('summary', { hasText: 'Известные проблемы' }) })
    const old = known.getByText('archive.example.test', { exact: true })
    const all = severity(page, 'Все')
    const criticalOnly = severity(page, 'Критичные')
    const warningsOnly = severity(page, 'Предупреждения')

    await critical.waitFor()
    await urgentExpiry.waitFor()
    await edge.waitFor()
    await details.locator('summary').waitFor()
    await known.locator('summary').waitFor()
    assert.equal(await all.getAttribute('aria-pressed'), 'true')
    assert.match(await queue.getByRole('heading', { name: /^Все проблемы/ }).innerText(), /9$/)
    assert.equal(await details.evaluate(element => element.open), false)
    assert.equal(await known.evaluate(element => element.open), false)
    assert.equal(await rows(queue).count(), 4)
    assert.equal(await warning.isVisible(), false)
    assert.equal(await old.isVisible(), false)
    const edgeSignal = edge.locator('.home-queue-row-title')
    const edgeName = edge.getByText('edge-01', { exact: true })
    assert.match(await edgeSignal.innerText(), /^оффлайн/i)
    assert.match(await edge.innerText(), /edge-01.*7 мин/s)
    assert((await edgeSignal.boundingBox()).y < (await edgeName.boundingBox()).y, 'The signal is shown above its object')
    await card.waitFor()
    assert.equal(await selected(queue).count(), 1, 'Desktop selects one issue as soon as data arrives')
    assert.equal(await recheck(queue.locator('.home-queue-list')).count(), 0, 'Actions belong to the inspector')
    await snapshot(page, 'queue-implemented')

    await all.press('Tab')
    assert.equal(await criticalOnly.evaluate(element => element.matches(':focus-visible')), true)
    await details.locator('summary').press('Enter')
    await warning.waitFor()
    assert.equal(await rows(queue).count(), 9)
    assert((await critical.boundingBox()).y < (await warning.boundingBox()).y, 'Critical signals must appear before warnings')
    assert((await urgentExpiry.boundingBox()).y < (await warning.boundingBox()).y, 'Urgent certificate expiry must be critical')

    await warning.press('Enter')
    await card.getByText('portal.example.test', { exact: true }).waitFor()
    assert.equal(await warning.getAttribute('aria-current'), 'true')
    await criticalOnly.click()
    assert.equal(await criticalOnly.getAttribute('aria-pressed'), 'true')
    await critical.waitFor()
    await warning.waitFor({ state: 'hidden' })
    assert.equal(await rows(queue).count(), 4)
    assert.equal(await rows(queue).first().getAttribute('aria-current'), 'true', 'The filter selects the first available row')
    assert.equal(await card.getByText('portal.example.test', { exact: true }).count(), 0)

    await warningsOnly.click()
    assert.equal(await warningsOnly.getAttribute('aria-pressed'), 'true')
    await warning.waitFor()
    await serverWarning.waitFor()
    await critical.waitFor({ state: 'hidden' })
    await urgentExpiry.waitFor({ state: 'hidden' })
    assert.match(await queue.getByRole('heading', { name: /^Предупреждения/ }).innerText(), /5$/)
    assert.equal(await rows(queue).count(), 5)

    const areas = page.getByRole('group', { name: 'Фильтр по разделам' })
    const sites = areas.getByRole('button', { name: /^Сайты/ })
    await sites.click()
    assert.equal(await sites.getAttribute('aria-pressed'), 'true')
    assert.equal(await warningsOnly.getAttribute('aria-pressed'), 'true')
    await warning.waitFor()
    await serverWarning.waitFor({ state: 'hidden' })
    assert.equal(await rows(queue).count(), 2)
    assert.match(await queue.getByRole('heading', { name: /^Предупреждения/ }).innerText(), /2$/)
    await sites.click()
    assert.equal(await sites.getAttribute('aria-pressed'), 'false')
    await serverWarning.waitFor()
    await sites.click()
    assert.equal(await old.isVisible(), false)
    await known.locator('summary').click()
    await old.waitFor()
    await known.locator('summary').click()

    await criticalOnly.click()
    await critical.waitFor()
    await edge.waitFor({ state: 'hidden' })
    await critical.click()
    await recheck(card).click()
    await card.getByText('Проблема осталась').waitFor()
    await urgentExpiry.click()
    assert.equal(await card.getByText('Проблема осталась').count(), 0, 'The job result belongs to the selected issue')
    await critical.click()
    await card.getByText('Проблема осталась').waitFor()
    assert.deepEqual(jobs.map(({ object_type, object_id, kind }) => ({ object_type, object_id, kind })), [
      { object_type: 'check', object_id: 1, kind: 'status' },
    ])
    assert.equal(new URL(page.url()).searchParams.get('check'), null, 'Selecting a row does not navigate')
    await card.getByRole('button', { name: 'Открыть объект', exact: true }).click()
    await page.waitForURL(url => url.searchParams.get('check') === '1')
  })
  console.log('PASS queue filters, desktop selection, recheck state and direct navigation')
}

async function desktopLayout() {
  await fixture(async (page, queue) => {
    const expiry = row(queue, 'auth.example.test')
    const card = inspector(queue)
    await warningDetails(queue).locator('summary').click()
    await row(queue, 'portal.example.test').waitFor()
    await expiry.click()
    for (const width of [1440, 1024]) {
      await page.setViewportSize({ width, height: 960 })
      const listBox = await queue.locator('.home-queue-list').boundingBox()
      const cardBox = await card.boundingBox()
      assert(cardBox && cardBox.x >= listBox.x + listBox.width - 1, 'Inspector appears beside the queue on a wide viewport')
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Page must fit the viewport')
      assert(await fits(queue), 'The queue must fit its container')
      for (const target of await rows(queue).all()) assert(await fits(target), 'The issue row must fit its container')
      for (const label of await page.locator('.home-queue-filter-head').all()) {
        assert(await fits(label), 'The filter label must fit its container')
      }
      await recheck(card).waitFor()
      await card.getByRole('button', { name: /Напомнить позже.*сертификатом/ }).waitFor()
      await card.getByRole('button', { name: 'Открыть объект', exact: true }).waitFor()
    }
    await page.getByRole('button', { name: 'Тема', exact: true }).click()
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'light')
    await snapshot(page, 'queue-light')
  })
  console.log('PASS desktop inspector, layout and theme')
}

async function mobileLayout() {
  await fixture(async (page, queue) => {
    const checkout = row(queue, 'checkout.example.test')
    const expiry = row(queue, 'auth.example.test')
    const card = inspector(queue)
    const close = card.getByRole('button', { name: 'Закрыть подробности', exact: true })
    await checkout.waitFor()
    await card.waitFor({ state: 'hidden' })
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 960 })
      await checkout.press('Enter')
      await card.getByText('checkout.example.test', { exact: true }).waitFor()
      assert.equal(await checkout.getAttribute('aria-current'), 'true')
      assert((await card.boundingBox()).y < (await checkout.boundingBox()).y, 'On mobile the card appears before the queue')
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Page must fit the mobile viewport')
      assert(await fits(queue), 'The mobile queue must fit its container')
      assert(await fits(card), 'The mobile inspector must fit its container')
      assert(await fits(checkout), 'The mobile issue row must fit its container')
      await close.press('Escape')
      await card.waitFor({ state: 'hidden' })
      assert.equal(await checkout.evaluate(element => document.activeElement === element), true, 'Escape returns focus to the selected issue')
      await expiry.click()
      await card.getByText('auth.example.test', { exact: true }).waitFor()
      if (width === 390) await snapshot(page, 'queue-mobile')
      await close.click()
      await card.waitFor({ state: 'hidden' })
      assert.equal(await expiry.evaluate(element => document.activeElement === element), true, 'Close returns focus to the selected issue')
    }
  }, { viewport: { width: 390, height: 960 } })
  console.log('PASS mobile detail selection, keyboard, focus return and layout at 320px')
}

async function scopedViewer() {
  await fixture(async (page, queue, { calls }) => {
    const checkout = row(queue, 'checkout.example.test')
    const card = inspector(queue)
    await checkout.click()
    await card.getByText('checkout.example.test', { exact: true }).waitFor()
    assert.equal(await row(queue, 'edge-01').count(), 0)
    assert.equal(await recheck(card).count(), 0)
    await severity(page, 'Предупреждения').click()
    await row(queue, 'media.example.test').click()
    await card.getByText('media.example.test', { exact: true }).waitFor()
    assert.equal(await card.getByRole('button', { name: /Напомнить позже/ }).count(), 0)
    assert.equal(await card.getByRole('button', { name: 'Открыть объект', exact: true }).count(), 1)
    const areas = page.getByRole('group', { name: 'Фильтр по разделам' })
    assert.equal(await areas.getByRole('button').count(), 1)
    assert.equal(calls.some(({ pathname }) => pathname === '/api/servers'), false)
    assert.equal(calls.some(({ method }) => ['POST', 'PATCH', 'PUT', 'DELETE'].includes(method)), false)
  }, { role: 'viewer', sections: ['sites'] })
  console.log('PASS sites-only viewer sees permitted issues and read-only actions')
}

async function loadingAndEmpty() {
  const { promise, resolve } = Promise.withResolvers()
  try {
    await fixture(async (page, queue) => {
      try {
        await queue.getByText(/загрузка/i).waitFor()
        assert.equal(await queue.getByText('Нет активных проблем').count(), 0)
      } finally {
        resolve()
      }
      await queue.getByText('Нет активных проблем').waitFor()
      await page.getByRole('group', { name: 'Фильтр по разделам' }).getByRole('button', { name: /^Сайты/ }).click()
      await queue.getByText('Нет активных проблем').waitFor()
      assert.equal(await selected(queue).count(), 0)
    }, { activeChecks: [], activeServers: [], waitForSites: promise })
  } finally {
    resolve()
  }
  console.log('PASS initial loading, empty queue and empty section')
}

async function criticalPagination() {
  const activeChecks = Array.from({ length: 7 }, (_, i) =>
    check(i + 20, 'batch-' + (i + 1) + '.example.test', { last_status: 'down' }))
  const longName = 'long-'.repeat(20) + 'monitor.example.test'
  const longMessage = 'failure'.repeat(64)
  activeChecks[6] = check(26, longName, { last_status: 'down', last_message: longMessage })
  activeChecks.push(check(27, 'slow.example.test', { last_status: 'degraded' }))
  await fixture(async (page, queue) => {
    await row(queue, activeChecks[0].name).waitFor()
    const more = queue.getByRole('button', { name: 'Показать еще 2', exact: true })
    assert.equal(await rows(queue).count(), 5)
    await warningDetails(queue).locator('summary').press('Enter')
    const warning = row(queue, 'slow.example.test')
    await warning.waitFor()
    await more.click()
    assert.equal(await rows(queue).count(), activeChecks.length)
    const target = row(queue, longName)
    assert((await target.boundingBox()).y < (await warning.boundingBox()).y, 'More critical issues stay above warnings')
    assert(await fits(target), 'The issue row must fit a long name at 320px')
    await target.press('Enter')
    const card = inspector(queue)
    const text = card.getByText(longMessage, { exact: false })
    await text.waitFor()
    assert(await card.getByText(longName, { exact: true }).isVisible(), 'The full name remains available in the inspector')
    assert(await fits(text), 'Long issue text must wrap at 320px')
    await card.getByRole('button', { name: 'Закрыть подробности', exact: true }).press('Enter')
    await card.waitFor({ state: 'hidden' })
    assert.equal(await target.evaluate(element => document.activeElement === element), true)
  }, { activeChecks, activeServers: [], viewport: { width: 320, height: 960 } })
  console.log('PASS critical pagination and long details at 320px')
}

async function groupedActions() {
  const activeServers = ['worker-01', 'worker-02'].map((name, index) => server(31 + index, name, {
    home_mutes: { 'd-acc': 'home:d-acc' },
    last_report: { docker: { present: true, access: false }, caps: { alert_recheck: true } },
    alert_since: { mem: time(24) },
    problems: [{ kind: 'mem', level: 1, sec: 'mem', text: 'Память заполнена - доступно ' + (120 + index * 48) + ' МиБ' }],
  }))
  await fixture(async (page, queue, { jobs, writes }) => {
    const card = inspector(queue)
    const memory = row(queue, 'Память заполнена')
    const docker = row(queue, 'Docker без доступа')
    await severity(page, 'Предупреждения').click()
    await memory.click()
    assert.equal(await memory.getAttribute('aria-current'), 'true')
    await card.getByText('доступно 120 МиБ', { exact: false }).waitFor()
    await card.getByText('доступно 168 МиБ', { exact: false }).waitFor()
    assert.equal(await queue.locator('.home-queue-list button.home-queue-row').count(), 2)

    await docker.click()
    const checkbox = (name) => card.getByRole('checkbox', { name, exact: true })
    await checkbox('worker-01').waitFor()
    await checkbox('worker-02').waitFor()
    await checkbox('worker-01').check()
    assert.equal(await checkbox('worker-02').isChecked(), false)
    await card.getByRole('button', { name: 'заглушить выбранные (1)', exact: true }).click()
    await card.getByRole('button', { name: 'заглушено 1, показать', exact: true }).click()
    await card.getByRole('button', { name: 'вернуть', exact: true }).click()
    await checkbox('worker-01').waitFor()
    assert.deepEqual(writes, [
      { pathname: '/api/servers/31/alert-mutes', body: { add: ['home:d-acc'], remove: [], replace_same_group: true } },
      { pathname: '/api/servers/31/alert-mutes', body: { add: [], remove: ['home:d-acc'], replace_same_group: false } },
    ])

    const groupRecheck = recheck(card.locator(':scope > div > .home-queue-inspector-actions'))
    await groupRecheck.click()
    await card.getByText(/Проблема осталась/).first().waitFor()
    assert.deepEqual(jobs.map(({ object_type, object_id, kind }) => ({ object_type, object_id, kind }))
      .sort((a, b) => a.object_id - b.object_id), [31, 32].map(object_id => ({
      object_type: 'server', object_id, kind: 'home:d-acc',
    })))
  }, { activeChecks: [], activeServers, role: 'editor', sections: ['servers', 'docker'] })
  console.log('PASS scoped editor, grouped server details, member selection, restore and bulk recheck')
}

async function markKnown() {
  const signal = 'Память занята на 88%'
  const note = 'Увеличим лимит во время окна обслуживания'
  const activeServers = [server(41, 'worker-04', {
    alert_since: { mem: time(24) },
    problems: [{ kind: 'mem', level: 1, text: signal, sec: 'mem' }],
  })]
  await fixture(async (page, queue, { writes }) => {
    const target = row(queue, signal)
    await severity(page, 'Предупреждения').click()
    await target.click()
    const card = inspector(queue)
    await card.getByRole('button', { name: 'Отметить как известную', exact: true }).click()
    const dialog = page.locator('.known-dialog')
    await dialog.getByPlaceholder('Что известно: кто чинит, когда, где тикет').fill(note)
    await dialog.getByRole('button', { name: 'Отметить', exact: true }).click()
    await target.waitFor({ state: 'detached' })
    const known = page.locator('details').filter({ has: page.locator('summary', { hasText: 'Известные проблемы' }) })
    await known.locator('summary').click()
    await known.getByText(note, { exact: false }).waitFor()
    assert.deepEqual(writes, [{ pathname: '/api/servers/41/known/mem', body: { text: note, days: 0 } }])
  }, { activeChecks: [], activeServers })
  console.log('PASS marking an issue as known from the inspector')
}

try {
  const results = await Promise.allSettled([
    queueFilters(), desktopLayout(), mobileLayout(), scopedViewer(), loadingAndEmpty(),
    criticalPagination(), groupedActions(), markKnown(),
  ])
  const failures = results.flatMap(result => result.status === 'rejected' ? [result.reason] : [])
  if (failures.length) throw new AggregateError(failures, 'Home queue regressions')
} finally {
  await browser.close()
}
