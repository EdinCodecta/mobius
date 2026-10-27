/* The expandable part of Storage and Memory: clear, running now, and kept. */
import { useRef, useState } from 'react'
import CleanupRow from './CleanupRow.jsx'
import KeptStorageDialog from './KeptStorageDialog.jsx'
import RunningNow from './RunningNow.jsx'
import { GROUP_COPY, formatBytes, keptTotal } from './storageMemoryPresentation.js'

function GroupHead({ group, children }) {
  const copy = GROUP_COPY[group]
  return (
    <div className="storage-memory__group-head">
      <div className="storage-memory__group-text">
        <h3 className="storage-memory__group-title">{copy.title}</h3>
        <p className="storage-memory__group-helper">{copy.helper}</p>
      </div>
      {children}
    </div>
  )
}

function KeptSummary({ kept, onOpenApp, inertBoundaryRef }) {
  const [open, setOpen] = useState(false)
  const buttonRef = useRef(null)
  if (!kept.length) return null
  return (
    <section className="storage-memory__group" aria-label={GROUP_COPY.kept.title}>
      <GroupHead group="kept">
        <span className="storage-memory__size">{formatBytes(keptTotal(kept))}</span>
        <button ref={buttonRef} type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => setOpen(true)}>View list</button>
      </GroupHead>
      {open && (
        <KeptStorageDialog kept={kept} onClose={() => setOpen(false)} onOpenApp={onOpenApp}
          restoreFocusRef={buttonRef} inertBoundaryRef={inertBoundaryRef} />
      )}
    </section>
  )
}

export default function StorageMemoryDetails({ id, resources, onOpenApp, onOpenChat, inertBoundaryRef }) {
  const { overview } = resources
  return (
    <div id={id} className="storage-memory__details">
      <section className="storage-memory__group" data-resource-group="clear" tabIndex={-1} aria-label={GROUP_COPY.clear.title}>
        <GroupHead group="clear" />
        {overview.cleanable.length
          ? <ul className="storage-memory__items">{overview.cleanable.map(item => <CleanupRow key={item.target} item={item} resources={resources} />)}</ul>
          : <p className="storage-memory__note">Sizes appear after the first measurement.</p>}
      </section>
      <RunningNow memory={overview.memory} running={overview.running} onStop={resources.stopTurn} onOpenChat={onOpenChat} />
      <KeptSummary kept={overview.kept} onOpenApp={onOpenApp} inertBoundaryRef={inertBoundaryRef} />
    </div>
  )
}
