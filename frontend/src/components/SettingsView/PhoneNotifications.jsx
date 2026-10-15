import { useCallback, useEffect, useRef, useState } from 'react'
import { Phone, X } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { isNativePushAvailable, isThisPhone } from '../../lib/nativePush.js'

function lastOpened(iso) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function phoneName(device, { inSentence = false } = {}) {
  const name = isThisPhone(device.tokenTail) ? 'This phone' : `Phone ···${device.tokenTail}`
  return inSentence ? name.charAt(0).toLowerCase() + name.slice(1) : name
}

/**
 * Phones registered for native push (the Möbius mobile app). Shown when any
 * phone is registered, or inside the app. A removed phone registers again the
 * next time Möbius opens on it; turning notifications off for the app in the
 * phone's settings stops them for good.
 */
export default function PhoneNotifications({ active = true }) {
  const [devices, setDevices] = useState([])
  const [confirming, setConfirming] = useState(null)
  const [removing, setRemoving] = useState(false)
  const dialogRef = useRef(null)

  const load = useCallback(async () => {
    try {
      const response = await api.push.devices.list()
      if (!response?.ok) return
      const body = await response.json()
      setDevices(Array.isArray(body) ? body : [])
    } catch {
      // Settings stays usable; the section simply shows nothing new.
    }
  }, [])

  useEffect(() => {
    if (active) load()
  }, [active, load])

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (confirming && !dialog.open) dialog.showModal?.()
    if (!confirming && dialog.open) dialog.close()
  }, [confirming])

  const remove = async () => {
    if (!confirming) return
    setRemoving(true)
    try {
      await api.push.devices.remove(confirming.id)
    } catch {
      // The refreshed list shows whether it is still there.
    }
    setRemoving(false)
    setConfirming(null)
    load()
  }

  if (devices.length === 0 && !isNativePushAvailable()) return null

  return (
    <section className="settings__section settings__section--compact">
      <span className="settings__label">Phone notifications</span>
      {devices.length === 0 ? (
        <p className="settings__subtext settings__subtext--tight">
          No phones yet. Open Möbius in the phone app and allow notifications.
        </p>
      ) : (
        <div className="settings-phones">
          {devices.map((device) => (
            <div className="settings-phone" key={device.id}>
              <span className="settings-phone__icon" aria-hidden="true"><Phone /></span>
              <span className="settings-phone__main">
                <span className="settings-phone__name">{phoneName(device)}</span>
                <span className="settings-phone__meta">Last opened {lastOpened(device.registeredAt)}</span>
              </span>
              <button
                className="settings-phone__remove"
                type="button"
                aria-label={`Remove ${phoneName(device)}`}
                onClick={() => setConfirming(device)}
              >
                <X />
              </button>
            </div>
          ))}
        </div>
      )}
      <dialog
        ref={dialogRef}
        className="settings-phone-confirm"
        aria-labelledby="settings-phone-confirm-title"
        onCancel={(event) => { event.preventDefault(); if (!removing) setConfirming(null) }}
      >
        <h3 id="settings-phone-confirm-title" className="settings-phone-confirm__title">
          Remove {confirming ? phoneName(confirming, { inSentence: true }) : 'phone'}?
        </h3>
        <p className="settings__subtext settings__subtext--tight">
          It stops getting notifications until Möbius opens on it again. To stop them for good,
          turn notifications off for the Möbius app in the phone's settings.
        </p>
        <div className="settings-phone-confirm__actions">
          <button
            className="settings__btn settings__btn--outline settings__btn--sm"
            type="button"
            disabled={removing}
            onClick={() => setConfirming(null)}
          >
            Cancel
          </button>
          <button
            className="settings__btn settings__btn--sm"
            type="button"
            disabled={removing}
            onClick={remove}
          >
            {removing ? 'Removing…' : 'Remove'}
          </button>
        </div>
      </dialog>
    </section>
  )
}
