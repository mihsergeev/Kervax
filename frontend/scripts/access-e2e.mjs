// Real-browser access regression on a disposable panel with synthetic data.
// Prerequisites: `npm ci` in frontend (lockfile pins playwright-core), Node.js,
// and an installed Chrome/Chromium executable provided as config.chrome.
// Run: node frontend/scripts/access-e2e.mjs < /private/path/disposable-panel.json
// Reads {base, username, password, chrome?, output?} as JSON from stdin.
// The JSON must also contain disposable:true; keep it private and outside Git.
// Never point this runner at a production panel: it creates and removes fixtures.
import assert from 'node:assert/strict'
import { randomBytes } from 'node:crypto'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { chromium } from 'playwright-core'

const input = []
for await (const chunk of process.stdin) input.push(chunk)
const cfg = JSON.parse(Buffer.concat(input).toString('utf8'))
assert(cfg.disposable === true, 'Configuration must explicitly mark a disposable panel')
const base = cfg.base.replace(/\/$/, '')
const out = cfg.output
const prefix = `e2e${Date.now().toString(36)}`
const password = randomBytes(24).toString('base64url')
const nextPassword = randomBytes(24).toString('base64url')
const rows = []
const users = []
const servers = []
const checks = []
const pageErrors = []
const runtimeSecrets = new Set([cfg.password, password, nextPassword].filter(Boolean))

function redacted(message) {
  let safe = String(message)
  for (const secret of runtimeSecrets) safe = safe.replaceAll(secret, '[redacted]')
  // Also catches JWTs obtained by the browser rather than the API fixture client.
  return safe.replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, '[redacted-jwt]')
}

async function request(token, route, method = 'GET', body) {
  const response = await fetch(base + route, {
    method,
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await response.text()
  let data
  try { data = JSON.parse(text) } catch { data = null }
  for (const secret of [data?.access_token, data?.token]) {
    if (typeof secret === 'string') runtimeSecrets.add(secret)
  }
  return { status: response.status, data, text }
}

async function expectStatus(token, route, expected, method = 'GET', body) {
  const response = await request(token, route, method, body)
  assert.equal(response.status, expected, `${method} ${route}: unexpected status`)
  return response.data
}

async function login(username, pass = password) {
  const data = await expectStatus(null, '/api/auth/login', 200, 'POST', { username, password: pass })
  assert.equal(typeof data.access_token, 'string')
  return data.access_token
}

async function check(name, fn) {
  const start = performance.now()
  try {
    await fn()
    rows.push({ name, passed: true, seconds: +((performance.now() - start) / 1000).toFixed(2) })
    console.log(`PASS ${name}`)
  } catch (error) {
    const message = redacted(error.message).split('\n').slice(0, 4).join(' ').slice(0, 300)
    rows.push({ name, passed: false, message })
    console.log(`FAIL ${name}: ${message}`)
    const open = browser.contexts().flatMap(ctx => ctx.pages()).at(-1)
    if (open && out) await shot(open, `failed-${rows.length}`).catch(() => {})
    for (const ctx of browser.contexts()) await ctx.close()
  }
}

const admin = await login(cfg.username, cfg.password)
const runningVersion = (await request(null, '/api/health')).data?.version
const browser = await chromium.launch({ executablePath: cfg.chrome || process.env.PW_CHROME,
  headless: true })

async function context(token) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'ru-RU',
    timezoneId: 'UTC', colorScheme: 'dark' })
  await ctx.addInitScript(({ token }) => {
    localStorage.setItem('kervax_lang', 'ru')
    if (token) localStorage.setItem('kervax_token', token)
  }, { token })
  const page = await ctx.newPage()
  page.setDefaultTimeout(10000)
  page.on('pageerror', error => pageErrors.push(error.name))
  return { ctx, page }
}

async function ready(page, suffix = '') {
  await page.goto(base + suffix, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: 'Меню', exact: true }).waitFor()
}

