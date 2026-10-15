// Which package installs a pull request adds to an npm lockfile (v2/v3).
// A registry version is immutable, so a name@version the base already
// installed with the same integrity is the same code and is not reported.

import { REGISTRY } from './registry.mjs'

const REGISTRY_TARBALL_PREFIX = `${REGISTRY}/`
const NODE_MODULES = 'node_modules/'

function packageName(installPath, entry) {
  // Aliased installs record the real package name; others are named by path.
  return entry.name ?? installPath.slice(installPath.lastIndexOf(NODE_MODULES) + NODE_MODULES.length)
}

function installsByNameAndVersion(lock) {
  const installs = Object.entries(lock?.packages ?? {})
    .filter(([installPath, entry]) => installPath && entry.version && !entry.link)
    .map(([installPath, entry]) => ({ ...entry, name: packageName(installPath, entry) }))
  return new Map(installs.map(entry => [`${entry.name}@${entry.version}`, entry]))
}

function describeChange(entry, sameVersion, previousVersions) {
  const resolved = entry.resolved ?? ''
  return {
    name: entry.name,
    version: entry.version,
    from: previousVersions.map(old => old.version).filter(version => version !== entry.version),
    newPackage: previousVersions.length === 0,
    integrityChanged: Boolean(sameVersion),
    newInstallScript: Boolean(entry.hasInstallScript) && !previousVersions.some(old => old.hasInstallScript),
    outsideRegistry: resolved !== '' && !resolved.startsWith(REGISTRY_TARBALL_PREFIX),
  }
}

export function changedPackages(baseLock, headLock) {
  const base = installsByNameAndVersion(baseLock)
  const baseByName = Map.groupBy(base.values(), entry => entry.name)
  const alreadyInstalled = (key, entry) => base.has(key) && base.get(key).integrity === entry.integrity
  return [...installsByNameAndVersion(headLock)]
    .filter(([key, entry]) => !alreadyInstalled(key, entry))
    .map(([key, entry]) => describeChange(entry, base.get(key), baseByName.get(entry.name) ?? []))
}
