// Run against local Vite with ansible-core on Linux or WSL. Only list synthetic hosts.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { chromium } from 'playwright-core'

const base = new URL(process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5334')
assert(['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname))
const browser = await chromium.launch({ executablePath: process.env.PW_CHROME, headless: true })
let commands
try {
  const page = await browser.newPage()
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== base.origin) return route.abort()
    return url.pathname.startsWith('/api/') ? route.fulfill({ json: {} }) : route.continue()
  })
  await page.goto(base.href)
  commands = await page.evaluate(async () => {
    const { helperCmd } = await import('/src/FleetUpdate.tsx')
    for (const hosts of [[], [''], ['all'], ['*'], ['kervax_outdated'], ['node-a,node-b'],
      ['node-a:node-b'], ['!node-a'], ['node-a[0]'], ['@selection'], ['-v'], ['node-a\n']]) {
      if (helperCmd(hosts) !== null) throw new Error(`Unsafe aliases accepted: ${JSON.stringify(hosts)}`)
    }
    return [helperCmd(['node-a', 'node.b']), helperCmd(['fixture_group']), helperCmd(['absent-node'])]
  })
  assert(commands.every(Boolean))
} finally {
  await browser.close()
}

const fixture = `
set -euo pipefail
fixture=$(mktemp -d)
trap 'rm -rf -- "$fixture"' EXIT
cd "$fixture"
export ANSIBLE_CONFIG="$fixture/ansible.cfg"
touch "$ANSIBLE_CONFIG"
export ANSIBLE_INVENTORY="$fixture/hosts"
cat >hosts <<'EOF'
[fixture_group]
node-a
node.b
unrelated-node
EOF
mkdir playbooks
cat >playbooks/kervax_helpers.yml <<'EOF'
- hosts: all
  gather_facts: false
  tasks: []
EOF
legacy=$(ansible-playbook playbooks/kervax_helpers.yml --limit=fixture_group --list-hosts)
[[ $legacy == *unrelated-node* ]]
actual=$(${commands[0]} --list-hosts)
[[ $actual == *node-a* && $actual == *node.b* && $actual != *unrelated-node* ]]
if ${commands[1]} --list-hosts; then exit 11; fi
if ${commands[2]} --list-hosts; then exit 12; fi
`
const windows = process.platform === 'win32'
const result = spawnSync(windows ? 'wsl' : 'bash', windows ? ['--cd', '/', '--', 'bash', '-s'] : ['-s'], {
  input: fixture, encoding: 'utf8',
})
assert.ifError(result.error)
assert.equal(result.status, 0, result.stdout + result.stderr)
console.log('PASS Ansible selects exactly the named hosts and refuses groups, patterns, and absent aliases')
