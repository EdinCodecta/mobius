/* Passive block session: a hidden, inert instance of an admitted app reports
   current state while the block is near the viewport; the host draws every
   control. See building-apps.md "Passive block sessions". */
import { Suspense, useEffect, useState } from 'react'
import { appFrameVersion } from '../../../lib/appVersion.js'
import useAppBlockSession from '../hooks/useAppBlockSession.js'
import { sessionSnapshot } from './appBlockSession.js'
import { AppBlockLayout, AppCanvas, StaticAppBlock } from './AppBlockParts.jsx'
import { usePassiveSlot } from './passiveSlots.js'

/** A callback ref, so the observer follows whichever section is mounted.
 *  `distance` (from the viewport's centre) orders blocks competing for slots. */
function useNearViewport() {
  const [node, setNode] = useState(null)
  const [view, setView] = useState({ near: false, distance: Infinity })
  useEffect(() => {
    if (!node || typeof IntersectionObserver === 'undefined') return undefined
    const observer = new IntersectionObserver(entries => {
      const entry = entries.at(-1)
      const rect = entry.boundingClientRect
      const centre = entry.rootBounds ? (entry.rootBounds.top + entry.rootBounds.bottom) / 2 : window.innerHeight / 2
      setView({ near: entry.isIntersecting, distance: Math.round(Math.abs((rect.top + rect.bottom) / 2 - centre)) })
    }, { rootMargin: '400px 0px' })
    observer.observe(node)
    return () => observer.disconnect()
  }, [node])
  return [setNode, view]
}

/** Whether an element is actually on screen in a visible document: no
 *  prefetch margin. Only this time counts toward arming Confirm. */