async function menu(page, item) {
  await page.getByRole('button', { name: 'Меню', exact: true }).click()
  await page.locator('.menu-pop').getByRole('button', { name: item, exact: true }).click()
}

async function shot(page, name) {
  if (!out) return
  await mkdir(out, { recursive: true })
  await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true })
}

try {
  const group = prefix + '-alpha'
  const otherGroup = prefix + '-beta'
  async function user(suffix, role, extra = {}) {
    const username = prefix + '-' + suffix
    const row = await expectStatus(admin, '/api/users', 201, 'POST', { username, password, role, ...extra })
    users.push(row.id)
    return { id: row.id, username, token: await login(username), role }
  }
  const editor = await user('editor', 'editor', { server_groups: [group], site_groups: [group] })
  const viewer = await user('viewer', 'viewer', { server_groups: [group], site_groups: [group] })
  const reader = await user('reader', 'viewer', { server_groups: [group], site_groups: [group], can_read_logs: true })
  const sites = await user('sites', 'viewer', { sections: ['sites'], server_groups: [group], site_groups: [group] })
  const nodes = await user('nodes', 'viewer', { sections: ['servers'], server_groups: [group], site_groups: [group] })
  async function server(suffix, groupName) {
    const data = await expectStatus(admin, '/api/servers', 201, 'POST', { name: prefix + '-' + suffix, group_name: groupName })
    servers.push(data.server.id)
    const report = { hostname: suffix + '.example', agent_version: '2.61', cpu_percent: 1,
      mem_used: 1024, mem_total: 2048, disks: [], procs: [{ pid: 100, comm: 'fixture-worker',
        cmdline: 'fixture-worker --label=fixture-hidden-log', cpu: 1, rss: 1 }],
      extras: { logerr: { v: 1, sources: [{ where: 'fixture', last: 'fixture-hidden-log' }] } } }
    await expectStatus(data.token, '/api/agent/report', 200, 'POST', report)
    return data.server
  }
  const ownServer = await server('node-alpha', group)
  const otherServer = await server('node-beta', otherGroup)
  async function monitor(suffix, groupName, target = 'https://alpha.example/') {
    const row = await expectStatus(admin, '/api/checks', 201, 'POST', {
      name: prefix + '-' + suffix, type: 'http', target, group_name: groupName,
      enabled: false, check_ssl: false, check_domain: false,
    })
    checks.push(row.id)
    return row
  }
  const ownCheck = await monitor('monitor-alpha', group)
  const otherCheck = await monitor('monitor-beta', otherGroup, 'https://beta.example/')
  const sensitiveUrl = new URL('https://alpha.example/')
  sensitiveUrl.username = 'fixture-user'
  sensitiveUrl.password = 'fixture-hidden-value'
  sensitiveUrl.search = '%74oken=fixture-hidden-value&visible=yes'
  const sensitiveCheck = await monitor('monitor-private', group, sensitiveUrl.toString())

  await check('API admin lists users and reads panel settings', async () => {
    await expectStatus(admin, '/api/users', 200)
    await expectStatus(admin, '/api/backup/config', 200)
  })
  await check('API editor/viewer cannot read admin settings or accounts', async () => {
    for (const u of [editor, viewer, reader]) {
      await expectStatus(u.token, '/api/users', 403)
      await expectStatus(u.token, '/api/backup/config', 403)
    }
  })
  await check('API group-scoped accounts see only their nodes and monitors', async () => {
    for (const u of [editor, viewer, reader]) {
      const nodes = await expectStatus(u.token, '/api/servers', 200)
      assert(nodes.some(x => x.id === ownServer.id))
      assert(!nodes.some(x => x.id === otherServer.id))
      await expectStatus(u.token, `/api/servers/${otherServer.id}`, 404)
      await expectStatus(u.token, `/api/checks/${otherCheck.id}`, 404)
    }
  })
  await check('API editor changes own objects and cannot move them to another group', async () => {
    await expectStatus(editor.token, `/api/servers/${ownServer.id}`, 200, 'PATCH', { cpu_alert_percent: 80 })
    await expectStatus(editor.token, `/api/checks/${ownCheck.id}`, 200, 'PATCH', { timeout_ms: 5000 })
    await expectStatus(editor.token, `/api/servers/${ownServer.id}`, 403, 'PATCH', { group_name: otherGroup })
    await expectStatus(editor.token, `/api/checks/${ownCheck.id}`, 403, 'PATCH', { group_name: otherGroup })
  })
  await check('API viewers cannot mutate nodes, monitors or issue restart commands', async () => {
    for (const u of [viewer, reader]) {
      await expectStatus(u.token, `/api/servers/${ownServer.id}`, 403, 'PATCH', { cpu_alert_percent: 70 })
      await expectStatus(u.token, `/api/checks/${ownCheck.id}`, 403, 'DELETE')
      await expectStatus(u.token, `/api/servers/${ownServer.id}/docker/command`, 403, 'POST', { action: 'restart', container: 'fixture' })
    }
  })
  await check('API section restrictions block direct object routes', async () => {
    await expectStatus(sites.token, '/api/servers', 403)
    await expectStatus(sites.token, `/api/servers/${ownServer.id}`, 403)
    await expectStatus(nodes.token, '/api/checks', 403)
    await expectStatus(nodes.token, `/api/checks/${ownCheck.id}`, 403)
  })
  await check('API log permission separates viewer and reader', async () => {
    for (const route of ['/api/servers', `/api/servers/${ownServer.id}`]) {
      const closed = await request(viewer.token, route)
      assert.equal(closed.status, 200)
      assert(!closed.text.includes('fixture-hidden-log'))
      const open = await request(reader.token, route)
      assert.equal(open.status, 200)
      assert(open.text.includes('fixture-hidden-log'))
    }
    await expectStatus(viewer.token, `/api/servers/${ownServer.id}/web-errors/lines?key=fixture`, 403)
    await expectStatus(viewer.token, `/api/servers/${ownServer.id}/backup/command`, 403, 'POST', { action: 'unit_journal', unit: 'fixture.service' })
  })
  await check('API viewer monitor URL hides userinfo and encoded query secrets', async () => {
    for (const u of [viewer, reader]) {
      const r = await request(u.token, `/api/checks/${sensitiveCheck.id}`)
      assert.equal(r.status, 200)
      assert(!r.text.includes('fixture-hidden-value'))
      assert(!r.text.includes('fixture-user'))
      assert(r.text.includes('visible=yes'))
    }
  })

  await check('Browser login rejects wrong password and accepts correct one', async () => {
    const { ctx, page } = await context()
    await page.goto(base, { waitUntil: 'domcontentloaded' })
    await page.getByLabel('Логин', { exact: true }).fill(editor.username)
    await page.getByLabel('Пароль', { exact: true }).fill('invalid-fixture-value')
    await page.getByRole('button', { name: 'Войти', exact: true }).click()
    await page.getByText('Неверный логин или пароль', { exact: true }).waitFor()
    await page.getByLabel('Пароль', { exact: true }).fill(password)
    await page.getByRole('button', { name: 'Войти', exact: true }).click()
    await page.getByRole('button', { name: 'Меню', exact: true }).waitFor()
    await ctx.close()
  })

  await check('Browser admin opens all six sections and user/settings windows', async () => {
    const { ctx, page } = await context(admin)
    await ready(page)
    for (const section of ['Сайты', 'Серверы', 'Докер', 'Кубер', 'Сервисы', 'Бэкапы']) {
      await page.getByRole('button', { name: section, exact: true }).first().click()
      await page.waitForTimeout(150)
    }
    await menu(page, 'Пользователи')
    await page.getByText('Новая учетка', { exact: true }).waitFor()
    await page.locator('.modal').getByRole('button', { name: 'Закрыть', exact: true }).first().click()
    await menu(page, 'Бэкап')
    await page.locator('.modal').waitFor()
    await shot(page, 'admin-settings')
    await ctx.close()
  })

  await check('Browser viewer sees scoped nodes and no edit or admin controls', async () => {
    const { ctx, page } = await context(viewer.token)
    await ready(page, `?server=${ownServer.id}`)
    await page.getByText(ownServer.name, { exact: true }).first().waitFor()
    assert.equal(await page.getByTitle('Изменить', { exact: true }).count(), 0)
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'Сайты', exact: true }).first().click()
    await page.getByText(ownCheck.name, { exact: false }).first().waitFor()
    assert.equal(await page.getByText(otherCheck.name, { exact: false }).count(), 0)
    assert.equal(await page.getByRole('button', { name: '+ Добавить монитор', exact: true }).count(), 0)
    assert(!(await page.locator('body').innerText()).includes('fixture-hidden-value'))
    await page.getByRole('button', { name: 'Меню', exact: true }).click()
    assert.equal(await page.locator('.menu-pop').getByRole('button', { name: 'Пользователи', exact: true }).count(), 0)
    assert.equal(await page.locator('.menu-pop').getByRole('button', { name: 'Бэкап', exact: true }).count(), 0)
    await shot(page, 'viewer-scope')
    await ctx.close()
  })

  await check('Browser section-specific viewer navigation matches API restrictions', async () => {
    for (const [u, allowed, denied] of [[sites, 'Сайты', 'Серверы'], [nodes, 'Серверы', 'Сайты']]) {
      const { ctx, page } = await context(u.token)
      await ready(page)
      assert.equal(await page.getByRole('button', { name: denied, exact: true }).count(), 0)
      assert(await page.getByRole('button', { name: allowed, exact: true }).count())
      await page.getByRole('button', { name: allowed, exact: true }).first().click()
      await ctx.close()
    }
  })

  await check('Browser editor opens monitor edit form and saves changes', async () => {
    const { ctx, page } = await context(editor.token)
    await ready(page, `?check=${ownCheck.id}`)
    await page.getByTitle('Изменить', { exact: true }).first().click()
    const modal = page.locator('.modal')
    await modal.getByText('Алерты', { exact: true }).click()
    const nameInput = modal.getByLabel('Название', { exact: false })
    await nameInput.fill(ownCheck.name + '-edited')
    await modal.getByRole('button', { name: 'Сохранить', exact: true }).click()
    await modal.waitFor({ state: 'hidden' })
    const saved = await expectStatus(admin, `/api/checks/${ownCheck.id}`, 200)
    assert.equal(saved.name, ownCheck.name + '-edited')
    await shot(page, 'editor-monitor')
    await ctx.close()
  })

  await check('Browser viewer can change own password and invalidates previous JWT', async () => {
    const { ctx, page } = await context(viewer.token)
    await ready(page)
    await menu(page, 'Моя учетка')
    await page.locator('.modal').getByText('Пароль', { exact: true }).first().click()
    await page.getByLabel('Текущий пароль', { exact: true }).fill(password)
    await page.getByLabel('Новый пароль (мин. 12 символов)', { exact: true }).fill(nextPassword)
    await page.getByLabel('Повторите новый пароль', { exact: true }).fill(nextPassword)
    await page.getByRole('button', { name: 'Сменить пароль', exact: true }).click()
    await page.getByText('Пароль изменён. Все прежние сессии завершены.', { exact: true }).waitFor()
    await expectStatus(viewer.token, '/api/auth/me', 401)
    const current = await page.evaluate(() => localStorage.getItem('kervax_token'))
    if (current) runtimeSecrets.add(current)
    await expectStatus(current, '/api/auth/me', 200)
    viewer.token = current
    await ctx.close()
  })

  await check('Browser admin/editor/viewer logout clears local session and revokes JWT', async () => {
    for (const role of ['admin', 'editor', 'viewer']) {
      const account = await user('logout-' + role, role)
      const { ctx, page } = await context(account.token)
      await ready(page)
      const revoked = page.waitForResponse(r => r.url().endsWith('/api/auth/logout') && r.request().method() === 'POST')
      await menu(page, 'Выйти')
      assert.equal((await revoked).status(), 204)
      await page.getByRole('button', { name: 'Войти', exact: true }).waitFor()
      assert(await page.evaluate(() => localStorage.getItem('kervax_token') === null), 'Local token must be cleared')
      await expectStatus(account.token, '/api/auth/me', 401)
      await ctx.close()
    }
  })

  await check('Browser ending impersonation revokes only that preview session', async () => {
    const preview = await expectStatus(admin, `/api/users/${editor.id}/impersonate`, 200, 'POST')
    const { ctx, page } = await context(preview.access_token)
    await ctx.addInitScript(({ admin, id }) => {
      localStorage.setItem('kervax_admin_token', admin)
      localStorage.setItem('kervax_imp_id', String(id))
    }, { admin, id: preview.imp_id })
    await ready(page)
    await menu(page, 'Вернуться к себе')
    await page.waitForFunction(token => localStorage.getItem('kervax_token') === token, admin)
    await page.getByRole('button', { name: 'Меню', exact: true }).waitFor()
    await expectStatus(admin, '/api/auth/me', 200)
    await expectStatus(editor.token, '/api/auth/me', 200)
    await expectStatus(preview.access_token, '/api/auth/me', 401)
    await ctx.close()
  })

  await check('Browser logout clears local state when revocation endpoint is unreachable', async () => {
    const { ctx, page } = await context(sites.token)
    await ready(page)
    await page.route('**/api/auth/logout', route => route.abort('connectionrefused'))
    await menu(page, 'Выйти')
    await page.getByRole('button', { name: 'Войти', exact: true }).waitFor()
    assert(await page.evaluate(() => localStorage.getItem('kervax_token') === null), 'Local token must be cleared')
    await ctx.close()
  })

  await check('Browser logout bounds a stalled revocation request and clears local state', async () => {
    const { ctx, page } = await context(nodes.token)
    await ready(page)
    await page.route('**/api/auth/logout', () => {})
    const started = performance.now()
    await menu(page, 'Выйти')
    await page.getByRole('button', { name: 'Войти', exact: true }).waitFor()
    const duration = performance.now() - started
    assert(duration >= 4500 && duration < 8500, 'Stalled logout must finish in approximately five seconds')
    assert(await page.evaluate(() => localStorage.getItem('kervax_token') === null), 'Local token must be cleared')
    await ctx.close()
  })

  await check('API deleted and recreated username cannot resurrect an old JWT', async () => {
    const replacement = await user('reused', 'viewer')
    await expectStatus(admin, `/api/users/${replacement.id}`, 204, 'DELETE')
    const row = await expectStatus(admin, '/api/users', 201, 'POST', {
      username: replacement.username, password, role: 'editor',
    })
    users.push(row.id)
    await expectStatus(replacement.token, '/api/auth/me', 401)
    await expectStatus(await login(replacement.username), '/api/auth/me', 200)
  })

  await check('Browser has no unhandled JavaScript exceptions', async () => {
    assert.equal(pageErrors.length, 0, `Unhandled JavaScript exception count: ${pageErrors.length}`)
  })
} finally {
  await browser.close()
  for (const id of checks.reverse()) await request(admin, `/api/checks/${id}`, 'DELETE')
  for (const id of servers.reverse()) await request(admin, `/api/servers/${id}`, 'DELETE')
  for (const id of [...new Set(users)].reverse()) await request(admin, `/api/users/${id}`, 'DELETE')
  if (out) {
    await mkdir(out, { recursive: true })
    await writeFile(path.join(out, 'results.json'), JSON.stringify({ version: runningVersion,
      generated: new Date().toISOString(), results: rows, pageErrors: pageErrors.length }, null, 2))
  }
}
console.log(`RESULT ${rows.filter(x => x.passed).length}/${rows.length} passed`)
if (rows.some(x => !x.passed)) process.exitCode = 1
