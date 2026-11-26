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
  activeChecks = checks, activeServers = servers, role = 'admin', sections, waitForSites,
} = {}) {
  const errors = [], calls = [], jobs = []
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, colorScheme: 'dark' })
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
        '/api/servers': activeServers,
        '/api/alert-rechecks': jobs,
        '/api/checks/discovered': { domains: [], hosts: {}, groups: [], ignored: [] },
        '/api/checks/local-probe-suggestions': { items: [] },
        '/api/alerts/coverage': { items: [] },
        '/api/maintenance': [],
      }[pathname] ?? activeChecks.find(c => pathname === `/api/checks/${c.id}`) ?? {}
      return route.fulfill({ json })
    })
    await page.goto(base)
    await verify(page, page.locator('#home-issue-queue'), { calls, jobs })
    assert.deepEqual(errors, [], 'Browser runtime errors')
  } finally {
    await page.close()
  }
}

const checkRow = (queue, name) => queue.getByText(name, { exact: true })
const severity = (page, name) => page.getByRole('group', { name: 'Фильтр по важности' })
  .getByRole('button', { name: new RegExp(`^${name}(?:\\s|$)`) })
const recheck = (scope) => scope.getByRole('button', { name: 'Исправил: перепроверить', exact: true })
const warningDetails = (queue) => queue.locator('details.home-queue-warnings')
const snapshot = async (page, file) => {
  if (!out) return
  await page.mouse.move(0, 0)
  await page.screenshot({ path: path.join(out, `${file}.png`), fullPage: true, animations: 'disabled' })
}

async function queueFilters() {
  await fixture(async (page, queue, { jobs }) => {
    const critical = checkRow(queue, 'checkout.example.test')
    const urgentExpiry = checkRow(queue, 'auth.example.test')
    const edgeName = checkRow(queue, 'edge-01')
    const warning = checkRow(queue, 'portal.example.test')
    const serverWarning = queue.getByText('Память занята на 88%', { exact: false })
    const details = warningDetails(queue)
    const known = page.locator('details').filter({ has: page.locator('summary', { hasText: 'Известные проблемы' }) })
    const old = page.getByText('archive.example.test', { exact: true })
    const rows = queue.locator('.home-attention-row:visible')
    const all = severity(page, 'Все')
    const criticalOnly = severity(page, 'Критичные')
    const warningsOnly = severity(page, 'Предупреждения')

    await critical.waitFor()
    await urgentExpiry.waitFor()
    await edgeName.waitFor()
    await details.locator('summary').waitFor()
    await known.locator('summary').waitFor()
    assert.equal(await all.getAttribute('aria-pressed'), 'true')
    assert.match(await queue.getByRole('heading', { name: /^Все проблемы/ }).innerText(), /9$/)
    assert.equal(await details.evaluate(element => element.open), false)
    assert.equal(await known.evaluate(element => element.open), false)
    assert.equal(await rows.count(), 4)
    assert.equal(await warning.isVisible(), false)
    assert.equal(await old.isVisible(), false)
    const edge = queue.locator('.home-attention-row').filter({ has: checkRow(page, 'edge-01') })
    const edgeSignal = edge.locator('.home-queue-entry-title')
    assert.match(await edgeSignal.innerText(), /^оффлайн/i)
    assert.match(await edge.locator('.home-queue-entry-meta').innerText(), /edge-01.*7 мин/s)
    assert((await edgeSignal.boundingBox()).y < (await edgeName.boundingBox()).y, 'The signal is shown above its object and age')
    await snapshot(page, 'queue-implemented')

    await all.press('Tab')
    assert.equal(await criticalOnly.evaluate(element => element.matches(':focus-visible')), true)
    await details.locator('summary').press('Enter')
    await warning.waitFor()
    assert.equal(await rows.count(), 9)
    assert((await critical.boundingBox()).y < (await warning.boundingBox()).y, 'Critical signals must appear before warnings')
    assert((await urgentExpiry.boundingBox()).y < (await warning.boundingBox()).y, 'Urgent certificate expiry must be critical')

    await criticalOnly.click()
    assert.equal(await criticalOnly.getAttribute('aria-pressed'), 'true')
    await critical.waitFor()
    await warning.waitFor({ state: 'hidden' })
    assert.equal(await rows.count(), 4)
    await warningsOnly.click()
    assert.equal(await warningsOnly.getAttribute('aria-pressed'), 'true')
    await warning.waitFor()
    await serverWarning.waitFor()
    await critical.waitFor({ state: 'hidden' })
    await urgentExpiry.waitFor({ state: 'hidden' })
    assert.match(await queue.getByRole('heading', { name: /^Предупреждения/ }).innerText(), /5$/)
    assert.equal(await rows.count(), 5)

    const areas = page.getByRole('group', { name: 'Фильтр по разделам' })
    const sites = areas.getByRole('button', { name: /^Сайты/ })
    await sites.click()
    assert.equal(await sites.getAttribute('aria-pressed'), 'true')
    assert.equal(await warningsOnly.getAttribute('aria-pressed'), 'true')
    await warning.waitFor()
    await serverWarning.waitFor({ state: 'hidden' })
    assert.equal(await rows.count(), 2)
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
    await edgeName.waitFor({ state: 'hidden' })
    const checkout = queue.locator('.home-attention-row').filter({ has: checkRow(page, 'checkout.example.test') })
    await recheck(checkout).click()
    await checkout.getByText('Проблема осталась').waitFor()
    assert.deepEqual(jobs.map(({ object_type, object_id, kind }) => ({ object_type, object_id, kind })), [
      { object_type: 'check', object_id: 1, kind: 'status' },
    ])
    await checkout.locator('.home-queue-target').click()
    await page.waitForURL(url => url.searchParams.get('check') === '1')
  })
  console.log('PASS queue severity, collapsed warnings and known issues, section filter, recheck and direct navigation')
}

