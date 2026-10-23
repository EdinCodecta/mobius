/* The passive block session controller binds every app report to one
   document generation and lets exactly one owner event be in flight. */
import assert from 'node:assert/strict'
import test, { mock } from 'node:test'
import { renderHook } from './react-hook-shim.mjs'
import useAppBlockSession, { CONFIRM_ARM_MS } from '../useAppBlockSession.js'
import { appBlockFromToken } from '../../markdown/appBlock.js'
import { createBlockDocuments, forwardBlockState } from '../../../AppCanvas/appBlockSessionHost.js'

const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({
  app: 'example', intent: 'open:a', title: 'Fix', interaction: 'inline',
  action: { label: 'Send all', intent: 'send-batch:sel-1' },
  items: [{ title: 'Row', intent: 'open:b', action: { label: 'Send', intent: 'send:b' } }] }) })

function mounted(near = true) {
  const h = renderHook(props => useAppBlockSession(props), { block, near, confirmVisible: true })
  const report = (fields, generation = h.result.current.generation) => h.result.current.onBlockState({
    type: 'moebius:app-block-state', sessionId: h.result.current.sessionId,
    actions: [{ key: 'send-batch:sel-1', label: 'Send all' }, { key: 'send:b', label: 'Send' }], ...fields,
  }, { generation })
  return { h, report, capability: (supported, generation, reset = true) => h.result.current.onBlockCapability(supported, { generation, reset }) }
}

test('the session envelope names every block action and no state is shown before a report', () => {
  const { h, capability, report } = mounted()
  assert.deepEqual(h.result.current.blockSession.actions.map(action => action.key), ['send-batch:sel-1', 'send:b'])
  assert.equal(h.result.current.frameMounted, true)
  assert.equal(h.result.current.state, null)
  assert.equal(h.result.current.dispatch('send:b', 'activate'), false, 'no event before the document negotiates')
  capability(true, 1)
  report({})
  assert.equal(h.result.current.state.actions.length, 2)
})

test('a double click before acknowledgement sends one event; only its exact ack releases the lock', () => {
  const { h, capability, report } = mounted()
  capability(true, 1)
  report({})
  assert.equal(h.result.current.dispatch('send-batch:sel-1', 'activate'), true)
  const first = h.result.current.event
  assert.equal(first.generation, 1)
  assert.equal(h.result.current.dispatch('send-batch:sel-1', 'activate'), false)
  assert.equal(h.result.current.dispatch('send:b', 'activate'), false)
  assert.equal(h.result.current.event, first)
  report({ ackNonce: 'not-the-reserved-nonce' })
  assert.equal(h.result.current.awaitingAck, true)
  report({ ackNonce: first.nonce, checkpoint: { id: '', data: 'malformed' } })
  assert.equal(h.result.current.awaitingAck, true, 'an invalid report cannot acknowledge')
  report({ ackNonce: first.nonce })
  assert.equal(h.result.current.awaitingAck, false)
  assert.equal(h.result.current.dispatch('send-batch:sel-1', 'cancel'), true)
  assert.notEqual(h.result.current.event.nonce, first.nonce)
  assert.equal(h.result.current.dispatch('unknown:key', 'activate'), false)
})

test('state and acknowledgement from another document generation are ignored', () => {
  const { h, capability, report } = mounted()
  capability(true, 1)
  report({})
  h.result.current.dispatch('send:b', 'activate')
  const sent = h.result.current.event
  capability(null, 2)
  // A document reload discards idle controls and the unacknowledged activate.
  assert.equal(h.result.current.state, null)
  assert.equal(h.result.current.event, null)
  report({ ackNonce: sent.nonce, summary: 'stale' }, 1)
  assert.equal(h.result.current.state, null)
  capability(true, 2, false)
  report({ summary: 'stale document' }, 1)
  assert.equal(h.result.current.state, null)
  report({ summary: 'current' }, 2)
  assert.equal(h.result.current.state.summary, 'current')
})

