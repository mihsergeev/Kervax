import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'

// Temporary mitigation for GHSA-fc5q-39xc-vjpf while upstream has no patched release.
// The frontend uses ordinary file globs and does not need repeated extglobs.
// Keep the original lockfile version so npm can report future advisories for it.
const require = createRequire(import.meta.url)
const lock = JSON.parse(readFileSync(new URL('../package-lock.json', import.meta.url), 'utf8'))
const installed = lock.packages['node_modules/picomatch']
const digest = (value) => createHash('sha256').update(value).digest('hex')
const original = '4e1e05447668aa1e94366c5516a2bcd8f5fff800cc92b1d0cd7d77703b8618c4'
const patched = '72b618a4a51970bab6faf023852684934ce6a716bef2e4bcc9418037a7070875'
const before = '    const analysis = analyzeRepeatedExtglob(body, opts);'
const after = `    // Build globs do not need unbounded extglob repetition.
    const analysis = token.type === 'plus' || token.type === 'star'
      ? { risky: true }
      : analyzeRepeatedExtglob(body, opts);`

function parserPath() {
  try {
    return require.resolve('picomatch/lib/parse.js')
  } catch (error) {
    if (error.code === 'MODULE_NOT_FOUND' && !process.argv.includes('--audit')) return null
    throw error
  }
}

function verifyLock() {
  const copies = Object.entries(lock.packages).filter(([name]) => name.endsWith('/node_modules/picomatch') || name === 'node_modules/picomatch')
  assert.equal(copies.length, 1, 'Review all installed picomatch copies')
  assert.equal(installed.version, '4.0.7', 'Review the glob mitigation for the new installed version')
  assert.equal(installed.dev, true, 'The mitigation applies only to frontend build tooling')
}

const path = parserPath()
if (path) {
  verifyLock()
  const parser = readFileSync(path, 'utf8')
  if (!process.argv.includes('--audit') && digest(parser) === original) {
    assert.equal(parser.split(before).length, 2, 'Expected exactly one extglob analysis site')
    const restricted = parser.replace(before, after)
    assert.equal(digest(restricted), patched, 'Unexpected patched parser bytes')
    writeFileSync(path, restricted)
  }
  assert.equal(digest(readFileSync(path)), patched, 'The installed glob parser is not patched')
}

if (process.argv.includes('--audit')) {
  // npm_execpath is the same npm CLI that starts the script, including on Windows.
  const audit = spawnSync(process.execPath, [process.env.npm_execpath, 'audit', '--audit-level=high', '--json'], { encoding: 'utf8' })
  if (audit.error) throw audit.error
  const report = JSON.parse(audit.stdout)
  const counts = report.metadata?.vulnerabilities
  assert(!audit.signal && !report.error && Number.isInteger(counts?.high) &&
    Number.isInteger(counts?.critical) && report.vulnerabilities,
    audit.stderr || 'npm audit did not return a vulnerability report')
  if (audit.status !== 0) {
    const known = 'https://github.com/advisories/GHSA-fc5q-39xc-vjpf'
    function onlyPatchedAdvisory(name, seen = new Set()) {
      if (seen.has(name) || !Object.hasOwn(report.vulnerabilities, name)) return false
      const entry = report.vulnerabilities[name]
      const node = `node_modules/${name}`
      if (entry.severity !== 'high' || entry.nodes.length !== 1 || entry.nodes[0] !== node ||
          lock.packages[node]?.dev !== true || entry.via.length === 0) return false
      if (name === 'picomatch') {
        const [cause] = entry.via
        return entry.via.length === 1 && cause.url === known &&
          cause.dependency === 'picomatch' && cause.range === '<=4.0.7'
      }
      const path = new Set([...seen, name])
      return entry.via.every((cause) => typeof cause === 'string' && onlyPatchedAdvisory(cause, path))
    }
    const high = Object.entries(report.vulnerabilities).filter(([, entry]) => entry.severity === 'high')
    if (!(audit.status === 1 && counts.critical === 0 && high.length > 0 && high.length === counts.high &&
          high.every(([name]) => onlyPatchedAdvisory(name)))) {
      process.stderr.write(audit.stdout)
      process.exitCode = 1
    } else {
      console.log('npm audit: GHSA-fc5q-39xc-vjpf mitigated by the verified build-only parser patch')
    }
  } else {
    assert.equal(counts.high + counts.critical, 0, 'npm audit returned inconsistent severity counts')
    console.log('npm audit: no high or critical advisories')
  }
}
