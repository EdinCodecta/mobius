/* A `mobius-app` block in an assistant reply. See building-apps.md. */
import { appQueries } from '../../../hooks/queries.js'
import { StaticAppBlock, installedApp } from './AppBlockParts.jsx'
import { passiveAppBlockAllowed } from './appBlockSession.js'
import PassiveAppBlock from './PassiveAppBlock.jsx'

export default function AppBlock({ block, onInternalNav }) {
  const apps = appQueries.list.useQuery()
  const app = installedApp(apps.data, block.app)
  // A passive session needs the block's request and the app's admission.
  if (block.session && passiveAppBlockAllowed(app)) {
    return <PassiveAppBlock block={block} app={app} onInternalNav={onInternalNav} />
  }
  return <StaticAppBlock block={block} app={app} appsLoading={apps.isLoading} onInternalNav={onInternalNav} />
}
