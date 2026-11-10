// Run against the local Vite server with synthetic reports and credentials.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { chromium } from 'playwright-core'
import { systemdUnitArg } from '../src/shell.ts'

const base = new URL(process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5334').origin
assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Use a local frontend')
const browser = await chromium.launch({ headless: true, executablePath: process.env.PW_CHROME })
const context = await browser.newContext({ viewport: { width: 1400, height: 950 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(e.message))
const password = "demo'$(printf INJECTED)' with spaces"
const localRepo = "/tmp/demo repo'$(printf INJECTED)"
const unsafeRepo = 'demo/../unrelated'
const validRepo = 'safe-demo'
const customData = '/mnt/demo/kervax-rest-server/data'
const customRepos = [
  invalidRepo(validRepo), invalidRepo(unsafeRepo), ...Array.from({ length: 3 }, (_, i) => invalidRepo(`demo-${i}`)),
  { name: 'demo-lock', valid: true, snapshots: 1, locked: true, lock_age_sec: 86400, last_activity: 0 },
]
const fixtureServer = (id, report) => ({
  id, name: `Demo server ${id}`, enabled: true, online: true, group_name: '',
  last_report: report,
})
function invalidRepo(name) { return { name, snapshots: 0, valid: false, last_activity: 0 } }
try {
  await page.addInitScript(() => {
    localStorage.setItem('kervax_token', 'synthetic-ui-fixture')
    localStorage.setItem('kervax_lang', 'ru')
    window.copied = null
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: async (text) => {
        if (window.denyCopy) throw new Error('Synthetic clipboard denial')
        window.copied = text
      } },
    })
  })
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const fixtures = {
      '/api/auth/me': { id: 1, username: 'fixture', role: 'admin' },
      '/api/servers': [
        fixtureServer(1, { backup: { present: true, configured: true, manageable: true } }),
        fixtureServer(2, { backup_server: { present: true, repos: [invalidRepo(unsafeRepo), invalidRepo(validRepo)] } }),
        fixtureServer(3, { backup_server: { present: true, data_dir: customData, repos: customRepos } }),
        fixtureServer(4, { backup_server: { present: true, data_dir: '/mnt/../kervax-rest-server/data', repos: [invalidRepo(validRepo)] } }),
      ],
      '/api/servers/1/backup/credentials': {
        repopass: password, repo_local: localRepo, repo_url: 'rest:https://backup.example.test/demo',
      },
    }
    return route.fulfill({ json: fixtures[url.pathname] ?? {} })
  })
  await page.goto(`${base}/?backup=1&sec=restore`)
  await page.getByRole('button', { name: 'Данные для восстановления', exact: false }).click()
  const box = page.locator('.restore-creds .agent-advice-cmd')
  const copy = box.getByRole('button', { name: 'Копировать' })
  await page.evaluate(() => { window.denyCopy = true })
  await copy.click()
  await page.getByRole('alert').getByText('Не удалось скопировать').waitFor()
  assert.equal(await page.evaluate(() => window.copied), null)
  await page.evaluate(() => { window.denyCopy = false })
  await copy.click()
  await box.getByRole('button', { name: '✓' }).waitFor()
  const command = await page.evaluate(() => window.copied)
  assert.equal(command, await box.locator('pre').textContent(), 'Copy must match the displayed command')

  // A fake restic captures its arguments; the command never touches a repository.
  const bash = process.platform === 'win32' ? 'wsl' : 'bash'
  const args = process.platform === 'win32' ? ['--cd', '/', '--', 'bash', '-s'] : ['-s']
  const run = spawnSync(bash, args, {
    input: `set -eu\nrestic() { printf '%s\\0' "$RESTIC_REPOSITORY" "$RESTIC_PASSWORD" "$@"; }\n${command}\n`,
    encoding: 'utf8', timeout: 10000,
  })
  assert.equal(run.status, 0, run.stderr)
  assert.deepEqual(run.stdout.split('\0'), [localRepo, password, 'snapshots', ''])

  for (const unit of ['demo*.service', 'demo;printf INJECTED.socket', '../demo.service', 'demo.service\nfalse']) {
    assert.equal(systemdUnitArg(unit), null, unit)
  }
  const escapedUnit = String.raw`demo\x20instance@template.service`
  const unitArgs = spawnSync(bash, args, {
    input: `printf '%s\\0' ${systemdUnitArg(escapedUnit)} ${systemdUnitArg('-.mount')}\n`,
    encoding: 'utf8', timeout: 10000,
  })
  assert.equal(unitArgs.status, 0, unitArgs.stderr)
  assert.deepEqual(unitArgs.stdout.split('\0'), [escapedUnit, '-.mount', ''])

  await page.goto(`${base}/?backupsrv=2&sec=repos`)
  await page.getByRole('button', { name: unsafeRepo, exact: false }).click()
  await page.getByRole('button', { name: 'Как удалить', exact: true }).click()
  const cleanup = page.locator('.repo-cleanup')
  await cleanup.getByText('Имя репозитория выглядит небезопасно', { exact: false }).waitFor()
  assert.equal(await cleanup.locator('pre').count(), 0, 'Unsafe repository must not produce a deletion command')
  await page.getByRole('button', { name: validRepo, exact: false }).click()
  await page.getByRole('button', { name: 'Как удалить', exact: true }).click()
  const unwrap = (sudoCommand) => {
    const unwrapped = spawnSync(bash, args, {
      input: `sudo() { [ "$1" = bash ] && [ "$2" = -c ] && printf '%s' "$3"; }\n${sudoCommand}\n`,
      encoding: 'utf8', timeout: 10000,
    })
    assert.equal(unwrapped.status, 0, unwrapped.stderr)
    const syntax = spawnSync(bash, [...args, '-n'], { input: unwrapped.stdout, encoding: 'utf8', timeout: 10000 })
    assert.equal(syntax.status, 0, syntax.stderr)
    return unwrapped.stdout
  }
  const defaultCommand = unwrap(await cleanup.locator('pre').textContent())
  assert.match(defaultCommand, /^DATA=\/app\/rest-server\/data$/m)
  assert.match(defaultCommand, /\[ ! -e "\$STORAGE" \]/)

  await page.goto(`${base}/?backupsrv=3&sec=repos`)
  await page.getByRole('button', { name: validRepo, exact: false }).click()
  await page.getByRole('button', { name: 'Как удалить', exact: true }).click()
  const customCommand = unwrap(await cleanup.locator('pre').textContent())
  assert(customCommand.includes(`DATA=${customData}`))
  assert.match(customCommand, /rm -rf -- "\$DATA\/\$R"/)
  assert.match(customCommand, /htpasswd -D "\$DATA\/\.htpasswd"/)
  assert.match(customCommand, /\/app\/rest-server\/system\/envs\/\$R.env/)
  assert.match(customCommand, /mountpoint -q -- "\$MOUNT"/)
  assert.match(customCommand, /\.kervax-volume-\$marker/)
  // Only run the preflight, against temporary directories and stubbed mount data.
  const guardAt = (cmd, data) => cmd.slice(0, cmd.indexOf('\nR='))
    .replace(/^DATA=.*$/m, () => `DATA="$sandbox/${data}"`)
    .replace(/^STORAGE=.*$/m, 'STORAGE="$sandbox/app/storage"')
    .replace(/^MOUNT=.*$/m, 'MOUNT="$sandbox/mount"')
  const preflight = spawnSync(bash, args, {
    input: `set -eu
sandbox="$(mktemp -d)"
trap 'rm -rf -- "$sandbox"' EXIT
mkdir -p "$sandbox/app/data" "$sandbox/mount/kervax-rest-server/data"
marker=0123456789abcdef
unsafe_dir=''
printf '%s\\n' "$sandbox/mount" "$marker" > "$sandbox/app/storage"
touch "$sandbox/mount/kervax-rest-server/data/.kervax-volume-$marker"
mountpoint() { [ "$1 $2" = "-q --" ] && [ "$3" = "$sandbox/mount" ]; }
stat() {
  if [ "$2" = '%u %a' ]; then
    [ "$4" != "$unsafe_dir" ] && printf '0 755\\n' || printf '0 775\\n'
  else
    [ "$3" = / ] && printf '1\\n' || printf '2\\n'
  fi
}
custom_guard() (
${guardAt(customCommand, 'mount/kervax-rest-server/data')}
)
default_guard() (
${guardAt(defaultCommand, 'app/data')}
)
custom_guard
if default_guard 2>/dev/null; then exit 11; fi
unsafe_dir="$sandbox/mount"
if custom_guard 2>/dev/null; then exit 12; fi
unsafe_dir=''
rm -- "$sandbox/mount/kervax-rest-server/data/.kervax-volume-$marker"
if custom_guard 2>/dev/null; then exit 13; fi
rm -- "$sandbox/app/storage"
default_guard
rmdir "$sandbox/app/data"
ln -s "$sandbox/mount/kervax-rest-server/data" "$sandbox/app/data"
if default_guard 2>/dev/null; then exit 14; fi
`,
    encoding: 'utf8', timeout: 10000,
  })
  assert.equal(preflight.status, 0, preflight.stderr || `preflight exit ${preflight.status}`)
  await page.getByRole('button', { name: 'demo-lock', exact: false }).click()
  await page.getByRole('button', { name: 'Как снять лок', exact: true }).click()
  const unlock = unwrap(await cleanup.locator('pre').textContent())
  assert(unlock.includes(`DATA=${customData}`))
  assert.match(unlock, /"\$RESTIC" -r "\$REPO" unlock/)
  assert.match(unlock, /find "\$REPO"\/locks/)

  await page.goto(`${base}/?backupsrv=3&sec=cleanup`)
  await page.getByRole('button', { name: /массовая зачистка/ }).click()
  const bulk = await page.locator('.bulk-cleanup pre').textContent()
  assert(bulk.includes(`DATA=${customData}`))
  assert(!bulk.includes(unsafeRepo), 'Unsafe repository names must not be in the bulk script')
  const bulkSyntax = spawnSync(bash, [...args, '-n'], { input: bulk, encoding: 'utf8', timeout: 10000 })
  assert.equal(bulkSyntax.status, 0, bulkSyntax.stderr)

  await page.goto(`${base}/?backupsrv=4&sec=repos`)
  await page.getByRole('button', { name: 'Как удалить', exact: true }).click()
  await cleanup.getByText('Каталог бэкапов не распознан.', { exact: false }).waitFor()
  assert.equal(await cleanup.locator('pre').count(), 0)

  const script = await page.evaluate(async (dataDir) => {
    const { bulkCleanupScript } = await import('/src/bulkCleanupScript.ts')
    return bulkCleanupScript([{ name: 'safe-demo', snapshots: 0 }], dataDir, 'Demo\nprintf INJECTED')
  }, customData)
  assert.match(script, /^# .*Demo printf INJECTED\./m)
  assert.doesNotMatch(script, /^printf INJECTED/m)
  assert.deepEqual(errors, [], 'Browser runtime errors')
  console.log('PASS shell-safe backup restore, deletion and systemd arguments; clipboard success/failure')
} finally {
  await browser.close()
}
