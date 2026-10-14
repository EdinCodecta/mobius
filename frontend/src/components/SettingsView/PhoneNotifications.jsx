import { useState, useSyncExternalStore } from 'react'
import { api } from '../../api/client.js'
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
  error: "Couldn't turn on notifications for this phone. Try again.",
}

/** Settings row for native push; rendered only inside the Möbius mobile app. */
export default function PhoneNotifications() {
  const pushState = useSyncExternalStore(
    subscribeNativePush, getNativePushState, getNativePushState,
  )
  const [testState, setTestState] = useState('idle')
  if (!isNativePushAvailable()) return null

  const enabled = isNativePushEnabled()
  const status = pushState.status === 'idle' ? (enabled ? 'on' : 'off') : pushState.status
  const busy = status === 'registering'

  async function sendTest() {
    setTestState('sending')
    try {
      const response = await api.push.devices.sendTest()
      setTestState(response?.ok ? 'sent' : 'failed')
    } catch {
      setTestState('failed')
    }
  }

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
            disabled={busy}
            onClick={() => { enableNativePush() }}
          >
            {busy ? 'Turning on…' : 'Turn on'}
          </button>
        )}
      </div>
      <p className="settings__subtext settings__subtext--tight" role="status">
        {STATUS_TEXT[status] || STATUS_TEXT.off}
      </p>
      {enabled && (
        <div className="settings__row">
          <span className="settings__subtext settings__subtext--tight">
            {testState === 'sent' && 'Test sent. It should arrive in a few seconds.'}
            {testState === 'failed' && "Couldn't send a test notification."}
          </span>
          <button
            className="settings__btn settings__btn--outline settings__btn--sm"
            type="button"
            disabled={testState === 'sending'}
            onClick={sendTest}
          >
            Send test
          </button>
        </div>
      )}
    </section>
  )
}
