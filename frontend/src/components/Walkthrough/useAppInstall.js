/* Install for the guide's app cards: one tap installs the app. The shell refreshes its own app list
   after an install, so the card turns to Installed on its own. An app asks what it may do the first
   time it opens, not here.

   One app is handled at a time: other Install buttons wait while an install is running. */
import { useCallback, useRef, useState } from 'react'
import { installFromCatalog } from './walkthroughInstall.js'

export function useAppInstall(catalog) {
  const [status, setStatus] = useState({})
  const working = useRef(false)

  const setOne = useCallback((id, value) => setStatus(current => {
    const next = { ...current }
    if (value) next[id] = value
    else delete next[id]
    return next
  }), [])

  const install = useCallback(async id => {
    const item = catalog?.get(id)
    if (!item?.manifest_url || working.current) return
    working.current = true
    setOne(id, { state: 'installing' })
    try {
      const result = await installFromCatalog(item.manifest_url)
      setOne(id, { state: 'installed', warnings: result.warnings })
    } catch (error) {
      setOne(id, { state: 'error', error: error.message || 'The app could not be installed.' })
    } finally {
      working.current = false
    }
  }, [catalog, setOne])

  const statusOf = useCallback(id => status[id] || null, [status])
  const busy = Object.values(status).some(value => value.state === 'installing')
  return { statusOf, install, busy }
}
