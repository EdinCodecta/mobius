/* A `mobius-app` block in an assistant reply. See building-apps.md. */
import { appQueries } from '../../../hooks/queries.js'
import { StaticAppBlock, installedApp } from './AppBlockParts.jsx'

export default function AppBlock({ block, onInternalNav }) {
  const apps = appQueries.list.useQuery()
  const app = installedApp(apps.data, block.app)
  return <StaticAppBlock block={block} app={app} appsLoading={apps.isLoading} onInternalNav={onInternalNav} />
}
