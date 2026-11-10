// Run against a local frontend build with PW_CHROME set to a Chrome executable.
import assert from 'node:assert/strict'
import { chromium } from 'playwright-core'

const base = new URL(process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5334')
assert(['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname))
const browser = await chromium.launch({ executablePath: process.env.PW_CHROME, headless: true })
const page = await browser.newPage()
page.setDefaultTimeout(10000)
const errors = []
page.on('pageerror', (e) => errors.push(e.message))

const ip = '192.0.2.42'
const now = new Date().toISOString()
const sample = (status, message = '') => ({ ts: now, status, latency_ms: 25, value: null, message })
const monitor = (id, name) => ({
  id, name, type: id === 1 ? 'http' : 'cert', target: `https://${name.toLowerCase()}.example.test`, enabled: true,
  port: 0, group_name: '', sort_order: id, interval_seconds: 60, timeout_ms: 10000, degraded_ms: 2000,
  retries: 3, alert_after_failures: 3, degraded_after_failures: 10, method: 'GET', expected_status: '200-399',
  keyword_up: '', keyword_down: '', check_ssl: false, check_domain: false, ssl_warn_days: [],
  domain_warn_days: [], extra_paths: [], location_ids: null, last_status: 'up', last_checked_at: now,
  last_message: '', last_latency_ms: 25, beats: [], alert_mutes: [], check_all_ips: id === 1,
  last_ip_results: id === 1 ? [{ ip, status: 'up', latency_ms: 25, message: '' }] : null,
})
let checks = [monitor(1, 'Alpha'), monitor(2, 'Beta')]
const history = (status) => ({ json: { check_id: 1, interval_seconds: 60, points: [sample(status)] } })
const held = () => ({ seen: Promise.withResolvers(), reply: Promise.withResolvers(), sent: Promise.withResolvers() })
const oldHistory = held(), oldLog = held(), deletion = held()
const delayed = async (route, pending) => {
  pending.seen.resolve()
  // A canceled fetch may already have been dropped by Chromium.
  await route.fulfill(await pending.reply.promise).catch(() => undefined)
  pending.sent.resolve()
}
let dayReads = 0, logReads = 0, ipReads = 0

try {
  await page.addInitScript(() => {
    localStorage.setItem('kervax_token', 'synthetic-ui-fixture')
    localStorage.setItem('kervax_lang', 'ru')
  })
  await page.route('**/*', async (route) => {
    const { pathname, origin, searchParams } = new URL(route.request().url())
    if (origin !== base.origin) return route.abort()
    if (!pathname.startsWith('/api/')) return route.continue()
    if (pathname === '/api/checks/1/history') {
      const hours = searchParams.get('hours')
      if (searchParams.has('ip')) {
        assert.equal(searchParams.get('ip'), ip)
        assert.equal(hours, '168')
        if (++ipReads === 2) return route.fulfill({ status: 500, json: { detail: 'Synthetic chart failure' } })
        return route.fulfill(history('up'))
      }
      if (hours === '24' && ++dayReads === 1) return delayed(route, oldHistory)
      return route.fulfill(history(hours === '168' ? 'degraded' : 'up'))
    }
    if (pathname === '/api/checks/1/log') {
      if (searchParams.get('failed') === '1') {
        return route.fulfill({ json: [sample('down', 'Current failed check')] })
      }
      if (++logReads === 1) return delayed(route, oldLog)
      return route.fulfill({ json: [sample('up', 'Current successful check')] })
    }
    if (pathname === '/api/checks/2' && route.request().method() === 'DELETE') return delayed(route, deletion)
    const defaults = {
      '/api/auth/me': { id: 1, username: 'fixture', role: 'admin' },
      '/api/servers': [], '/api/maintenance': [], '/api/alert-rechecks': [],
      '/api/checks/discovered': { domains: [], hosts: {}, ignored: [], groups: [] },
      '/api/checks/overview': { total: checks.length, up: checks.length, down: 0, open_incidents: 0, checks },
      '/api/checks/incidents': [], '/api/checks/1/uptime': { day: 100, week: 100, month: 100 },
      '/api/checks/2/uptime': { day: 100, week: 100, month: 100 },
      '/api/checks/2/log': [],
      '/api/checks/2/history': { check_id: 2, interval_seconds: 60, points: [] },
    }
    return route.fulfill({ json: defaults[pathname] ?? {} })
  })
  await page.goto(new URL('?check=1', base).href)
  const card = page.locator('.hs-modal')
  const nav = card.locator('.srv-nav')
  const rowTarget = (name) => page.locator('.check-target').getByText(`https://${name}.example.test`, { exact: true })
  const render = () => page.evaluate(() => new Promise((resolve) =>
    requestAnimationFrame(() => requestAnimationFrame(resolve))))
  const selectWindow = (label) => card.getByRole('button', { name: label, exact: true }).click()
  const release = async (pending, response) => {
    pending.reply.resolve(response)
    await pending.sent.promise
    await render()
  }

  await card.getByRole('heading', { name: 'Alpha', exact: true }).waitFor()
  await oldHistory.seen.promise
  await selectWindow('7д')
  await card.locator('.statusbar-seg-degraded').waitFor()
  await selectWindow('24ч')
  await card.locator('.statusbar-seg-up').waitFor()
  await release(oldHistory, history('down'))
  assert.equal(await card.locator('.statusbar-seg-down').count(), 0, 'Old response replaced the selected day')

  await nav.getByRole('button', { name: 'Журнал проверок', exact: true }).click()
  await oldLog.seen.promise
  await card.getByRole('button', { name: 'Только сбои', exact: true }).click()
  await card.getByText('Current failed check', { exact: true }).waitFor()
  await release(oldLog, { json: [sample('up', 'Discarded successful check')] })
  assert.equal(await card.getByText('Discarded successful check').count(), 0, 'An all-results response replaced failures')
  await card.getByRole('button', { name: 'Все', exact: true }).click()
  await card.getByText('Current successful check', { exact: true }).waitFor()

  await selectWindow('7д')
  await nav.getByRole('button', { name: 'Пути и адреса', exact: true }).click()
  await card.getByRole('button', { name: new RegExp(ip.replaceAll('.', '\\.')) }).click()
  await card.locator('.chart-cap-btn').click()
  const full = page.locator('.chart-full')
  await full.getByText(`Время ответа · IP ${ip}`, { exact: true }).waitFor()
  assert.equal(await full.locator('.win-btn-active').textContent(), '7д')
  await full.getByRole('alert').getByText('Synthetic chart failure').waitFor()
  assert.equal(await full.getByText('загрузка…', { exact: true }).count(), 0)
  await full.getByRole('button', { name: 'Повторить запрос', exact: true }).click()
  await full.locator('.statusbar-seg-up').waitFor()
  assert.equal(await full.getByRole('alert').count(), 0)
  await full.getByTitle('Закрыть', { exact: true }).click()

  await card.getByTitle('Закрыть', { exact: true }).click()
  await rowTarget('beta').click()
  await card.getByTitle('Изменить', { exact: true }).click()
  const edit = page.locator('.alerts-modal')
  await edit.getByRole('button', { name: 'Удаление', exact: true }).click()
  page.once('dialog', (dialog) => dialog.accept())
  await edit.getByRole('button', { name: /Удалить монитор/ }).click()
  await deletion.seen.promise
  await edit.locator('.al-head').getByRole('button', { name: 'Закрыть', exact: true }).click()
  await card.getByTitle('Закрыть', { exact: true }).click()
  await rowTarget('alpha').click()
  await card.getByRole('heading', { name: 'Alpha', exact: true }).waitFor()
  checks = checks.filter((check) => check.id !== 2)
  await release(deletion, { status: 204 })
  await rowTarget('beta').waitFor({ state: 'detached' })
  await card.getByRole('heading', { name: 'Alpha', exact: true }).waitFor()
  assert.equal(new URL(page.url()).searchParams.get('check'), '1')
  assert.deepEqual(errors, [])
  console.log('Check detail: history ABA, log filters, chart retry and delete navigation passed')
} catch (error) {
  if (errors.length) console.error(errors)
  throw error
} finally {
  await browser.close()
}
