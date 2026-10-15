import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api/client.js'
import { isNativePushAvailable, isThisPhone } from '../../lib/nativePush.js'

function lastOpened(iso) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/**
 * Phones registered for native push (the Möbius mobile app). Shown when any
 * phone is registered, or inside the app. A removed phone registers again the
 * next time Möbius opens on it; turning notifications off for the app in the
 * phone's settings stops them for good.
 */
export default function PhoneNotifications({ active = true }) {
  const [devices, setDevices] = useState([])
  const [removing, setRemoving] = useState(null)

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

  const remove = async (id) => {
    setRemoving(id)
    try {
      await api.push.devices.remove(id)
    } catch {
      // The refreshed list shows whether it is still there.
    }
    setRemoving(null)
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
      ) : devices.map((device) => (
        <div className="settings__row" key={device.id}>
          <div>
            <div>{isThisPhone(device.tokenTail) ? 'This phone' : `Phone ···${device.tokenTail}`}</div>
            <p className="settings__subtext settings__subtext--tight">
              Last opened {lastOpened(device.registeredAt)}
            </p>
          </div>
          <button
            className="settings__btn settings__btn--outline settings__btn--sm"
            type="button"
            disabled={removing === device.id}
            onClick={() => remove(device.id)}
          >
            {removing === device.id ? 'Removing…' : 'Remove'}
          </button>
        </div>
      ))}
    </section>
  )
}
