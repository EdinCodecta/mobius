import test from 'node:test'
import assert from 'node:assert/strict'
import {
  acceptsBlockState, blockEventDelivery, blockSessionEnvelope, createBlockDocuments, hostRequestAllowed,
} from '../../components/AppCanvas/appBlockSessionHost.js'

test('every frame load is a new, never-reused document generation', () => {
  const documents = createBlockDocuments()
  const first = documents.load('v1')
  const reload = documents.load('v1')
  const successor = documents.load('v2')
  assert.ok(first.generation < reload.generation && reload.generation < successor.generation)
  const remounted = createBlockDocuments().load('v1')
  assert.ok(remounted.generation > successor.generation, 'a remounted canvas never reuses a generation')
  assert.equal(documents.get('v1'), reload)
  assert.equal(documents.mounted('v1', 'yes').supported, false, 'only an explicit true is support')
  assert.equal(documents.mounted('v2', true).supported, true)
  assert.equal(documents.mounted('gone', true), null)
  documents.delete('v2')
  assert.equal(documents.get('v2'), null)
})

test('the init envelope carries the session and its document generation, nothing else', () => {
  const session = { sessionId: 's', actions: [{ key: 'a' }], retain: true, checkpoint: { id: 'c', data: 'd' }, extra: 'x' }
  assert.deepEqual(blockSessionEnvelope(session, 4), { sessionId: 's', generation: 4, actions: [{ key: 'a' }],
    checkpoint: { id: 'c', data: 'd' }, retain: true, recoveryError: null })
  assert.equal(blockSessionEnvelope(null, 4), null)
  assert.equal(blockSessionEnvelope(session, undefined), null)
})

test('state is accepted only from the live supporting document for this session', () => {
  const session = { sessionId: 's' }
  const document = { generation: 2, supported: true }
  const message = { sessionId: 's' }
  assert.equal(acceptsBlockState({ message, session, document, isLive: true }), true)
  assert.equal(acceptsBlockState({ message, session, document, isLive: false }), false)
  assert.equal(acceptsBlockState({ message, session, document: { generation: 2, supported: null }, isLive: true }), false)
  assert.equal(acceptsBlockState({ message: { sessionId: 'other' }, session, document, isLive: true }), false)
  assert.equal(acceptsBlockState({ message, session: null, document, isLive: true }), false)
})

test('an owner event is posted once, only to the generation it was dispatched for', () => {
  const session = { sessionId: 's' }
  const document = { generation: 2, supported: true }
  const event = { sessionId: 's', generation: 2, key: 'send:a', event: 'confirm', nonce: 'n1', extra: 'dropped' }
  assert.deepEqual(blockEventDelivery({ session, event, document, sentNonce: null }),
    { type: 'moebius:app-block-action', sessionId: 's', generation: 2, key: 'send:a', event: 'confirm', nonce: 'n1' })
  assert.equal(blockEventDelivery({ session, event, document, sentNonce: 'n1' }), null, 'never replayed')
  assert.equal(blockEventDelivery({ session, event, document: { generation: 3, supported: true }, sentNonce: null }), null,
    'a promoted or reloaded document never receives the previous document event')
  assert.equal(blockEventDelivery({ session, event, document: { generation: 2, supported: false }, sentNonce: null }), null)
  assert.equal(blockEventDelivery({ session: { sessionId: 'other' }, event, document, sentNonce: null }), null)
})

test('host requests are honoured only from the live document of a visible canvas', () => {
  assert.equal(hostRequestAllowed({ sourceVersion: 'v1', liveVersion: 'v1', visible: true }), true)
  assert.equal(hostRequestAllowed({ sourceVersion: 'v1', liveVersion: 'v1', visible: false }), false)
  assert.equal(hostRequestAllowed({ sourceVersion: 'v2', liveVersion: 'v1', visible: true }), false)
})
