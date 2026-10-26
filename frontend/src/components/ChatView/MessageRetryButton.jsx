/* MessageRetryButton — "Try again" on the latest settled reply. It asks the
   agent for a fresh attempt as an ordinary owner message; the earlier reply
   and anything it already did stay in the transcript. One tap sends once. */
import { useState } from 'react'
import { Regenerate } from '@openai/apps-sdk-ui/components/Icon'

export default function MessageRetryButton({ onTryAgain }) {
  const [sent, setSent] = useState(false)

  function handleTryAgain(e) {
    // The containing reply owns reveal timing; this tap is not a reveal.
    e.stopPropagation()
    if (sent) return
    setSent(true)
    onTryAgain()
  }

  return (
    <button
      type="button"
      className="chat__msg-retry"
      onClick={handleTryAgain}
      disabled={sent}
      aria-label="Try again"
      title="Try again"
    >
      <Regenerate width={14} height={14} aria-hidden="true" />
    </button>
  )
}
