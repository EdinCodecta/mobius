/* While the agent works in a shared folder, show it and offer Stop (pauses every shared folder). */
import { createPortal } from 'react-dom'
import { FolderShared } from '@openai/apps-sdk-ui/components/Icon'
import useSharedFolders from '../../hooks/useSharedFolders.js'
import { pauseAllFolders } from '../../lib/sharedFolders/folderActions.js'

export default function SharedFolderActivity() {
  const { activity } = useSharedFolders()
  if (!activity || typeof document === 'undefined') return null
  return createPortal(
    <button
      type="button"
      className="screen-control-button screen-control-button--active shared-folder-activity"
      aria-label={`Agent is using ${activity.folderName}. Stop sharing folders.`}
      title="Pause every shared folder"
      onClick={() => { void pauseAllFolders() }}
    >
      <FolderShared width={18} height={18} aria-hidden="true" />
      <span className="shared-folder-activity__text">Agent is using {activity.folderName}</span>
      <span className="screen-control-button__live" aria-hidden="true" />
      <span className="screen-control-button__stop">Stop</span>
    </button>,
    document.body,
  )
}
