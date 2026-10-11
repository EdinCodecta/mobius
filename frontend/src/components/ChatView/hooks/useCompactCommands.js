/* Manual compaction owns action latches, bounded status reads, chat-scoped
 * recovery, and which Stop the composer means while context is prepared. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, jsonOrThrow } from '../../../api/client.js'
import { getOnlineSnapshot } from '../../../lib/connectivityStore.js'
import { compactFailureInput } from '../slashCommands.js'
import { sendFailureMessage } from '../sendFailure.js'
import { isProviderSwitchBlocking } from '../providerSwitch.js'

const COMPACTION_PROGRESS_TIMEOUT_MS = 15000

export default function useCompactCommands({
  chatId, provisionalNewChat, hidden, serverCompactingKind,
  activationSettledRef, inputValueRef, setComposerInput, setSendFailure, fetchMessages, stopTurn,
}) {
  // A "/compact" submission is a chat action, not a turn: it rewrites the live
  // context instead of asking the model anything. Compact and Pause belong to
  // their chat, so a pending action in one chat never blocks another chat's.
  // A ``kind:chatId`` key is the synchronous same-chat duplicate latch, added
  // and removed only by the call that owns it; the state snapshot lets the
  // visible chat render its own outstanding compact POST.
  const pendingActionsRef = useRef(new Set())
  const [pendingActions, setPendingActions] = useState(() => new Set())
  const [compactProgressRecord, setCompactProgressRecord] = useState(null)
  const compactProgressRequestRef = useRef(0)
  const compactProgressControllerRef = useRef(null)
  const compactProgressActiveRef = useRef(false)
  const compactActionLifecycleRef = useRef(null)
  const activeCompactChatRef = useRef(chatId)
  activeCompactChatRef.current = chatId

  useEffect(() => {
    const lifecycle = { active: true }
    compactActionLifecycleRef.current = lifecycle
    return () => { lifecycle.active = false }
  }, [chatId])

  function claimAction(key) {
    if (pendingActionsRef.current.has(key)) return false
    pendingActionsRef.current.add(key)
    setPendingActions(new Set(pendingActionsRef.current))
    return true
  }

  function releaseAction(key) {
    pendingActionsRef.current.delete(key)
    setPendingActions(new Set(pendingActionsRef.current))
  }

  const cancelCompactProgress = useCallback(() => {
    ++compactProgressRequestRef.current
    compactProgressControllerRef.current?.abort()
    compactProgressControllerRef.current = null
  }, [])

  const refreshCompactProgress = useCallback(async (targetChatId = chatId) => {
    if (!targetChatId || provisionalNewChat || hidden || !compactProgressActiveRef.current
      || activeCompactChatRef.current !== targetChatId) return
    cancelCompactProgress()
    const controller = new AbortController()
    compactProgressControllerRef.current = controller
    const request = ++compactProgressRequestRef.current
    // Own the read deadline here: shared-browser transport forwards a caller
    // signal, but does not implement apiFetch's optional timeoutMs.
    const deadline = setTimeout(() => {
      const error = new Error('Compaction progress timed out')
      error.name = 'TimeoutError'
      controller.abort(error)
    }, COMPACTION_PROGRESS_TIMEOUT_MS)
    controller.signal.addEventListener('abort', () => clearTimeout(deadline), { once: true })
    try {
      const result = await jsonOrThrow(await api.chats.compactProgress(targetChatId, {
        signal: controller.signal,
      }), 'Compaction progress failed')
      if (!controller.signal.aborted && request === compactProgressRequestRef.current
        && activeCompactChatRef.current === targetChatId) {
        setCompactProgressRecord({ chatId: targetChatId, progress: result.progress || null })
      }
    } catch {
      // A failed status read must not erase the last known recovery handle.
    } finally {
      clearTimeout(deadline)
      if (compactProgressControllerRef.current === controller) {
        compactProgressControllerRef.current = null
      }
    }
  }, [chatId, provisionalNewChat, hidden, cancelCompactProgress])

  useEffect(() => {
    // Read on entry and on the existing compaction edge, including other tabs.
    // There is deliberately no polling and no automatic next POST.
    compactProgressActiveRef.current = !hidden && !provisionalNewChat
    void refreshCompactProgress(chatId)
    return () => {
      compactProgressActiveRef.current = false
      cancelCompactProgress()
    }
  }, [chatId, hidden, provisionalNewChat, serverCompactingKind, refreshCompactProgress, cancelCompactProgress])

  // "/compact" never becomes a message. It asks the backend to replace this
  // chat's live context with a fresh briefing and reset the provider session;
  // the visible transcript is untouched and the platform renders the stored
  // compaction as its own "Context compacted" card. A card action passes no
  // submitted input, so the owner's unrelated draft is left untouched.
  async function runCompactCommand(instructions = '', submittedInput = '/compact', recoveryId = null) {
    if (!activationSettledRef.current) return
    if (!chatId || provisionalNewChat) {
      setSendFailure('There’s no chat context to compact yet.')
      return
    }
    if (serverCompactingKind === 'compact') return
    if (isProviderSwitchBlocking(chatId)) return
    const actionKey = `compact:${chatId}`
    if (!claimAction(actionKey)) return
    if (submittedInput !== null) setComposerInput('')
    setSendFailure(null)
    const targetChatId = chatId
    const lifecycle = compactActionLifecycleRef.current
    const stillVisible = () => lifecycle?.active && activeCompactChatRef.current === targetChatId
    cancelCompactProgress()
    let completed = false
    try {
      const result = await jsonOrThrow(await api.chats.compact(targetChatId, {
        ...(instructions ? { instructions } : {}),
        batch_id: crypto.randomUUID(),
        ...(recoveryId ? { recovery_id: recoveryId } : {}),
      }), 'Compaction failed')
      // The action response is authoritative even if a server edge started
      // an optional GET during its POST. That older read cannot replace it.
      if (stillVisible()) {
        cancelCompactProgress()
        setCompactProgressRecord({ chatId: targetChatId, progress: result.progress || null })
      }
      if (result.ok) {
        completed = true
        if (stillVisible()) await fetchMessages({ force: true })
      }
    } catch (err) {
      if (stillVisible()) {
        if (!completed && submittedInput !== null) {
          setComposerInput(compactFailureInput(inputValueRef.current, submittedInput))
        }
        setSendFailure(sendFailureMessage(err, { online: getOnlineSnapshot() }))
      }
    } finally {
      // Status readback is optional: it must never own the action latch.
      releaseAction(actionKey)
      if (lifecycle?.active) void refreshCompactProgress(targetChatId)
    }
  }

  async function stopCompactCommand() {
    const actionKey = `pause:${chatId}`
    if (!chatId || !claimAction(actionKey)) return
    const targetChatId = chatId
    const lifecycle = compactActionLifecycleRef.current
    cancelCompactProgress()
    try {
      await jsonOrThrow(await api.chats.compactStop(targetChatId), 'Pausing compaction failed')
    } catch (err) {
      if (lifecycle?.active && activeCompactChatRef.current === targetChatId) {
        setSendFailure(sendFailureMessage(err, { online: getOnlineSnapshot() }))
      }
    } finally {
      releaseAction(actionKey)
      if (lifecycle?.active) void refreshCompactProgress(targetChatId)
    }
  }

  // The server's compacting window spans the batch runner that makes a pane
  // read ``running``, so a reopened pane knows preparation is the running work.
  const preparing = pendingActions.has(`compact:${chatId}`)
    || serverCompactingKind === 'compact'
  const compactionProgress = (compactProgressRecord?.chatId === chatId && compactProgressRecord.progress)
    || (preparing ? { state: 'running' } : null)

  function continueCompaction() {
    const recoveryId = compactionProgress?.recovery_id
    if (recoveryId) void runCompactCommand('', null, recoveryId)
  }

  // Turn Stop clears queued input and waits on the transition lock the batch
  // holds, so during preparation the composer's Stop is Pause: queued input,
  // the draft and the previous session stay intact and no next batch starts.
  function stopComposerWork() {
    return preparing ? stopCompactCommand() : stopTurn()
  }

  return {
    preparing, compactionProgress,
    runCompactCommand, continueCompaction, stopCompactCommand, stopComposerWork,
  }
}
