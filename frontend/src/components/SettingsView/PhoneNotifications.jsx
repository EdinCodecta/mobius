import { useSyncExternalStore } from 'react'
import {
  disableNativePush,
  enableNativePush,
  getNativePushState,
  isNativePushAvailable,
  isNativePushEnabled,
  subscribeNativePush,
} from '../../lib/nativePush.js'

const STATUS_TEXT = {
  off: 'Get notifications on this phone, even when Möbius is closed.',
  registering: 'Turning on…',
  on: 'Notifications are on for this phone.',
  denied: "Notifications are turned off for Möbius in this phone's settings.",
}

/** Settings row for native push; rendered only inside the Möbius mobile app. */
export default function PhoneNotifications() {
  const pushState = useSyncExternalStore(
    subscribeNativePush, getNativePushState, getNativePushState,
  )
  if (!isNativePushAvailable()) return null

  const enabled = isNativePushEnabled()
  const status = pushState.status === 'idle' ? (enabled ? 'on' : 'off') : pushState.status
  const message = status === 'error'
    ? pushState.error || "Couldn't turn on notifications for this phone. Try again."
    : STATUS_TEXT[status] || STATUS_TEXT.off

  return (
    <section className="settings__section settings__section--compact">
      <div className="settings__row">
        <span className="settings__label">Phone notifications</span>
        {enabled ? (
          <button
            className="settings__btn settings__btn--outline settings__btn--sm"
            type="button"
            onClick={() => { disableNativePush().catch(() => {}) }}
          >
            Turn off
          </button>
        ) : (
          <button
            className="settings__btn settings__btn--sm"
            type="button"
            disabled={status === 'registering'}
            onClick={() => { enableNativePush() }}
          >
            {status === 'registering' ? 'Turning on…' : 'Turn on'}
          </button>
        )}
      </div>
      <p className="settings__subtext settings__subtext--tight" role="status">
        {message}
      </p>
    </section>
  )
}
