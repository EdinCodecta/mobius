import { useEffect, useState } from 'react'
import { expiryCountdown } from '../components/Drawer/uninstalledApps.js'

/**
 * The rows that can still be recovered, each with its countdown.
 *
 * A row leaves the list the moment its countdown reaches zero, the same
 * instant the server starts refusing to recover it. The view wakes once, when
 * the soonest label changes, and times that wake against the real clock: rows
 * that arrive or change between wakes were measured against an older moment,
 * so a deadline that has already passed is caught up at once.
 */
export default function useExpiryCountdowns(rows) {
  const [now, setNow] = useState(() => Date.now())
  const live = rows
    .map(row => ({ row, countdown: expiryCountdown(row.expires_at, now) }))
    .filter(entry => entry.countdown)
  const nextChangeAt = Math.min(...live.map(entry => entry.countdown.changesAt))

  useEffect(() => {
    if (!Number.isFinite(nextChangeAt)) return undefined
    const timer = setTimeout(() => setNow(Date.now()), Math.max(0, nextChangeAt - Date.now()))
    return () => clearTimeout(timer)
  }, [now, nextChangeAt])

  return live
}
