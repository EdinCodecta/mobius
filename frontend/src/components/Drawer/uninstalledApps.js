const DAY = 24 * 60 * 60 * 1000

/** "6 days left" / "Under a day left" until an app is deleted for good. */
export function expiryLabel(expiresAt, now = Date.now()) {
  const left = Date.parse(expiresAt) - now
  if (!Number.isFinite(left) || left <= 0) return 'Expiring now'
  const days = Math.floor(left / DAY)
  if (days >= 1) return `${days} ${days === 1 ? 'day' : 'days'} left`
  return 'Under a day left'
}

/** How urgent the expiry is, for the pill's colour: '' | 'soon' | 'urgent'. */
export function expiryTone(expiresAt, now = Date.now()) {
  const left = Date.parse(expiresAt) - now
  if (!Number.isFinite(left) || left < DAY) return 'urgent'
  return left < 3 * DAY ? 'soon' : ''
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
