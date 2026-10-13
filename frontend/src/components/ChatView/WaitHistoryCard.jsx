/* WaitHistoryCard preserves settled monitor and dependency outcomes beside chat history. */

import { ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import GoalPlanDetails from './GoalPlanDetails.jsx'
import LifecycleIcon, { LifecycleOutcome } from './LifecycleIcon.jsx'
import { waitHistoryViewModel } from './waitHistory.js'

export default function WaitHistoryCard({ summary }) {
  const view = waitHistoryViewModel(summary)
  if (!view) return null

  if (summary.target_kind) return (
    <aside className="chat__goal-history chat__wait-history" aria-label={view.ariaLabel}>
      <details className="chat__goal-history-details">
        <summary className="chat__goal-history-summary">
          <LifecycleIcon kind="wait" />
          <span className="chat__goal-history-copy">
            <span className="chat__goal-history-kicker">{view.kicker}</span>
            <strong className="chat__goal-history-objective">{view.condition}</strong>
            {view.metadata && <span className="chat__goal-history-meta">{view.metadata}</span>}
          </span>
          <ChevronRight className="chat__panel-chevron" width={14} height={14} aria-hidden="true" />
        </summary>
        <GoalPlanDetails dependencyHistory={[summary]} />
      </details>
    </aside>
  )
  return (
    <aside
      className={`chat__goal-history chat__wait-history chat__wait-history--${view.tone}`}
      aria-label={view.ariaLabel}
    >
      <LifecycleIcon kind="wait" />
      <div className="chat__goal-history-copy">
        <span className="chat__goal-history-kicker chat__wait-history-kicker">
          <LifecycleOutcome tone={view.tone} />{view.kicker}
        </span>
        <strong className="chat__goal-history-objective">{view.condition}</strong>
        {view.metadata && (
          <span className="chat__goal-history-meta">{view.metadata}</span>
        )}
      </div>
    </aside>
  )
}
