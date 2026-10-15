// Which package installs a pull request adds or alters in an npm lockfile
// (v2/v3). Every entry is compared on its own, so a tampered copy cannot hide
// behind an unchanged duplicate of the same version elsewhere in the tree.

import { REGISTRY } from './registry.mjs'

const NODE_MODULES = 'node_modules/'

function packageName(installPath, entry) {
  // Aliased installs record the real package name; others are named by path.
  return entry.name ?? installPath.slice(installPath.lastIndexOf(NODE_MODULES) + NODE_MODULES.length)
}

function installs(lock) {
  return Object.entries(lock?.packages ?? {})
    .filter(([installPath, entry]) => installPath && entry.version && !entry.link)
    .map(([installPath, entry]) => ({ ...entry, name: packageName(installPath, entry) }))
}

// Everything that decides which code npm downloads and whether it runs on install.
const fingerprint = entry =>
  [entry.name, entry.version, entry.integrity, entry.resolved, Boolean(entry.hasInstallScript)].join(' ')

function describeChange(entry, previousVersions) {
  const resolved = entry.resolved ?? ''
  return {
    name: entry.name,
    version: entry.version,
    newPackage: previousVersions.length === 0,
    changedDownload: previousVersions.some(old => old.version === entry.version),
    newInstallScript: Boolean(entry.hasInstallScript) && !previousVersions.some(old => old.hasInstallScript),
    outsideRegistry: resolved !== '' && !resolved.startsWith(`${REGISTRY}/`),
  }
}

export function changedPackages(baseLock, headLock) {
  const base = installs(baseLock)
  const known = new Set(base.map(fingerprint))
  const changed = new Map(installs(headLock)
    .filter(entry => !known.has(fingerprint(entry)))
    .map(entry => [fingerprint(entry), entry]))
  return [...changed.values()].map(entry => describeChange(entry, base.filter(old => old.name === entry.name)))
}