async function layout() {
  await fixture(async (page, queue) => {
    await checkRow(queue, 'checkout.example.test').waitFor()
    await warningDetails(queue).locator('summary').click()
    await checkRow(queue, 'portal.example.test').waitFor()
    const expiry = queue.locator('.home-attention-row').filter({ has: checkRow(page, 'auth.example.test') })
    for (const width of [1440, 850, 390, 320]) {
      await page.setViewportSize({ width, height: 960 })
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Page overflow at ${width}px`)
      assert(await queue.evaluate(element => element.scrollWidth <= element.clientWidth + 1), `Queue overflow at ${width}px`)
      for (const label of await page.locator('.home-queue-filter-head').all()) {
        assert(await label.evaluate(element => element.scrollWidth <= element.clientWidth + 1), `Clipped section name at ${width}px`)
      }
      for (const row of await queue.locator('.home-attention-row:visible').all()) {
        assert(await row.evaluate(element => element.scrollWidth <= element.clientWidth + 1), `Issue row overflow at ${width}px`)
      }
      const rowBox = await expiry.boundingBox()
      const controls = await Promise.all([
        recheck(expiry).boundingBox(), expiry.locator('.quick-mute').boundingBox(), expiry.locator('.home-open').boundingBox(),
      ])
      for (const [i, box] of controls.entries()) {
        assert(box && box.x >= rowBox.x && box.x + box.width <= rowBox.x + rowBox.width + 1
          && box.y >= rowBox.y && box.y + box.height <= rowBox.y + rowBox.height + 1, `Clipped SSL action at ${width}px`)
        for (const other of controls.slice(0, i)) {
          const intersects = box.x < other.x + other.width && box.x + box.width > other.x
            && box.y < other.y + other.height && box.y + box.height > other.y
          assert(!intersects, `Overlapping SSL actions at ${width}px`)
        }
      }
      if (width === 390) await snapshot(page, 'queue-mobile')
    }
    await page.setViewportSize({ width: 1440, height: 960 })
    await page.getByRole('button', { name: 'Тема', exact: true }).click()
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'light')
    await snapshot(page, 'queue-light')
  })
  console.log('PASS dark and light themes, queue layout down to 320px')
}

async function scopedViewer() {
  await fixture(async (page, queue, { calls }) => {
    await checkRow(queue, 'checkout.example.test').waitFor()
    assert.equal(await queue.getByText('edge-01', { exact: true }).count(), 0)
    await severity(page, 'Предупреждения').click()
    await checkRow(queue, 'media.example.test').waitFor()
    assert.equal(await queue.locator('.quick-mute').count(), 0)
    assert.equal(await recheck(queue).count(), 0)
    const areas = page.getByRole('group', { name: 'Фильтр по разделам' })
    assert.equal(await areas.getByRole('button').count(), 1)
    assert.equal(calls.some(({ pathname }) => pathname === '/api/servers'), false)
  }, { role: 'viewer', sections: ['sites'] })
  console.log('PASS sites-only viewer receives allowed data and read-only actions')
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
    }, { activeChecks: [], activeServers: [], waitForSites: promise })
  } finally {
    resolve()
  }
  console.log('PASS initial loading, empty queue and empty section')
}

async function criticalPagination() {
  const activeChecks = Array.from({ length: 7 }, (_, i) =>
    check(i + 20, `batch-${i + 1}.example.test`, { last_status: 'down' }))
  const longName = `${'long-'.repeat(20)}monitor.example.test`
  const longMessage = 'failure'.repeat(64)
  activeChecks[6] = check(26, longName, { last_status: 'down', last_message: longMessage })
  activeChecks.push(check(27, 'slow.example.test', { last_status: 'degraded' }))
  await fixture(async (page, queue) => {
    await checkRow(queue, activeChecks[0].name).waitFor()
    const rows = queue.locator('.home-attention-row:visible')
    const more = queue.getByRole('button', { name: 'Показать еще 2', exact: true })
    assert.equal(await rows.count(), 5)
    await warningDetails(queue).locator('summary').press('Enter')
    const warning = checkRow(queue, 'slow.example.test')
    await warning.waitFor()
    await more.click()
    assert.equal(await rows.count(), activeChecks.length)
    const target = checkRow(queue, longName)
    assert((await target.boundingBox()).y < (await warning.boundingBox()).y, 'More critical issues stay above warnings')
    await page.setViewportSize({ width: 320, height: 960 })
    const longRow = queue.locator('.home-attention-row').filter({ has: checkRow(page, longName) })
    assert(await longRow.evaluate(element => element.scrollWidth <= element.clientWidth + 1), 'Long issue title must wrap at 320px')
    const details = longRow.getByRole('button', { name: 'Подробности', exact: true })
    await details.press('Enter')
    const text = queue.locator('.home-queue-detail').filter({ hasText: longMessage })
    await text.waitFor()
    assert(await text.evaluate(element => element.scrollWidth <= element.clientWidth + 1), 'Long issue text must wrap at 320px')
    const collapse = longRow.getByRole('button', { name: 'Свернуть подробности', exact: true })
    assert.equal(await collapse.getAttribute('aria-expanded'), 'true')
    await collapse.press('Enter')
    await text.waitFor({ state: 'hidden' })
  }, { activeChecks, activeServers: [] })
  console.log('PASS expanded critical page stays above warnings and long text wraps at 320px')
}

try {
  const results = await Promise.allSettled([
    queueFilters(), layout(), scopedViewer(), loadingAndEmpty(), criticalPagination(),
  ])
  const failures = results.flatMap(result => result.status === 'rejected' ? [result.reason] : [])
  if (failures.length) throw new AggregateError(failures, 'Home queue regressions')
} finally {
  await browser.close()
}
