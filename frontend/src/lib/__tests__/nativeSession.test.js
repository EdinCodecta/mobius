import { beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'

import {
  isNativeSignOutAvailable,
  notifyAppSignedOut,
  resetNativeSessionForTests,
} from '../nativeSession.js'

function appWindow(signOut) {
  const calls = []
  return {
    calls,
    win: {
      MobiusNative: {
        signOut: (...args) => {
          calls.push(args)
          return signOut()
        },
      },
    },
  }
}

beforeEach(() => resetNativeSessionForTests())

test('a normal browser keeps its own sign-in handling', async () => {
  assert.equal(isNativeSignOutAvailable({}), false)
  assert.equal(await notifyAppSignedOut({ win: {} }), false)
  // An older app build that predates signOut is a normal browser here.
  assert.equal(await notifyAppSignedOut({ win: { MobiusNative: { registerPush() {} } } }), false)
})

test('the app takes over after being told once', async () => {
  const { win, calls } = appWindow(async () => null)
  assert.equal(await notifyAppSignedOut({ win }), true)
  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0], [])
})

test('several sessions ending at once make a single call to the app', async () => {
  let finish
  const { win, calls } = appWindow(() => new Promise((resolve) => { finish = resolve }))
  const results = [notifyAppSignedOut({ win }), notifyAppSignedOut({ win }), notifyAppSignedOut({ win })]
  finish(null)
  assert.deepEqual(await Promise.all(results), [true, true, true])
  assert.equal(await notifyAppSignedOut({ win }), true)
  assert.equal(calls.length, 1)
})

test('a failed call falls back to the browser path and can be tried again', async () => {
  let fail = true
  const { win, calls } = appWindow(async () => {
    if (fail) throw new Error('bridge unavailable')
    return null
  })
  assert.equal(await notifyAppSignedOut({ win }), false)
  fail = false
  assert.equal(await notifyAppSignedOut({ win }), true)
  assert.equal(calls.length, 2)
})
