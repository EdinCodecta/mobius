/* A `mobius-app` block in an assistant reply. See building-apps.md. */
import { useState } from 'react'
import { appQueries } from '../../../hooks/queries.js'
import { StaticAppBlock, installedApp } from './AppBlockParts.jsx'
import { passiveAppBlockAllowed } from './appBlockSession.js'
import PassiveAppBlock from './PassiveAppBlock.jsx'

/** Which block to render. A passive session needs the block's request and the
 *  app's live admission. If admission disappears (an update drops the
 *  capability or its applied digest) while the session still owns an
 *  unresolved outcome, the session stays mounted but `held`: it keeps its
 *  lock and checkpoint and offers no new action until the outcome settles. */
export function appBlockRoute({ requested, installed, admitted, unresolved }) {
  if (!requested || !installed) return 'static'
  if (admitted) return 'passive'
  return unresolved ? 'held' : 'static'
}

export default function AppBlock({ block, onInternalNav }) {
  const apps = appQueries.list.useQuery()
  const app = installedApp(apps.data, block.app)
  // Reported by a mounted passive session: it owns an unresolved outcome.
  const [unresolved, setUnresolved] = useState(false)
  const route = appBlockRoute({ requested: block.session, installed: Boolean(app),
    admitted: passiveAppBlockAllowed(app), unresolved })
  if (route !== 'static') {
    return <PassiveAppBlock block={block} app={app} onInternalNav={onInternalNav}
      admitted={route === 'passive'} onRetainedChange={setUnresolved} />
  }
  return <StaticAppBlock block={block} app={app} appsLoading={apps.isLoading} onInternalNav={onInternalNav} />
}
