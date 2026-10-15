// npm's bulk advisory endpoint, the one `npm audit` uses. It includes GitHub's
// malware advisories and returns only advisories that affect the exact
// versions asked about.

export const REGISTRY = 'https://registry.npmjs.org'

// <name>/-/<unscoped name>-<version>.tgz, allowing only characters a package
// name or version can hold, so no percent-encoding can point somewhere else.
const TARBALL_PATH = /^\/((?:@[\w.!~*'()-]+\/)?([\w.!~*'()-]+))\/-\/\2-([\w.+-]+)\.tgz$/

// The package and version a registry tarball URL downloads, or null for any
// other URL. It is parsed the way fetch parses it, so `..` segments resolve first.
export function registryTarball(resolved) {
  const url = URL.parse(resolved)
  const match = url?.origin === REGISTRY ? url.pathname.match(TARBALL_PATH) : null
  return match && { name: match[1], version: match[3] }
}

export async function findAdvisories(changes) {
  const byName = Object.groupBy(changes, change => change.name)
  const query = Object.fromEntries(
    Object.entries(byName).map(([name, group]) => [name, group.map(change => change.version)]),
  )
  const response = await fetch(`${REGISTRY}/-/npm/v1/security/advisories/bulk`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(query),
    signal: AbortSignal.timeout(30_000),
  })
  if (!response.ok) throw new Error(`npm advisory lookup returned HTTP ${response.status}`)
  return response.json()
}
