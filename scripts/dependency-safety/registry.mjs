// npm registry lookups for changed packages. The bulk advisory endpoint is the
// one `npm audit` uses; it includes GitHub's malware advisories and returns
// only advisories that affect the exact versions asked about.

export const REGISTRY = 'https://registry.npmjs.org'
const REQUEST_TIMEOUT_MS = 30_000

export async function findAdvisories(changes) {
  const versionsByName = Object.groupBy(changes, change => change.name)
  const query = Object.fromEntries(
    Object.entries(versionsByName).map(([name, group]) => [name, group.map(change => change.version)]),
  )
  const response = await fetch(`${REGISTRY}/-/npm/v1/security/advisories/bulk`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(query),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
  if (!response.ok) throw new Error(`npm advisory lookup returned HTTP ${response.status}`)
  return response.json()
}

async function publishedAt(change) {
  try {
    const name = change.name.replace('/', '%2F')
    const response = await fetch(`${REGISTRY}/${name}`, { signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) })
    if (!response.ok) return null
    const packument = await response.json()
    return packument.time?.[change.version] ?? null
  } catch {
    // An unknown date is reported as such rather than failing the whole check.
    return null
  }
}

export async function publishDates(changes) {
  const dates = new Map()
  // One request at a time: a regenerated lockfile can change hundreds of
  // packages, and a burst of full packument downloads gets rate limited.
  for (const change of changes) {
    dates.set(`${change.name}@${change.version}`, await publishedAt(change))
  }
  return dates
}
