const MINUTE = 60 * 1000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

// The last day counts down in rough steps. Each label is an upper bound, so
// "~6 hrs left" means somewhere between one and six hours remain.
const LAST_DAY_STEPS = [[HOUR, '~1 hr'], [6 * HOUR, '~6 hrs'], [12 * HOUR, '~12 hrs'], [DAY, '~1 day']]

/**
 * How long an uninstalled app can still be recovered, or null once it is gone.
 *
 * `label` is "6 days left" … "1 day left" in whole days, then "~1 day left",
 * "~12 hrs left", "~6 hrs left" and "~1 hr left" on the last day. It is gone at
 * zero, the moment the server stops accepting a recover. `tone` colours the
 * pill: '' | 'soon' (under 3 days) | 'urgent' (the last day). `nextChangeIn`
 * is how many milliseconds until the label changes, so a view can wake exactly
 * then instead of polling.
 */
export function expiryCountdown(expiresAt, now = Date.now()) {
  const left = Date.parse(expiresAt) - now
  if (!Number.isFinite(left) || left <= 0) return null
  const tone = left < DAY ? 'urgent' : left < 3 * DAY ? 'soon' : ''
  if (left >= DAY) {
    const days = Math.floor(left / DAY)
    return {
      label: `${days} ${days === 1 ? 'day' : 'days'} left`,
      tone,
      nextChangeIn: left - days * DAY + 1,
    }
  }
  const step = LAST_DAY_STEPS.findIndex(([limit]) => left <= limit)
  const below = step === 0 ? 0 : LAST_DAY_STEPS[step - 1][0]
  return { label: `${LAST_DAY_STEPS[step][1]} left`, tone, nextChangeIn: left - below }
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
