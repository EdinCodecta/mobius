import assert from 'node:assert/strict'
import test from 'node:test'

import { changedPackages } from './lockfile-changes.mjs'
import { hasAdvisories, renderReport, REPORT_MARKER } from './report.mjs'

function install(name, version, extra = {}) {
  const resolved = `https://registry.npmjs.org/${name}/-/${name}-${version}.tgz`
  return { version, resolved, integrity: `sha512-${name}-${version}`, ...extra }
}

const lock = packages => ({ lockfileVersion: 3, packages: { '': { name: 'app' }, ...packages } })
const names = changes => changes.map(change => change.name)

test('reports only entries the base did not already install', () => {
  const base = lock({ 'node_modules/katex': install('katex', '0.18.9'), 'node_modules/acorn': install('acorn', '8.18.0') })
  const head = lock({ 'node_modules/katex': install('katex', '0.19.0'), 'node_modules/acorn': install('acorn', '8.18.0') })
  const changes = changedPackages(base, head)
  assert.deepEqual(names(changes), ['katex'])
  assert.equal(changes[0].newPackage, false)
})

test('a version that only moved within the tree is not a change', () => {
  const base = lock({ 'node_modules/a/node_modules/ms': install('ms', '2.1.3') })
  assert.deepEqual(changedPackages(base, lock({ 'node_modules/ms': install('ms', '2.1.3') })), [])
})

test('a tampered copy cannot hide behind an unchanged duplicate of the same version', () => {
  const base = lock({ 'node_modules/ms': install('ms', '2.1.3') })
  const head = lock({
    'node_modules/a/node_modules/ms': install('ms', '2.1.3', { integrity: 'sha512-swapped' }),
    'node_modules/ms': install('ms', '2.1.3'),
  })
  const [change, ...rest] = changedPackages(base, head)
  assert.deepEqual(rest, [])
  assert.equal(change.changedDownload, true)
})

test('flags new packages, new install scripts and downloads from outside the registry', () => {
  const base = lock({ 'node_modules/esbuild': install('esbuild', '0.1.0', { hasInstallScript: true }) })
  const head = lock({
    'node_modules/esbuild': install('esbuild', '0.2.0', { hasInstallScript: true }),
    'node_modules/sneaky': install('sneaky', '1.0.0', { hasInstallScript: true, resolved: 'https://example.com/s.tgz' }),
  })
  const [esbuild, sneaky] = changedPackages(base, head)
  assert.deepEqual([esbuild.newPackage, esbuild.newInstallScript, esbuild.outsideRegistry], [false, false, false])
  assert.deepEqual([sneaky.newPackage, sneaky.newInstallScript, sneaky.outsideRegistry], [true, true, true])
})

test('aliases use the real package name and workspace links are skipped', () => {
  const head = lock({
    'node_modules/string-width-cjs': install('string-width', '4.2.3', { name: 'string-width' }),
    'node_modules/local-app': { resolved: 'packages/local-app', link: true },
  })
  assert.deepEqual(names(changedPackages(null, head)), ['string-width'])
})

const katex = { name: 'katex', version: '0.19.0' }

test('a clean report starts with its marker and says nothing stood out', () => {
  const text = renderReport({ changes: [katex], advisories: {} })
  assert.equal(text.split('\n')[0], REPORT_MARKER)
  assert.match(text, /No known malware or vulnerabilities in the 1 changed package/)
  assert.match(text, /Nothing else stood out/)
})

test('advisories for a changed package block the merge and link the advisory', () => {
  const advisories = { katex: [{ title: 'Malware in katex', url: 'https://github.com/advisories/GHSA-x', severity: 'critical', vulnerable_versions: '=0.19.0' }] }
  assert.equal(hasAdvisories(advisories), true)
  assert.match(renderReport({ changes: [katex], advisories }), /Do not merge[\s\S]*\[Malware in katex\]\(https:\/\/github\.com\/advisories\/GHSA-x\)/)
})

test('an unreachable advisory database is reported as a failure, not as clean', () => {
  const text = renderReport({ changes: [katex], advisories: null, advisoryError: 'HTTP 503' })
  assert.match(text, /Could not check npm's advisory database/)
  assert.doesNotMatch(text, /No known malware/)
})

test('lockfile text cannot break out of its code span', () => {
  const text = renderReport({ changes: [{ name: 'x`\n@someone', version: '1.0.0', newPackage: true }], advisories: {} })
  assert.match(text, /- `x@someone@1\.0\.0`: new package/)
})
