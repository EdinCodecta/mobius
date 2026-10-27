const DAY = 24 * 60 * 60 * 1000

/** "Deleted for good in 6 days" / "in under a day" / "today" for an expiry. */
export function expiryLabel(expiresAt, now = Date.now()) {
  const left = Date.parse(expiresAt) - now
  if (!Number.isFinite(left) || left <= 0) return 'Deleted for good soon'
  const days = Math.floor(left / DAY)
  if (days >= 1) return `Deleted for good in ${days} ${days === 1 ? 'day' : 'days'}`
  return 'Deleted for good in under a day'
}

export function removedLabel(deletedAt) {
  const when = new Date(deletedAt)
  if (Number.isNaN(when.getTime())) return ''
  return `Removed ${when.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`
}

/** The server's reason for a failed recover, in its own words when it gave one. */
export async function recoverErrorMessage(response) {
  try {
    const body = await response.json()
    if (typeof body?.detail === 'string' && body.detail) return body.detail
  } catch {
    // fall through to the generic text
  }
  return 'Could not recover this app. Try again.'
}
