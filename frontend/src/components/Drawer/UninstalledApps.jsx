import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import { expiryLabel, recoverErrorMessage, removedLabel } from './uninstalledApps.js'
import './UninstalledApps.css'

// Apps that were uninstalled but are still in their recovery window. Hidden
// when there are none, so it costs nothing until it is needed.
export default function UninstalledApps() {
  const queryClient = useQueryClient()
  const { data: rows = [] } = appQueries.uninstalled.useQuery()
  const [busyId, setBusyId] = useState(null)
  const [error, setError] = useState('')

  if (rows.length === 0) return null

  async function recover(row) {
    setBusyId(row.id)
    setError('')
    try {
      const response = await api.apps.recover(row.id)
      if (!response.ok) {
        setError(await recoverErrorMessage(response))
        return
      }
      await Promise.all([
        appQueries.list.invalidate(queryClient),
        appQueries.uninstalled.invalidate(queryClient),
      ])
    } catch {
      setError('Could not reach Möbius. Try again.')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className="uninstalled-apps" aria-labelledby="uninstalled-apps-title">
      <div className="uninstalled-apps__head">
        <h2 id="uninstalled-apps-title">Recently uninstalled</h2>
        <p>
          Kept for 7 days, then deleted for good. Recovering an app brings back its data
          and your earlier answer to its permission request.
        </p>
      </div>
      <ul className="uninstalled-apps__list">
        {rows.map(row => (
          <li className="uninstalled-apps__row" key={row.id}>
            <div className="uninstalled-apps__info">
              <strong>{row.name}</strong>
              <span>
                {removedLabel(row.deleted_at)} · {expiryLabel(row.expires_at)}
              </span>
            </div>
            <button
              type="button"
              className="uninstalled-apps__recover"
              disabled={busyId !== null}
              onClick={() => recover(row)}
              aria-label={`Recover ${row.name}`}
            >
              {busyId === row.id ? 'Recovering…' : 'Recover'}
            </button>
          </li>
        ))}
      </ul>
      {error && <p className="uninstalled-apps__error" role="alert">{error}</p>}
    </section>
  )
}