test('a reload during an unacknowledged Confirm holds ownership instead of offering controls', () => {
  const { h, capability, report } = mounted()
  capability(true, 1)
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    report({ actions: [{ key: 'send:b', confirming: true, confirmation: [{ title: 'Exact', facts: [] }] }] })
    mock.timers.tick(CONFIRM_ARM_MS)
  } finally { mock.timers.reset() }
  assert.equal(h.result.current.dispatch('send:b', 'confirm'), true)
  capability(null, 2)
  const held = h.result.current.state
  assert.equal(held.recoveryPending, true)
  assert.ok(held.actions.every(action => action.disabled && !action.confirming))
  assert.equal(h.result.current.retained, true)
})

test('releasing an idle frame offscreen discards its negotiation; re-entry waits for fresh state', () => {
  const { h, capability, report } = mounted()
  capability(true, 1)
  report({})
  assert.ok(h.result.current.state)
  h.rerender({ block, near: false })
  assert.equal(h.result.current.frameMounted, false)
  assert.equal(h.result.current.state, null)
  assert.equal(h.result.current.supported, null)
  h.rerender({ block, near: true })
  assert.equal(h.result.current.frameMounted, true)
  assert.equal(h.result.current.dispatch('send:b', 'activate'), false, 'stale controls cannot act during renegotiation')
  report({}, 1)
  assert.equal(h.result.current.state, null, 'the released document cannot report again')
})

test('an unresolved owner stays mounted offscreen', () => {
  const { h, capability, report } = mounted()
  capability(true, 1)
  report({ retain: true, checkpoint: { id: 'c1', data: 'x' } })
  h.rerender({ block, near: false })
  assert.equal(h.result.current.frameMounted, true)
  assert.deepEqual(h.result.current.blockSession.checkpoint, { id: 'c1', data: 'x' })
  assert.equal(h.result.current.blockSession.retain, true)
})

test('an unsupported revision unmounts the hidden frame and stays static', () => {
  const { h, capability } = mounted()
  capability(false, 1)
  assert.equal(h.result.current.supported, false)
  assert.equal(h.result.current.frameMounted, false)
  h.rerender({ block, near: false })
  h.rerender({ block, near: true })
  assert.equal(h.result.current.supported, false, 'scrolling does not remount an unsupported session')
  assert.equal(h.result.current.frameMounted, false)
})

test('through the AppCanvas route, an old document cannot resurrect Confirm after a same-iframe reload', () => {
  const { h, capability } = mounted()
  const documents = createBlockDocuments()
  const deliver = message => forwardBlockState({ message: { type: 'moebius:app-block-state',
    sessionId: h.result.current.sessionId, ...message }, session: h.result.current.blockSession,
  document: documents.get('v1'), isLive: true, onState: h.result.current.onBlockState })
  const confirming = { actions: [{ key: 'send:b', confirming: true, confirmation: [{ title: 'Old target', facts: [] }] }] }
  const old = documents.load('v1')
  documents.mounted('v1', { supportsAppBlocks: true, blockGeneration: old.generation })
  capability(true, old.generation)
  assert.equal(deliver({ generation: old.generation, ...confirming }), true)
  h.result.current.dispatch('send:b', 'cancel')
  const cancel = h.result.current.event
  deliver({ generation: old.generation, actions: [{ key: 'send:b', label: 'Send' }], ackNonce: cancel.nonce })
  assert.equal(h.result.current.state.actions[0].confirming, false)

  // Reload: same window, new document. Its predecessor's queued reports follow.
  const next = documents.load('v1')
  capability(null, next.generation)
  assert.equal(documents.mounted('v1', { supportsAppBlocks: true, blockGeneration: old.generation }), null,
    'the old mount report does not negotiate for the new document')
  documents.mounted('v1', { supportsAppBlocks: true, blockGeneration: next.generation })
  capability(true, next.generation, false)
  for (const generation of [old.generation, undefined, next.generation + 1]) {
    assert.equal(deliver({ generation, ...confirming, ackNonce: cancel.nonce }), false)
  }
  assert.equal(h.result.current.state, null, 'no old Confirm and no old acknowledgement')
  assert.equal(deliver({ generation: next.generation, actions: [{ key: 'send:b', label: 'Send' }] }), true)
  assert.equal(h.result.current.state.actions[0].confirming, false)
})

const confirmList = title => ({ actions: [{ key: 'send-batch:sel-1', confirming: true, confirmation: [{ title, facts: [] }] }] })

