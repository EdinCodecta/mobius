import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import useExpiryCountdowns from '../../hooks/useExpiryCountdowns.js'
import AppIcon from '../AppIcon.jsx'
import { recoverErrorMessage, removedLabel } from './uninstalledApps.js'
import './UninstalledApps.css'

// Apps that were uninstalled but are still in their recovery window. Hidden
// when there are none, so it costs nothing until it is needed.
export default function UninstalledApps() {
  const queryClient = useQueryClient()
  const { data: rows = [] } = appQueries.uninstalled.useQuery()
  const [busyId, setBusyId] = useState(null)
  const [error, setError] = useState('')
  const live = useExpiryCountdowns(rows)

  if (live.length === 0) return null

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
        <h2 id="uninstalled-apps-title">
          Recently uninstalled <span className="uninstalled-apps__count">{live.length}</span>
        </h2>
        <p>
          You can recover an uninstalled app, with its data, for 7 days. After that it is
          removed permanently.
        </p>
      </div>
      <ul className="uninstalled-apps__list">
        {live.map(({ row, countdown }) => {
          const { tone } = countdown
          return (
            <li className="uninstalled-apps__row" key={row.id}>
              <AppIcon
                item={{
                  id: row.id,
                  slug: row.slug,
                  icon_url: row.icon_data_url,
                  background_color: row.background_color,
                  theme_color: row.theme_color,
                }}
                label={row.name}
                size={null}
                className="uninstalled-apps__icon"
              />
              <div className="uninstalled-apps__info">
                <strong>{row.name}</strong>
                <span>{removedLabel(row.deleted_at)}</span>
              </div>
              <span className={`uninstalled-apps__expiry${tone ? ` is-${tone}` : ''}`}>
                {countdown.label}
              </span>
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
          )
        })}
      </ul>
      {error && <p className="uninstalled-apps__error" role="alert">{error}</p>}
    </section>
  )
}
