import { useCallback, useEffect, useRef, useState } from 'react'
import { Phone, X } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { disconnectPhone, isThisPhone } from '../../lib/nativePush.js'

function lastOpened(iso) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function phoneName(device) {
  return isThisPhone(device.tokenTail) ? 'This phone' : `Phone ···${device.tokenTail}`
}

/**
 * Phones connected for native push (the Möbius mobile app). A phone connects
 * by itself when the app opens with notifications allowed in the phone's own
 * settings; Möbius has no on/off switch. Here the owner sees the connected
 * phones and can disconnect any of them.
 */
export default function PhoneNotifications({ active = true }) {
  const [devices, setDevices] = useState([])
  const [confirming, setConfirming] = useState(null)
  const [removing, setRemoving] = useState(false)
  const [message, setMessage] = useState('')
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

  const confirmDisconnect = async () => {
    setRemoving(true)
    setMessage((await disconnectPhone(confirming)) || '')
    setRemoving(false)
    setConfirming(null)
    load()
  }

  const confirmingThisPhone = !!confirming && isThisPhone(confirming.tokenTail)

  return (
    <section className="settings__section settings__section--compact">
      <div className="settings__row">
        <span className="settings__label">Phone notifications</span>
      </div>
      {message && (
        <p className="settings__subtext settings__subtext--tight" role="status">{message}</p>
      )}
      <p className="settings__subtext settings__subtext--tight">
        {devices.length > 0
          ? 'These phones get your Möbius notifications. To stop them on a phone, turn off notifications for Möbius in its settings.'
          : 'No phones connected. A phone connects when the Möbius app is opened on it with notifications allowed.'}
      </p>
      {devices.length > 0 && (
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
                aria-label={`Disconnect ${phoneName(device).toLowerCase()}`}
                onClick={() => setConfirming(device)}
              >
                <X />
              </button>
            </div>
          ))}
        </div>
      )}
      <p className="settings__subtext settings__subtext--tight">
        To reach your phone, notification titles and text pass through mobius.you and Google or Apple.
      </p>
      <dialog
        ref={dialogRef}
        className="settings-phone-confirm"
        aria-labelledby="settings-phone-confirm-title"
        onCancel={(event) => { event.preventDefault(); if (!removing) setConfirming(null) }}
      >
        <h3 id="settings-phone-confirm-title" className="settings-phone-confirm__title">
          {confirmingThisPhone ? 'Disconnect this phone?' : `Disconnect phone ···${confirming?.tokenTail}?`}
        </h3>
        <p className="settings__subtext settings__subtext--tight">
          It stops getting Möbius notifications. If the Möbius app is opened on it again with notifications allowed, it connects again.
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
            onClick={confirmDisconnect}
          >
            {removing ? 'Disconnecting…' : 'Disconnect'}
          </button>
        </div>
      </dialog>
    </section>
  )
}
