/* Read-only preview and reviewed install for the guide's app cards. The rows the owner reviews come
   from the server with the preview (`access_rows`), the same rows the first-open permission request
   shows, so there is one copy of the wording. */
import { apiFetch } from '../../api/client.js'
import { detailToMessage } from '../../lib/errorDetail.js'

const DEFAULT_ERROR = 'This app’s access could not be checked right now.'

/* Read-only: fetches the app's manifest and returns its capability contract,
   digest and access rows. Nothing is installed. */
export async function previewAppAccess(manifestUrl, signal) {
  const response = await apiFetch('/apps/preview', { method: 'POST', body: JSON.stringify({ manifest_url: manifestUrl }), signal, timeoutMs: 20_000 })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(detailToMessage(data.detail, DEFAULT_ERROR))
  return data
}

/* Installs exactly what the reviewer saw: the server refuses if the app's
   capabilities changed after `reviewedDigest` was computed. Resolves with
   { status: 'installed' } or { status: 'changed', preview } when the owner
   must review the new access first. */
export async function installReviewedApp(manifestUrl, reviewedDigest) {
  const response = await apiFetch('/apps/install', { method: 'POST', body: JSON.stringify({ manifest_url: manifestUrl, reviewed_capability_digest: reviewedDigest }), timeoutMs: 120_000 })
  const data = await response.json().catch(() => ({}))
  if (response.status === 409 && data.detail?.code === 'capability_changed') {
    return { status: 'changed', preview: { capability_contract: data.detail.capability_contract, capability_digest: data.detail.capability_digest, access_rows: data.detail.access_rows } }
  }
  if (!response.ok) throw new Error(detailToMessage(data.detail, 'The app could not be installed.'))
  return { status: 'installed', warnings: Array.isArray(data.warnings) ? data.warnings : [] }
}
