/* Loads the Storage and Memory overview and runs its owner actions. */
import { useCallback, useEffect, useState } from 'react'
import { api, jsonOrThrow } from '../../../api/client.js'
import { removedQueuedSentence } from './storageMemoryPresentation.js'

export default function useStorageMemory({ active }) {
  const [overview, setOverview] = useState(null)
  const [error, setError] = useState('')
  const [measuring, setMeasuring] = useState(false)
  const [checkedAt, setCheckedAt] = useState(null)

  const load = useCallback(async ({ refresh = false } = {}) => {
    if (refresh) setMeasuring(true)
    try {
      setOverview(await api.resources.overview({ refresh }))
      setCheckedAt(new Date().toISOString())
      setError('')
    } catch (err) {
      setError(err?.message || 'Storage and memory are unavailable right now.')
    } finally {
      if (refresh) setMeasuring(false)
    }
  }, [])

  useEffect(() => {
    if (active) void load()
  }, [active, load])

  const preview = useCallback(target => api.resources.previewCleanup(target), [])

  const clear = useCallback(async (target) => {
    const result = await api.resources.runCleanup(target)
    setOverview(result.resources)
    setCheckedAt(new Date().toISOString())
    setError('')
    return result
  }, [])

  // A Stop that timed out answers 200 with stopped: false; that is a failure
  // the owner can retry, not a success.
  const stopTurn = useCallback(async (chatId) => {
    const result = await jsonOrThrow(await api.chats.stop(chatId), 'Could not stop this turn')
    const clearedQueued = result?.cleared_pending_cids?.length || 0
    if (result?.stopped !== true) {
      throw new Error(`The turn did not stop in time. Try again.${clearedQueued ? ` ${removedQueuedSentence(clearedQueued)}` : ''}`)
    }
    await load()
    return { clearedQueued }
  }, [load])

  return {
    overview,
    error,
    measuring,
    checkedAt,
    measure: () => load({ refresh: true }),
    preview,
    clear,
    stopTurn,
  }
}
