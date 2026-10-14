import { useEffect } from 'react'
import { resumeNativePush } from '../lib/nativePush.js'

/**
 * Re-arms native push in the Möbius mobile app after login and on every load.
 *
 * Only a phone whose owner turned notifications on (Settings) re-registers:
 * the first registration can raise the OS prompt, so it waits for that tap.
 * Re-registering on load is what lets the page hear `mobius:push-token`
 * rotations, which the app only delivers after a call in the same page.
 */
export default function useNativePushResume() {
  useEffect(() => {
    resumeNativePush().catch(() => {})
  }, [])
}
