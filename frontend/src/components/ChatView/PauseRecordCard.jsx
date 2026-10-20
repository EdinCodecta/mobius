/* History of a pause that a later continuation resolved: the chat keeps a quiet
 * record of why work stopped (a platform update, low storage, a limit) next to
 * the "Resumed" notice, with no Resume control or "will continue" promise. */

import { Pause } from '@openai/apps-sdk-ui/components/Icon'
import MarkerCard from './MarkerCard.jsx'

const TITLES = {
  restart: 'Paused for a platform update',
  storage: 'Paused while storage was low',
  memory: 'Paused while memory was low',
  usage_limit: 'Paused at a usage limit',
  rate_limit: 'Paused at a rate limit',
  credits: 'Paused while out of credits',
  model_capacity: 'Paused while the model was busy',
  model_capacity_exhausted: 'Paused while the model was busy',
  goal_handoff: 'Goal paused',
}

export function pauseRecordTitle(block) {
  if (block?.code === 'goal_settlement_unfinished') return 'Work interrupted'
  return TITLES[block?.pause?.kind] || 'Paused'
}

export default function PauseRecordCard({ block }) {
  return (
    <MarkerCard
      title={pauseRecordTitle(block)}
      icon={<Pause width={14} height={14} aria-hidden="true" />}
    />
  )
}
