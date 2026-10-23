/* The passive block session controller binds every app report to one
   document generation and lets exactly one owner event be in flight. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { renderHook } from './react-hook-shim.mjs'
import useAppBlockSession from '../useAppBlockSession.js'
import { appBlockFromToken } from '../../markdown/appBlock.js'

const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({
  app: 'example', intent: 'open:a', title: 'Fix', interaction: 'inline',
  action: { label: 'Send all', intent: 'send-batch:sel-1' },
  items: [{ title: 'Row', intent: 'open:b', action: { label: 'Send', intent: 'send:b' } }] }) })

function mounted(near = true) {
  const h = renderHook(props => useAppBlockSession(props), { block, near })
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
  assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), true)
  const first = h.result.current.event
  assert.equal(first.generation, 1)
  assert.equal(h.result.current.dispatch('send-batch:sel-1', 'confirm'), false)
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
  report({ actions: [{ key: 'send:b', confirming: true, confirmation: [{ title: 'Exact', facts: [] }] }] })
  h.result.current.dispatch('send:b', 'confirm')
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
