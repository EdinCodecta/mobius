// npm's bulk advisory endpoint, the one `npm audit` uses. It includes GitHub's
// malware advisories and returns only advisories that affect the exact
// versions asked about.

export const REGISTRY = 'https://registry.npmjs.org'

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
