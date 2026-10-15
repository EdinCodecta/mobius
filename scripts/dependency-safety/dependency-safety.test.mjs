import assert from 'node:assert/strict'
import test from 'node:test'

import { changedPackages } from './lockfile-changes.mjs'
import { hasAdvisories, packageNotes, renderReport, REPORT_MARKER } from './report.mjs'

const tarball = (name, version) => `https://registry.npmjs.org/${name}/-/${name}-${version}.tgz`

function install(name, version, extra = {}) {
  return { version, resolved: tarball(name, version), integrity: `sha512-${name}-${version}`, ...extra }
}

function lock(packages) {
  return { lockfileVersion: 3, packages: { '': { name: 'app' }, ...packages } }
}

const NOW = new Date('2026-10-15T12:00:00Z')
const OLD_CHANGE = { name: 'katex', version: '0.19.0', from: ['0.18.9'] }

test('reports only package versions the base did not already install', () => {
  const base = lock({ 'node_modules/katex': install('katex', '0.18.9'), 'node_modules/acorn': install('acorn', '8.18.0') })
  const head = lock({ 'node_modules/katex': install('katex', '0.19.0'), 'node_modules/acorn': install('acorn', '8.18.0') })
  const [change, ...rest] = changedPackages(base, head)
  assert.deepEqual(rest, [])
  assert.equal(change.name, 'katex')
  assert.deepEqual(change.from, ['0.18.9'])
  assert.equal(change.newPackage, false)
})

test('a version that only moved within the tree is not a change', () => {
  const base = lock({ 'node_modules/a/node_modules/ms': install('ms', '2.1.3') })
  const head = lock({ 'node_modules/ms': install('ms', '2.1.3') })
  assert.deepEqual(changedPackages(base, head), [])
})

test('the same version with different contents is flagged', () => {
  const base = lock({ 'node_modules/ms': install('ms', '2.1.3') })
  const head = lock({ 'node_modules/ms': install('ms', '2.1.3', { integrity: 'sha512-swapped' }) })
  const [change] = changedPackages(base, head)
  assert.equal(change.integrityChanged, true)
  assert.deepEqual(packageNotes(change, '2020-01-01T00:00:00Z', NOW), ['same version, different contents'])
})

test('a package the base never installed is new', () => {
  const [change] = changedPackages(lock({}), lock({ 'node_modules/left-pad': install('left-pad', '1.3.0') }))
  assert.equal(change.newPackage, true)
  assert.deepEqual(change.from, [])
})

test('an install script is flagged only when the package did not already run one', () => {
  const base = lock({ 'node_modules/esbuild': install('esbuild', '0.1.0', { hasInstallScript: true }) })
  const head = lock({
    'node_modules/esbuild': install('esbuild', '0.2.0', { hasInstallScript: true }),
    'node_modules/sneaky': install('sneaky', '1.0.0', { hasInstallScript: true }),
  })
  const flagged = changedPackages(base, head).filter(change => change.newInstallScript).map(change => change.name)
  assert.deepEqual(flagged, ['sneaky'])
})

test('a tarball from outside the npm registry is flagged', () => {
  const head = lock({ 'node_modules/ms': install('ms', '9.9.9', { resolved: 'https://example.com/ms.tgz' }) })
  assert.equal(changedPackages(lock({}), head)[0].outsideRegistry, true)
})

test('aliases use the real package name and workspace links are skipped', () => {
  const head = lock({
    'node_modules/string-width-cjs': install('string-width', '4.2.3', { name: 'string-width' }),
    'node_modules/local-app': { resolved: 'packages/local-app', link: true },
  })
  assert.deepEqual(changedPackages(null, head).map(change => change.name), ['string-width'])
})

test('releases younger than the cooldown are flagged and older ones are not', () => {
  assert.deepEqual(packageNotes(OLD_CHANGE, '2026-10-13T12:00:00Z', NOW), ['published 2 day(s) ago'])
  assert.deepEqual(packageNotes(OLD_CHANGE, '2026-10-01T12:00:00Z', NOW), [])
  assert.deepEqual(packageNotes(OLD_CHANGE, null, NOW), ['publish date unknown'])
})

function report(advisories, advisoryError = null) {
  const dates = new Map([['katex@0.19.0', '2026-10-01T12:00:00Z']])
  const sections = [{ lockfile: 'frontend/package-lock.json', changes: [OLD_CHANGE] }]
  return renderReport({ sections, advisories, advisoryError, dates, now: NOW })
}

test('a clean report starts with its marker and says nothing stood out', () => {
  const text = report({})
  assert.equal(text.split('\n')[0], REPORT_MARKER)
  assert.match(text, /No known malware or vulnerabilities in the 1 changed package/)
  assert.match(text, /Nothing else stood out/)
  assert.match(text, /\| `katex` \| 0\.18\.9 \| 0\.19\.0 \| 2026-10-01 \|/)
})

test('advisories for a changed package block the merge and link the advisory', () => {
  const advisories = { katex: [{ title: 'Malware in katex', url: 'https://github.com/advisories/GHSA-x', severity: 'critical', vulnerable_versions: '=0.19.0' }] }
  assert.equal(hasAdvisories(advisories), true)
  assert.match(report(advisories), /Do not merge until resolved[\s\S]*\[Malware in katex\]\(https:\/\/github\.com\/advisories\/GHSA-x\)/)
})

test('an unreachable advisory database is reported as not checked, not as clean', () => {
  const text = report(null, 'npm advisory lookup returned HTTP 503')
  assert.equal(hasAdvisories(null), false)
  assert.match(text, /\*\*not checked\*\*/)
  assert.doesNotMatch(text, /No known malware/)
})
