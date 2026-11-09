// Run against an isolated live fixture panel, never a production installation.
// The fixture exposes /lab/control; all normal checks and UI requests use real API paths.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { chromium } from 'playwright-core'

const base = process.env.KERVAX_TEST_BASE_URL
const password = process.env.KERVAX_TEST_PASSWORD
const browserPath = process.env.KERVAX_TEST_BROWSER
assert(base && password && browserPath, 'Set KERVAX_TEST_BASE_URL, KERVAX_TEST_PASSWORD and KERVAX_TEST_BROWSER')
const screenshots = process.env.KERVAX_TEST_SCREENSHOTS
const browser = await chromium.launch({ headless: true, executablePath: browserPath })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const failures = []
page.on('pageerror', (e) => failures.push(e.message))
try {
  const login = await page.request.post(`${base}/api/auth/login`, { data: { username: 'admin', password } })
  assert.equal(login.status(), 200)
  const token = (await login.json()).access_token
  const headers = { Authorization: `Bearer ${token}` }
  const jobs = async () => (await page.request.get(`${base}/api/alert-rechecks`, { headers })).json()
  const wait = async (predicate, message, timeout = 40000) => {
    const until = Date.now() + timeout
    while (Date.now() < until) {
      if (await predicate()) return
      await page.waitForTimeout(250)
    }
    throw new Error(message)
  }
  const control = async (data) => assert.equal((await page.request.post(`${base}/lab/control`, { data })).status(), 200)
  const click = async (row) => row.getByRole('button', { name: 'Исправил: перепроверить', exact: true }).click()
  const screenshot = async (name) => {
    if (!screenshots) return
    await fs.mkdir(screenshots, { recursive: true })
    await page.screenshot({ path: path.join(screenshots, name + '.png'), fullPage: true })
  }
  await control({ a: '6.0.1', b: '6.0.0' })
  assert.equal((await page.request.post(`${base}/lab/reset`)).status(), 200)
  await page.goto(base)
  await page.evaluate((t) => localStorage.setItem('kervax_token', t), token)
  await page.reload()
  const queue = page.locator('#home-issue-queue')
  const group = queue.locator('.home-attention-grpbox').filter({ hasText: 'WordPress 6.0.0' })
  await group.waitFor()
  await click(group)
  await wait(async () => {
    const rows = (await jobs()).filter((x) => x.kind === 'tamper')
    return rows.length === 2 && rows.some((x) => x.status === 'fixed') && rows.some((x) => x.status === 'remains')
  }, 'Partial CMS group result missing')
  await wait(async () => !(await queue.innerText()).includes('Demo site A') &&
    (await queue.innerText()).includes('Demo site B'), 'Fixed group member remained on home page')
  await screenshot('recheck-desktop-partial')
  await page.setViewportSize({ width: 390, height: 844 })
  await screenshot('recheck-mobile-partial')
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Mobile horizontal overflow')
  await page.setViewportSize({ width: 1440, height: 1000 })
  console.log('PASS live CMS group: one verified, one remains; desktop/mobile layout')

  await control({ b: 'failed' })
  await page.waitForTimeout(5200)
  const remaining = queue.locator('.home-attention-row').filter({ hasText: 'Demo site B' }).first()
  await click(remaining)
  await wait(async () => (await jobs()).some((x) => x.kind === 'tamper' && x.status === 'unavailable'), 'HTTP failure falsely treated as recovery')
  await wait(async () => (await remaining.innerText()).includes('Не удалось проверить'), 'Unavailable result absent in the row')
  await screenshot('recheck-desktop-unavailable')
  console.log('PASS unavailable HTTP observation keeps the alert visible')

  await control({ b: '6.0.1' })
  await page.waitForTimeout(5200)
  await click(remaining)
  await wait(async () => (await jobs()).filter((x) => x.kind === 'tamper').every((x) => x.status === 'fixed'), 'CMS recovery not verified')
  await wait(async () => !(await queue.innerText()).includes('Demo site'), 'CMS alert did not clear')

  await control({ cert_days: 365 })
  const cert = queue.locator('.home-attention-row').filter({ hasText: 'Demo cluster' }).first()
  await click(cert)
  await wait(async () => (await jobs()).some((x) => x.kind === 'kube_expiry' && x.status === 'fixed'), 'Sandboxed agent did not verify renewed certificate', 60000)
  await cert.waitFor({ state: 'detached' })
  await screenshot('recheck-desktop-recovered')
  console.log('PASS real certificate renewal -> sandboxed agent -> root collector receipt -> home alert removed')
  assert.deepEqual(failures, [], 'Browser runtime errors')
  console.log('PASS no browser runtime errors')
} finally { await browser.close() }
