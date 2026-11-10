// Run against the local Vite dev server. All API responses are synthetic.
import assert from 'node:assert/strict'
import { chromium } from 'playwright-core'

const base = new URL(process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5338').origin
assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Use a local frontend')
const browser = await chromium.launch({ headless: true, executablePath: process.env.PW_CHROME })
const previous = '2026-01-01T10:00:00Z'
const current = '2026-01-01T10:01:00Z'
const server = { id: 1, name: 'Fixture agent', group_name: '', online: false, enabled: true,
  first_report_at: null, last_seen: previous, last_report: null, problems: [], agent_advice: [] }
const status = { ip: '192.0.2.10', firewall: 'open' }
const enrolled = { server, token: 'synthetic-agent-fixture',
  install_cmd: 'curl https://panel.example.test/api/agent/install.sh | sh' }

async function fixture(api, verify) {
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } })
  page.setDefaultTimeout(7000)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  try {
    await page.clock.install()
    await page.addInitScript(enroll => {
      localStorage.setItem('kervax_token', 'synthetic-ui-fixture')
      localStorage.setItem('kervax_lang', 'ru')
      window.installFixture = enroll
    }, enrolled)
    const defaults = {
      '/api/servers': [server],
      '/api/servers/1/install-status': status,
      '/api/locations': [],
      '/api/checks/known-hosts': { hosts: {}, groups: [], ignored: [] },
    }
    await page.route('**/api/**', route => {
      const { pathname } = new URL(route.request().url())
      return api(route, pathname, () => route.fulfill({ json: defaults[pathname] ?? {} }))
    })
    await page.route('**/install-fixture', async route => {
      const response = await route.fetch({ url: base })
      const html = await response.text()
      assert(html.includes('/src/main.tsx'), 'Use the Vite development server')
      return route.fulfill({ response, body: html.replace('/src/main.tsx', '/tests/agentInstallStatus.check.ts') })
    })
    await page.goto(`${base}/install-fixture`)
    const dialog = page.locator('.ic-modal')
    await verify(page, dialog)
    assert.deepEqual(errors, [], 'Browser runtime errors')
  } catch (error) {
    assert.deepEqual(errors, [], 'Browser runtime errors')
    throw error
  } finally { await page.close() }
}

try {
  let latest = previous
  await fixture((route, pathname, fallback) => pathname === '/api/servers'
    ? route.fulfill({ json: [{ ...server, last_seen: latest }] }) : fallback(), async (page, dialog) => {
    await dialog.getByText('адрес 192.0.2.10 открыт в фаерволе панели').waitFor()
    assert(await dialog.getByText('Ждем первый отчет агента').isVisible(), 'The old report cannot confirm installation')
    latest = current
    await page.clock.fastForward(4100)
    await dialog.getByText('Агент на связи', { exact: true }).waitFor()
  })

  const firstPoll = Promise.withResolvers(), release = Promise.withResolvers()
  let statusReads = 0, platform = 'linux'
  await fixture(async (route, pathname, fallback) => {
    if (pathname === '/api/servers') return route.fulfill({ json: [{ ...server, last_seen: current,
      last_report: { platform }, agent_version: '2.9' }] })
    if (pathname === '/api/servers/1/install-status' && ++statusReads === 1) {
      firstPoll.resolve()
      await release.promise
      return route.fulfill({ json: status, headers: { 'x-fixture': 'stale-poll' } })
    }
    return fallback()
  }, async (page, dialog) => {
    await firstPoll.promise
    await dialog.getByText('Windows', { exact: true }).click()
    const staleResponse = page.waitForResponse(response => response.headers()['x-fixture'] === 'stale-poll')
    release.resolve()
    await (await staleResponse).finished()
    await page.clock.runFor(250)
    assert(await dialog.getByText('Ждем первый отчет агента').isVisible(), 'A late Linux poll cannot confirm Windows')
    platform = 'windows'
    await page.clock.fastForward(4100)
    await dialog.getByText('Агент на связи', { exact: true }).waitFor()
  })
  console.log('PASS install waits for a fresh report from the selected platform')
} finally { await browser.close() }
