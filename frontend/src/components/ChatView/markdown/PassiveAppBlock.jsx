/* Passive block session: a hidden, inert instance of an admitted app reports
   current state while the block is near the viewport; the host draws every
   control. See building-apps.md "Passive block sessions". */
import { Suspense, useCallback, useEffect, useState } from 'react'
import { appFrameVersion } from '../../../lib/appVersion.js'
import useAppBlockSession from '../hooks/useAppBlockSession.js'
import { transcriptHostRequest } from './appBlock.js'
import { sessionSnapshot } from './appBlockSession.js'
import { AppBlockLayout, AppCanvas, StaticAppBlock, useTranscriptNavigation } from './AppBlockParts.jsx'

/** A callback ref, so the observer follows whichever section is mounted. */
function useNearViewport() {
  const [node, setNode] = useState(null)
  const [near, setNear] = useState(false)
  useEffect(() => {
    if (!node || typeof IntersectionObserver === 'undefined') return undefined
    const observer = new IntersectionObserver(entries => {
      setNear(entries.some(entry => entry.isIntersecting))
    }, { rootMargin: '400px 0px' })
    observer.observe(node)
    return () => observer.disconnect()
  }, [node])
  return [setNode, near]
}

function SessionLinks({ links }) {
  if (!links.length) return null
  return <span className="md-app-block__links">{links.map(link =>
    <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a>)}</span>
}

/** Host-drawn controls for one app action. Before this document reports,
 *  saved actions are history, not controls. Confirm takes the action's own
 *  fixed-size place; "Not now" sits beside it. */
export function SessionControls({ target, state, locked, onEvent }) {
  if (!state) return <span className="md-app-block__pending" role="status">Loading…</span>
  if (state.hidden) return null
  const control = state.confirming
    ? <span className="md-app-block__controls">
      <button type="button" className="md-app-block__cancel" disabled={locked}
        onClick={() => onEvent(target.intent, 'cancel')}>Not now</button>
      <button type="button" className="md-app-block__action" disabled={locked || state.disabled || !state.confirmation}
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
export function SessionConfirmation({ action, appName }) {
  if (!action) return null
  return <div className="md-app-block__confirmation" role="group" aria-label={`Confirm with ${appName}`}>
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
export function PassiveBlockView({ block, app, state, locked, onEvent, onInternalNav, sectionRef = null, children = null }) {
  const actionState = key => state?.actions.find(action => action.key === key) || null
  const actionFor = target => target
    ? <SessionControls target={target} state={actionState(target.intent)} locked={locked} onEvent={onEvent} />
    : null
  return <AppBlockLayout block={sessionSnapshot(block, state)} app={app} onInternalNav={onInternalNav}
    actionFor={actionFor} summary={state?.summary || ''} sectionRef={sectionRef}>
    <SessionConfirmation action={state?.actions.find(action => action.confirming)} appName={app.name} />
    {state?.notice ? <p className="md-app-block__notice" role="status">{state.notice}</p> : null}
    {children}
  </AppBlockLayout>
}

function SessionHost({ app, session, onHostRequest }) {
  return <div className="md-app-block__session-host" aria-hidden="true" inert>
    <Suspense fallback={null}>
      <AppCanvas appId={app.id} appName={app.name} appSlug={app.slug} version={appFrameVersion(app)}
        storageGeneration={app.storage_generation} offlineCapable={app.offline_capable === true}
        capabilityContract={app.capability_contract || null} active={false} visible={false} interactive={false}
        navigationTarget={false} blockSession={session.blockSession} blockEvent={session.event}
        onBlockState={session.onBlockState} onBlockCapability={session.onBlockCapability}
        onHostRequest={onHostRequest} />
    </Suspense>
  </div>
}

export default function PassiveAppBlock({ block, app, onInternalNav }) {
  const [sectionRef, near] = useNearViewport()
  const session = useAppBlockSession({ block, near })
  const navigate = useTranscriptNavigation(onInternalNav)
  const onHostRequest = useCallback((_, request) => transcriptHostRequest(request, { visible: false, navigate }), [navigate])
  // An app revision without session support is an ordinary static block.
  if (session.supported === false && !session.retained) {
    return <StaticAppBlock block={block} app={app} appsLoading={false} onInternalNav={onInternalNav} sectionRef={sectionRef} />
  }
  const { state } = session
  const locked = session.awaitingAck || Boolean(state?.actions.some(action => action.busy))
  return <PassiveBlockView block={block} app={app} state={state} locked={locked} onEvent={session.dispatch}
    onInternalNav={onInternalNav} sectionRef={sectionRef}>
    {session.frameMounted ? <SessionHost app={app} session={session} onHostRequest={onHostRequest} /> : null}
  </PassiveBlockView>
}