function useOnScreen() {
  const [node, setNode] = useState(null)
  const [intersecting, setIntersecting] = useState(false)
  const [documentVisible, setDocumentVisible] = useState(() =>
    typeof document === 'undefined' || document.visibilityState === 'visible')
  useEffect(() => {
    if (!node || typeof IntersectionObserver === 'undefined') { setIntersecting(false); return undefined }
    const observer = new IntersectionObserver(entries => setIntersecting(entries.at(-1).isIntersecting))
    observer.observe(node)
    return () => { observer.disconnect(); setIntersecting(false) }
  }, [node])
  useEffect(() => {
    const update = () => setDocumentVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [])
  return [setNode, intersecting && documentVisible]
}

/** Static unless an admitted, supporting session has a frame slot. A view
 *  the reader opened in the static form stays until the reader closes it. */
export function passiveBlockMode({ supported, near, granted, retained, staticOpen }) {
  if (retained) return 'passive'
  return supported === false || staticOpen || (near && !granted) ? 'static' : 'passive'
}

function SessionLinks({ links }) {
  if (!links.length) return null
  return <span className="md-app-block__links">{links.map(link =>
    <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a>)}</span>
}

/** Host-drawn controls for one app action. Before this document reports,
 *  saved actions are history, not controls. Confirm takes the action's own
 *  fixed-size place; "Not now" sits beside it. */
export function SessionControls({ target, state, locked, confirmArmed = false, confirmRef = null, onEvent }) {
  if (!state) return <span className="md-app-block__pending" role="status">Loading…</span>
  if (state.hidden) return null
  const control = state.confirming
    ? <span className="md-app-block__controls">
      <button type="button" className="md-app-block__cancel" disabled={locked}
        onClick={() => onEvent(target.intent, 'cancel')}>Not now</button>
      <button ref={confirmRef} type="button" className="md-app-block__action"
        disabled={locked || state.disabled || !state.confirmation || !confirmArmed}
        onClick={() => onEvent(target.intent, 'confirm')}>Confirm</button>
    </span>
    : <button type="button" className="md-app-block__action" disabled={locked || state.disabled}
      aria-busy={state.busy || undefined} title={state.label || target.label}
      onClick={() => onEvent(target.intent, 'activate')}>{state.label || target.label}</button>
  return <div className="md-app-block__session">
    <div className="md-app-block__session-row">
      {state.status ? <span className={`md-app-block__status is-${state.statusTone}`}>{state.status}</span> : null}
      <SessionLinks links={state.links} />
      {control}
    </div>
    {state.note && !state.confirming ? <p className={`md-app-block__note is-${state.tone}`} role="status">{state.note}</p> : null}
  </div>
}

/** What Confirm will do, exactly as the app supplied it. The provenance note
 *  comes first: it describes the saved snapshot, not the app's list. */
export function SessionConfirmation({ action, appName, listRef = null }) {
  if (!action) return null
  return <div ref={listRef} className="md-app-block__confirmation" role="group" aria-label={`Confirm with ${appName}`}>
    <p className="md-app-block__provenance" role="note">
      {`The saved title and details above come from the conversation. The list below comes from ${appName}.`}</p>
    {action.confirmation
      ? <><strong>{appName} will act on:</strong><ul>{action.confirmation.map((item, i) => <li key={i}>
        <span>{item.title}</span>
        <dl>{item.facts.map((fact, j) => <div key={j}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}</dl>
      </li>)}</ul></>
      : <p role="status">{action.note}</p>}
  </div>
}

/** The passive block for one parsed session state. The parser allows at most
 *  one enabled confirmation, and this is the action whose list is shown. */
export function PassiveBlockView({ block, app, state, locked, confirmArmed = false, onEvent, onInternalNav,
  sectionRef = null, confirmRef = null, listRef = null, children = null }) {
  const actionState = key => state?.actions.find(action => action.key === key) || null
  const actionFor = target => target
    ? <SessionControls target={target} state={actionState(target.intent)} locked={locked}
      confirmArmed={confirmArmed} confirmRef={confirmRef} onEvent={onEvent} />
    : null
  return <AppBlockLayout block={sessionSnapshot(block, state)} app={app} onInternalNav={onInternalNav}
    actionFor={actionFor} summary={state?.summary || ''} sectionRef={sectionRef}>
    <SessionConfirmation action={state?.actions.find(action => action.confirming)} appName={app.name} listRef={listRef} />
    {state?.notice ? <p className="md-app-block__notice" role="status">{state.notice}</p> : null}
    {children}
  </AppBlockLayout>
}

/** No host-request handler: AppCanvas refuses every request from this hidden canvas. */
function SessionHost({ app, session }) {
  return <div className="md-app-block__session-host" aria-hidden="true" inert>
    <Suspense fallback={null}>
      <AppCanvas appId={app.id} appName={app.name} appSlug={app.slug} version={appFrameVersion(app)}
        storageGeneration={app.storage_generation} offlineCapable={app.offline_capable === true}
        capabilityContract={app.capability_contract || null} active={false} visible={false} interactive={false}
        navigationTarget={false} blockSession={session.blockSession} blockEvent={session.event}
        onBlockState={session.onBlockState} onBlockCapability={session.onBlockCapability} />
    </Suspense>
  </div>
}

export default function PassiveAppBlock({ block, app, onInternalNav }) {
  const [sectionRef, { near, distance }] = useNearViewport()
  const [confirmRef, confirmOnScreen] = useOnScreen()
  const [listRef, listOnScreen] = useOnScreen()
  const [retained, setRetained] = useState(false)
  const [staticOpen, setStaticOpen] = useState(false)
  const granted = usePassiveSlot(app.id, { wanted: near && !staticOpen, retained, distance })
  const session = useAppBlockSession({ block, near: near && granted && !staticOpen,
    confirmVisible: confirmOnScreen && listOnScreen })
  useEffect(() => setRetained(session.retained), [session.retained])
  if (passiveBlockMode({ supported: session.supported, near, granted, retained: session.retained, staticOpen }) === 'static') {
    return <StaticAppBlock block={block} app={app} appsLoading={false} onInternalNav={onInternalNav}
      sectionRef={sectionRef} onOpenChange={setStaticOpen} />
  }
  const { state } = session
  const locked = session.awaitingAck || Boolean(state?.actions.some(action => action.busy))
  return <PassiveBlockView block={block} app={app} state={state} locked={locked} confirmArmed={session.confirmArmed}
    onEvent={session.dispatch} onInternalNav={onInternalNav} sectionRef={sectionRef}
    confirmRef={confirmRef} listRef={listRef}>
    {session.frameMounted ? <SessionHost app={app} session={session} /> : null}
  </PassiveBlockView>
}
