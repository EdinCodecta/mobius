/* Session controller for one passive transcript block.

   Everything app-reported (state, acknowledgements) is bound to the exact
   document generation that negotiated support. A new document resets idle
   controls; releasing an idle offscreen frame discards its state entirely, so
   re-entry shows pending controls until the next document reports.

   Confirm takes the action's place, so it is armed per *showing*: one
   continuous on-screen display of one exact confirmation (document
   generation, action key and app list). The showing ends when the list
   changes or disappears (cancel, a different action, a revert, a reload,
   release) or stops being visible; the next one must earn a fresh
   CONFIRM_ARM_MS. A second click of a double-click, a list swapped just
   before a click, or a list that changed offscreen never confirms unseen. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { blockActions } from '../markdown/appBlock.js'
import {
  sessionDocumentReset, sessionRetained, sessionState, sessionStateUpdate,
} from '../markdown/appBlockSession.js'

const UNNEGOTIATED = Object.freeze({ generation: null, supported: null })
export const CONFIRM_ARM_MS = 500

function confirmationIdentity(state) {
  const action = state?.actions.find(item => item.confirming && item.confirmation)
  return action ? JSON.stringify([action.key, action.confirmation]) : null
}

export default function useAppBlockSession({ block, near, confirmVisible = false }) {
  const [sessionId] = useState(() => crypto.randomUUID())
  const [state, setState] = useState(null)
  const [event, setEvent] = useState(null)
  const eventRef = useRef(null)
  const [negotiation, setNegotiation] = useState(UNNEGOTIATED)
  // Callbacks read the latest negotiation synchronously, before React commits.
  const negotiationRef = useRef(UNNEGOTIATED)
  const actions = useMemo(() => blockActions(block), [block])
  const keys = useMemo(() => new Set(actions.map(action => action.key)), [actions])
  // The current showing: a new number whenever what is shown changes,
  // including through "nothing shown", so no earlier arming is ever reused.
  const identity = confirmationIdentity(state)
  const [disarms, setDisarms] = useState(0)
  const showKey = identity && confirmVisible && negotiation.supported === true
    ? `${negotiation.generation}:${disarms}:${identity}` : null
  const showingRef = useRef({ key: null, number: 0, action: null })
  if (showingRef.current.key !== showKey) {
    showingRef.current = { key: showKey, number: showingRef.current.number + 1, action: showKey && JSON.parse(identity)[0] }
  }
  const showing = showingRef.current.number
  const [armedShowing, setArmedShowing] = useState(0)
  const armedShowingRef = useRef(0)
  useEffect(() => {
    if (!showKey) return undefined
    const timer = setTimeout(() => {
      armedShowingRef.current = showing
      setArmedShowing(showing)
    }, CONFIRM_ARM_MS)
    return () => clearTimeout(timer)
  }, [showKey, showing])
  const confirmArmed = showKey !== null && armedShowing === showing
  // Synchronous invalidation for reports, resets and events that land before
  // React re-renders: the next click cannot use the previous showing.
  const disarm = useCallback(() => {
    armedShowingRef.current = 0
    setDisarms(count => count + 1)
  }, [])
  const stateRef = useRef(state)
  stateRef.current = state
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
    disarm()
    negotiationRef.current = UNNEGOTIATED
    setNegotiation(UNNEGOTIATED)
    setState(null)
    clearEvent()
  }, [frameWanted, clearEvent, disarm])

  const onBlockCapability = useCallback((supported, { generation, reset }) => {
    if (generation !== negotiationRef.current.generation || reset) disarm()
    if (reset) {
      // Capture before clearing: the updater may run after the ref changes.
      const previous = eventRef.current
      setState(current => sessionDocumentReset(current, previous))
      clearEvent()
    }
    const next = { generation, supported }
    negotiationRef.current = next
    setNegotiation(next)
  }, [clearEvent, disarm])

  const onBlockState = useCallback((message, { generation }) => {
    const current = negotiationRef.current
    if (current.supported !== true || generation !== current.generation) return
    const safe = sessionState(message, sessionId, keys)
    if (!safe) return
    // A report that changes or removes the shown list ends the showing now.
    const held = safe.checkpointInvalid || safe.recoveryError || stateRef.current?.recoveryPending
    if ((held ? null : confirmationIdentity(safe)) !== confirmationIdentity(stateRef.current)) disarm()
    setState(previous => sessionStateUpdate(previous, safe))
    if (!safe.checkpointInvalid && safe.ackNonce && safe.ackNonce === eventRef.current?.nonce) clearEvent()
  }, [sessionId, keys, clearEvent, disarm])

  // Reserve synchronously: a second click before React commits, or before
  // the app acknowledges this exact nonce, cannot send another event.
  const dispatch = useCallback((key, kind) => {
    const current = negotiationRef.current
    if (eventRef.current || current.supported !== true || !keys.has(key)) return false
    // Confirm only the current showing, once it has been on screen long enough.
    const shown = showingRef.current
    if (kind === 'confirm' && (!shown.key || armedShowingRef.current !== shown.number || shown.action !== key)) return false
    // Any other owner event ends the showing (Not now, or a new activation).
    if (kind !== 'confirm') disarm()
    const message = { sessionId, generation: current.generation, key, event: kind, nonce: crypto.randomUUID() }
    eventRef.current = message
    setEvent(message)
    return true
  }, [sessionId, keys, disarm])

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
    sessionId, state, event, awaitingAck: event !== null, supported: negotiation.supported, confirmArmed,
    generation: negotiation.generation, retained, frameMounted, blockSession,
    onBlockCapability, onBlockState, dispatch,
  }
}