test('two quick clicks on the action deliver no confirm: Confirm arms only after its list was shown', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const { h, capability, report } = mounted()
    capability(true, 1)
    report({})
    // First click: activate. The app acknowledges and asks for confirmation at once.
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'activate'), true)
    report({ ackNonce: h.result.current.event.nonce, ...confirmList('Target') })
    // Second click lands where Confirm now is, tens of milliseconds later.
    mock.timers.tick(40)
    assert.equal(h.result.current.confirmArmed, false)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false)
    assert.equal(h.result.current.event, null, 'no confirm reached the app')
    mock.timers.tick(CONFIRM_ARM_MS)
    assert.equal(h.result.current.confirmArmed, true)
    assert.equal(h.result.current.dispatch('send:b', 'confirm'), false, 'only the shown action can confirm')
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), true)
  } finally { mock.timers.reset() }
})

test('replacing the confirmation contents re-arms Confirm', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const { h, capability, report } = mounted()
    capability(true, 1)
    report(confirmList('First'))
    mock.timers.tick(CONFIRM_ARM_MS)
    assert.equal(h.result.current.confirmArmed, true)
    report(confirmList('Swapped'))
    assert.equal(h.result.current.confirmArmed, false)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false)
    mock.timers.tick(CONFIRM_ARM_MS - 1)
    assert.equal(h.result.current.confirmArmed, false)
    // The same list reported again does not restart the delay.
    report(confirmList('Swapped'))
    mock.timers.tick(1)
    assert.equal(h.result.current.confirmArmed, true)
    assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), true)
  } finally { mock.timers.reset() }
})

// Arming belongs to one continuous visible showing. Each sequence first arms
// list A, then shows it again some other way: a fresh 500 ms is required.
for (const mode of ['repeated activation', 'A-B-A', 'reload', 'changed offscreen', 'document hidden']) {
  test(`a previously armed list must earn a fresh visible delay: ${mode}`, () => {
    mock.timers.enable({ apis: ['setTimeout'] })
    try {
      const { h, capability, report } = mounted()
      capability(true, 1)
      report(confirmList('A'))
      mock.timers.tick(CONFIRM_ARM_MS)
      assert.equal(h.result.current.confirmArmed, true)
      if (mode === 'repeated activation') {
        // Not now, then a double-click on the action that returns the same list.
        assert.equal(h.result.current.dispatch('send-batch:sel-1', 'cancel'), true)
        report({ ackNonce: h.result.current.event.nonce })
        assert.equal(h.result.current.dispatch('send-batch:sel-1', 'activate'), true)
        report({ ...confirmList('A'), ackNonce: h.result.current.event.nonce })
        mock.timers.tick(40)
      } else if (mode === 'A-B-A') {
        report(confirmList('B'))
        mock.timers.tick(40)
        report(confirmList('A'))
      } else if (mode === 'reload') {
        capability(null, 2)
        capability(true, 2, false)
        report(confirmList('A'), 2)
      } else if (mode === 'changed offscreen') {
        report({ ...confirmList('A'), retain: true })
        h.rerender({ block, near: false, confirmVisible: false })
        report({ ...confirmList('Changed while unseen'), retain: true })
        mock.timers.tick(CONFIRM_ARM_MS)
        h.rerender({ block, near: true, confirmVisible: true })
      } else {
        h.rerender({ block, near: true, confirmVisible: false })
        mock.timers.tick(CONFIRM_ARM_MS)
        h.rerender({ block, near: true, confirmVisible: true })
      }
      assert.equal(h.result.current.confirmArmed, false)
      assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false)
      assert.equal(h.result.current.event?.event === 'confirm', false, 'no confirm reached the app')
      mock.timers.tick(CONFIRM_ARM_MS)
      assert.equal(h.result.current.confirmArmed, true, 'the current showing arms after its own delay')
      assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), true)
    } finally { mock.timers.reset() }
  })
}

test('a report that swaps the list disarms before the next render', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const { h, capability, report } = mounted()
    capability(true, 1)
    report(confirmList('A'))
    mock.timers.tick(CONFIRM_ARM_MS)
    const { dispatch, onBlockState, sessionId } = h.result.current
    // Call the callbacks captured before the swap re-renders.
    onBlockState({ type: 'moebius:app-block-state', sessionId, ...confirmList('B') }, { generation: 1 })
    assert.equal(dispatch('send-batch:sel-1', 'confirm'), false)
  } finally { mock.timers.reset() }
})
