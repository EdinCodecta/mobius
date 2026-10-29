const DAY = 24 * 60 * 60 * 1000

/** "6 days left" / "1 day left": how long an uninstalled app can still be recovered. */
export function expiryLabel(expiresAt, now = Date.now()) {
  const left = Date.parse(expiresAt) - now
  if (!Number.isFinite(left) || left <= 0) return 'Expiring now'
  const days = Math.floor(left / DAY)
  if (days >= 1) return `${days} ${days === 1 ? 'day' : 'days'} left`
  return '1 day left'
}

/** How urgent the expiry is, for the pill's colour: '' | 'soon' | 'urgent'. */
export function expiryTone(expiresAt, now = Date.now()) {
  const left = Date.parse(expiresAt) - now
  if (!Number.isFinite(left) || left < DAY) return 'urgent'
  return left < 3 * DAY ? 'soon' : ''
}

/** "Uninstalled on October 27": the month written out, and the year only when it is not this year. The
 *  App Store's note on an uninstalled app's page writes its dates the same way. */
export function removedLabel(deletedAt, now = new Date()) {
  const when = new Date(deletedAt)
  if (Number.isNaN(when.getTime())) return ''
  const options = { day: 'numeric', month: 'long' }
  if (when.getFullYear() !== now.getFullYear()) options.year = 'numeric'
  return `Uninstalled on ${when.toLocaleDateString(undefined, options)}`
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
