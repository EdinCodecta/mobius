import test from 'node:test'
import assert from 'node:assert/strict'

import { navPushOutcome, serveClipboardWrite } from '../../components/AppCanvas/appFrameProtocol.js'

test('clipboard host returns only the attributed write outcome', async () => {
  const replies = []
  const source = { postMessage: (message) => replies.push(message) }
  assert.equal(serveClipboardWrite({
    message: {
      type: 'moebius:clipboard-write',
      requestId: 'copy-1',
      text: 'one command',
    },
    source,
    writeText: async (text) => text === 'one command',
  }), true)
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(replies, [{
    type: 'moebius:clipboard-write-result', requestId: 'copy-1', ok: true,
  }])
})

test('clipboard host ignores unrelated messages', () => {
  assert.equal(serveClipboardWrite({
    message: { type: 'something-else' },
    source: null,
    writeText: async () => true,
  }), false)
})

test('a nav-push is acknowledged only by a visible canvas with a history owner', () => {
  const message = { type: 'moebius:nav-push', requestId: 'r1', label: 'Detail', reversible: 'yes', userActivated: true }
  const calls = []
  const onNavPush = (appId, meta) => { calls.push([appId, meta]); return true }
  assert.equal(navPushOutcome({ visible: true, onNavPush, appId: 7, message }), true)
  assert.deepEqual(calls, [[7, { requestId: 'r1', label: 'Detail', reversible: false, userActivated: true }]])
  assert.equal(navPushOutcome({ visible: false, onNavPush, appId: 7, message }), false)
  // An embedded transcript view has no history owner: reject, never acknowledge.
  assert.equal(navPushOutcome({ visible: true, onNavPush: undefined, appId: 7, message }), false)
  assert.equal(calls.length, 1)
})
