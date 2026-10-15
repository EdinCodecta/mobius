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
  const changes = changedPackages(null, head)
  assert.deepEqual(names(changes), ['string-width'])
  assert.equal(changes[0].misidentified, false)
})

test('a lockfile name or version cannot redirect the advisory lookup away from the downloaded package', () => {
  const renamed = { 'node_modules/evil': install('evil', '1.0.0', { name: 'lodash', version: '4.17.21' }) }
  const dotted = { 'node_modules/evil': install('evil', '1.0.0', { resolved: 'https://registry.npmjs.org/evil/../lodash/-/lodash-4.17.21.tgz' }) }
  const identity = packages => changedPackages(null, lock(packages)).map(({ name, version, misidentified }) => [name, version, misidentified])
  assert.deepEqual(identity(renamed), [['evil', '1.0.0', true]])
  assert.deepEqual(identity(dotted), [['lodash', '4.17.21', true]])
})

test('a registry URL that is not a plain tarball path is not trusted as a registry download', () => {
  for (const path of ['lodash/-/evil-1.0.0.tgz', '%65vil/-/%65vil-1.0.0.tgz', '@x%2Fevil/-/evil-1.0.0.tgz']) {
    const head = lock({ 'node_modules/evil': install('evil', '1.0.0', { resolved: `https://registry.npmjs.org/${path}` }) })
    assert.equal(changedPackages(null, head)[0].outsideRegistry, true, path)
  }
})

test('an entry without a version still counts, identified by its download', () => {
  const versionless = { resolved: 'https://registry.npmjs.org/evil/-/evil-1.0.0.tgz', integrity: 'sha512-evil' }
  const [change] = changedPackages(null, lock({ 'node_modules/evil': versionless }))
  assert.deepEqual([change.name, change.version, change.misidentified], ['evil', '1.0.0', true])
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
  assert.match(renderReport({ changes: [katex], advisories }), /Do not merge[\s\S]*\[`Malware in katex`\]\(https:\/\/github\.com\/advisories\/GHSA-x\) \(critical,/)
})

test('advisory text cannot inject Markdown, links or mentions into the comment', () => {
  const advisory = { title: 'x](https://evil.example)\n@someone', url: 'https://evil.example/)', severity: 'critical\n## Merge', vulnerable_versions: '*' }
  const text = renderReport({ changes: [katex], advisories: { katex: [advisory] } })
  assert.match(text, /^- `katex`: `x\]\(https:\/\/evil\.example\)@someone` \(unknown severity, affects `\*`\)$/m)
  const failure = renderReport({ changes: [katex], advisories: null, advisoryError: 'Unexpected token, "\n## Merge" is not valid JSON' })
  assert.doesNotMatch(failure, /^## Merge/m)
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
