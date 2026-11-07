// Run against the local frontend with PW_CHROME set to a Chrome executable.
import assert from 'node:assert/strict'
import { chromium } from 'playwright-core'

const base = process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5334'
assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Use a local frontend')

const discoveryPath = '/api/checks/discovered'
const browser = await chromium.launch({ headless: true, executablePath: process.env.PW_CHROME })
try {
  const fixture = async (role, sections) => {
    const page = await browser.newPage({ viewport: { width: 360, height: 800 } })
    const discovered = {
      domains: ['alpha.example.test', 'beta.example.test', 'gamma.example.test', '*.example.test',
        'existing.example.test', 'ignored.example.test'].map(domain => ({ domain, servers: ['example-node'] })),
      hosts: { 'existing.example.test': 1 },
      ignored: ['ignored.example.test'],
      groups: [],
    }
    const calls = [], errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.addInitScript(() => {
      localStorage.setItem('kervax_token', 'synthetic-ui-fixture')
      localStorage.setItem('kervax_lang', 'ru')
    })
    await page.route('**/api/**', async route => {
      const request = route.request()
      const pathname = new URL(request.url()).pathname
      const body = request.postDataJSON()
      calls.push({ pathname, body })
      let data = {
        '/api/auth/me': { id: 1, username: 'fixture', role, sections },
        '/api/checks/overview': { total: 0, up: 0, down: 0, open_incidents: 0, checks: [] },
        '/api/servers': [],
        '/api/maintenance': [],
        '/api/alert-rechecks': [],
        '/api/checks/local-probe-suggestions': { items: [] },
        '/api/alerts/coverage': { items: [] },
        [discoveryPath]: discovered,
        [discoveryPath + '/probe']: { items: [] },
        [discoveryPath + '/probes']: { items: [] },
      }[pathname] ?? {}
      if (pathname === discoveryPath + '/ignore') {
        discovered.ignored = body.ignore
          ? [...discovered.ignored, ...body.domains]
          : discovered.ignored.filter(domain => !body.domains.includes(domain))
        data = { updated: body.domains.length }
      } else if (pathname === '/api/checks/adopt') {
        const [domain] = body.domains
        discovered.hosts[domain] = 2
        data = { created: 1, skipped: [], local: 0, group_name: '', hosts: discovered.hosts,
          items: [{ domain, check_id: 2, local: false, server: '', path: '', expected_status: '', reason: '', problem: '' }] }
      }
      return route.fulfill({ json: data })
    })
    return { page, discovered, calls, errors }
  }

  const { page, discovered, calls, errors } = await fixture('editor', ['sites'])
  const suggestions = page.locator('.home-domain-suggestions')
  const count = n => suggestions.locator('.home-domain-label .net-n').filter({ hasText: new RegExp(`^${n}$`) }).waitFor()
  const row = domain => suggestions.locator('li').filter({ hasText: domain })
  const modal = page.locator('.adopt-modal')
  const probes = () => calls.filter(call => call.pathname === discoveryPath + '/probe')
  await page.clock.install()
  await page.goto(base)
  await count(3)
  assert.equal(await row('alpha.example.test').isVisible(), false, 'Suggestions should start collapsed')
  discovered.domains.push({ domain: 'delta.example.test', servers: ['example-node'] })
  await page.clock.fastForward(120_000)
  await count(4)
  assert.equal(probes().length, 0, 'Polling must not probe sites')
  const view = suggestions.getByRole('button', { name: 'Посмотреть', exact: true })
  const collapse = suggestions.getByRole('button', { name: 'Свернуть список' })
  const options = suggestions.getByRole('button', { name: 'Настройки подсказок' })
  const hideBlock = suggestions.getByRole('button', { name: 'Не показывать на главной' })
  assert.equal(await view.getAttribute('aria-expanded'), 'false')
  await options.click()
  await hideBlock.waitFor()
  assert.equal(await row('alpha.example.test').isVisible(), false, 'Options must not expand the list')
  await options.click()
  await view.press('Enter')
  assert.equal(await collapse.getAttribute('aria-expanded'), 'true')
  assert.equal(await suggestions.locator('li').count(), 3)
  assert(await suggestions.evaluate(element => element.scrollWidth <= element.clientWidth + 1), 'Mobile overflow')
  await suggestions.getByRole('button', { name: 'Все домены (4)' }).click()
  await modal.getByText('delta.example.test', { exact: true }).waitFor()
  await modal.getByRole('button', { name: 'Закрыть', exact: true }).click()

  await row('beta.example.test').getByRole('button', { name: 'Не мониторить' }).click()
  await count(3)
  assert.deepEqual(calls.find(call => call.pathname === discoveryPath + '/ignore').body,
    { domains: ['beta.example.test'], ignore: true })
  await row('alpha.example.test').getByRole('button', { name: 'Добавить', exact: true }).click()
  assert.equal(await modal.getByText('delta.example.test', { exact: true }).count(), 0)
  await modal.getByRole('button', { name: 'Поставить на мониторинг' }).click()
  await modal.getByRole('button', { name: 'Готово' }).click()
  await count(2)
  assert.deepEqual(calls.find(call => call.pathname === '/api/checks/adopt').body.domains, ['alpha.example.test'])

  await collapse.click()
  assert.equal(await row('gamma.example.test').isVisible(), false)
  await options.click()
  await hideBlock.click()
  await suggestions.waitFor({ state: 'hidden' })
  const discoverCalls = calls.filter(call => call.pathname === discoveryPath).length
  await page.reload()
  await page.getByRole('button', { name: 'Сайты', exact: true }).waitFor()
  await page.clock.fastForward(120_000)
  assert.equal(calls.filter(call => call.pathname === discoveryPath).length, discoverCalls, 'Disabled widget still polls')
  await page.getByRole('button', { name: 'Сайты', exact: true }).click()
  await page.getByRole('button', { name: 'Показывать новые домены на главной' }).click()
  await page.getByRole('button', { name: 'Посмотреть и добавить' }).click()
  await modal.getByRole('button', { name: 'ненужных: 2' }).click()
  await modal.locator('.adopt-row').filter({ hasText: 'beta.example.test' }).getByTitle('Вернуть в предложения').click()
  await modal.getByRole('button', { name: 'скрыть ненужные (1)' }).waitFor()
  await modal.getByRole('button', { name: 'Закрыть', exact: true }).click()
  await page.locator('.brand-link').click()
  await count(3)
  assert.equal(await row('gamma.example.test').isVisible(), false, 'Re-enabled suggestions must start collapsed')
  assert.deepEqual(errors, [])

  for (const [role, sections] of [['viewer', ['sites']], ['editor', ['servers']]]) {
    const restricted = await fixture(role, sections)
    await restricted.page.goto(base)
    await restricted.page.locator('.brand-link').waitFor()
    assert.equal(await restricted.page.locator('.home-domain-suggestions').count(), 0)
    assert.equal(restricted.calls.some(call => call.pathname === discoveryPath), false)
    assert.deepEqual(restricted.errors, [])
  }
  console.log('PASS discovery refresh, collapsed mobile preview, adoption, dismissal, restore, preference and access')
} finally {
  await browser.close()
}
