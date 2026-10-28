/* Install for the guide's app cards: one request, no check first. What an app may do is asked the
   first time it opens (its permission request), so the guide does not repeat it here. */
import { apiFetch } from '../../api/client.js'
import { detailToMessage } from '../../lib/errorDetail.js'

/* Resolves with { warnings }; throws a readable Error when the install is refused. */
export async function installFromCatalog(manifestUrl) {
  const response = await apiFetch('/apps/install', { method: 'POST', body: JSON.stringify({ manifest_url: manifestUrl }), timeoutMs: 120_000 })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(detailToMessage(data.detail, 'The app could not be installed.'))
  return { warnings: Array.isArray(data.warnings) ? data.warnings : [] }
}
