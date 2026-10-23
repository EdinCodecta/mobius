/* Session controller for one passive transcript block.

   Everything app-reported (state, acknowledgements) is bound to the exact
   document generation that negotiated support. A new document resets idle
   controls; releasing an idle offscreen frame discards its state entirely, so
   re-entry shows pending controls until the next document reports. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { blockActions } from '../markdown/appBlock.js'
import {
  sessionDocumentReset, sessionRetained, sessionState, sessionStateUpdate,
} from '../markdown/appBlockSession.js'

const UNNEGOTIATED = Object.freeze({ generation: null, supported: null })

export default function useAppBlockSession({ block, near }) {
  const [sessionId] = useState(() => crypto.randomUUID())
  const [state, setState] = useState(null)
  const [event, setEvent] = useState(null)
  const eventRef = useRef(null)
  const [negotiation, setNegotiation] = useState(UNNEGOTIATED)
  // Callbacks read the latest negotiation synchronously, before React commits.
  const negotiationRef = useRef(UNNEGOTIATED)
  const actions = useMemo(() => blockActions(block), [block])
  const keys = useMemo(() => new Set(actions.map(action => action.key)), [actions])
  const retained = sessionRetained(state, event)
  const frameWanted = near || retained
  // An unsupported document still keeps unresolved ownership mounted.
  const frameMounted = frameWanted && (negotiation.supported !== false || retained)

  const clearEvent = useCallback(() => {
    eventRef.current = null
    setEvent(null)
  }, [])

  // Idle sessions are released offscreen; nothing a supporting document
  // negotiated survives. An unsupported revision stays a static block.
  useEffect(() => {
    if (frameWanted || negotiationRef.current.supported === false) return
    negotiationRef.current = UNNEGOTIATED
    setNegotiation(UNNEGOTIATED)
    setState(null)
    clearEvent()
  }, [frameWanted, clearEvent])

  const onBlockCapability = useCallback((supported, { generation, reset }) => {
    if (reset) {
      // Capture before clearing: the updater may run after the ref changes.
      const previous = eventRef.current
      setState(current => sessionDocumentReset(current, previous))
      clearEvent()
    }
    const next = { generation, supported }
    negotiationRef.current = next
    setNegotiation(next)
  }, [clearEvent])

  const onBlockState = useCallback((message, { generation }) => {
    const current = negotiationRef.current
    if (current.supported !== true || generation !== current.generation) return
    const safe = sessionState(message, sessionId, keys)
    if (!safe) return
    setState(previous => sessionStateUpdate(previous, safe))
    if (!safe.checkpointInvalid && safe.ackNonce && safe.ackNonce === eventRef.current?.nonce) clearEvent()
  }, [sessionId, keys, clearEvent])

  // Reserve synchronously: a second click before React commits, or before
  // the app acknowledges this exact nonce, cannot send another event.
  const dispatch = useCallback((key, kind) => {
    const current = negotiationRef.current
    if (eventRef.current || current.supported !== true || !keys.has(key)) return false
    const message = { sessionId, generation: current.generation, key, event: kind, nonce: crypto.randomUUID() }
    eventRef.current = message
    setEvent(message)
    return true
  }, [sessionId, keys])

  // Scalar dependencies: the parser copies envelopes on every message even
  // when their observational data is unchanged.
  const checkpointId = state?.checkpoint?.id ?? null
  const checkpointData = state?.checkpoint?.data ?? null
  const recoveryError = state?.recoveryError ?? null
  const blockSession = useMemo(() => ({
    sessionId, actions, retain: retained, recoveryError,
    checkpoint: checkpointId ? { id: checkpointId, data: checkpointData } : null,
  }), [sessionId, actions, retained, recoveryError, checkpointId, checkpointData])

  return {
    sessionId, state, event, awaitingAck: event !== null, supported: negotiation.supported,
    generation: negotiation.generation, retained, frameMounted, blockSession,
    onBlockCapability, onBlockState, dispatch,
  }
}
