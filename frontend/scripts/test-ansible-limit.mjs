// Run against local Vite with ansible-core on Linux or WSL. Only list synthetic hosts.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { chromium } from 'playwright-core'

const base = new URL(process.env.KERVAX_TEST_BASE_URL || 'http://127.0.0.1:5334')
assert(['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname))
const browser = await chromium.launch({ executablePath: process.env.PW_CHROME, headless: true })
let command
try {
  const page = await browser.newPage()
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== base.origin) return route.abort()
    return url.pathname.startsWith('/api/') ? route.fulfill({ json: {} }) : route.continue()
  })
  await page.goto(base.href)
  command = await page.evaluate(async () => {
    const { helperCmd } = await import('/src/FleetUpdate.tsx')
    const expected = 'ansible-playbook playbooks/kervax_helpers.yml -l kervax_outdated'
    if (helperCmd([]) !== null) throw new Error('Empty rollout has a command')
    for (const hosts of [['node-a', 'node.b'], ['single_alias'], ['absent-node'], ['all'],
      ['*'], ['kervax_outdated'], ['node-a:node-b'], ['@selection'], ['-v'], ['node-a\n'],
      ['node-a; touch injected']]) {
      if (helperCmd(hosts) !== expected) throw new Error('Reported names changed the fixed command')
    }
    return helperCmd(['single_alias', 'node.b'])
  })
  assert(command)
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

[single_alias]
node-a

[kervax_outdated]
node-a
node.b
EOF
mkdir playbooks
cat >playbooks/kervax_helpers.yml <<'EOF'
- hosts: all
  gather_facts: false
  tasks: []
EOF
actual=$(${command} --list-hosts)
[[ $actual == *node-a* && $actual == *node.b* && $actual != *unrelated-node* ]]
# The controller, rather than a stale copied host list, chooses the next targets.
cat >hosts <<'EOF'
[kervax_outdated]
unrelated-node

[current]
node-a
node.b
EOF
actual=$(${command} --list-hosts)
[[ $actual == *unrelated-node* && $actual != *node-a* && $actual != *node.b* ]]
`
const windows = process.platform === 'win32'
const result = spawnSync(windows ? 'wsl' : 'bash', windows ? ['--cd', '/', '--', 'bash', '-s'] : ['-s'], {
  input: fixture, encoding: 'utf8',
})
assert.ifError(result.error)
assert.equal(result.status, 0, result.stdout + result.stderr)
console.log('PASS Fixed helper command uses current inventory targets and never interpolates reported names')
