import { beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'

import { shouldRedirectToMobiusSignIn } from '../mobiusSignIn.js'
import {
  handOffExplicitSignOut,
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
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(calls.length, 1)
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

test('a bridge that throws immediately can still be tried again', async () => {
  let fail = true
  const { win, calls } = appWindow(() => {
    if (fail) throw new Error('bridge unavailable')
    return Promise.resolve(null)
  })
  assert.equal(await notifyAppSignedOut({ win }), false)
  fail = false
  assert.equal(await notifyAppSignedOut({ win }), true)
  assert.equal(calls.length, 2)
})

function memoryStores() {
  const storage = () => {
    const data = new Map()
    return {
      getItem: (key) => (data.has(key) ? data.get(key) : null),
      setItem: (key, value) => { data.set(key, String(value)) },
      removeItem: (key) => { data.delete(key) },
    }
  }
  return { local: storage(), session: storage() }
}

function redirectsAfterReload(stores) {
  return shouldRedirectToMobiusSignIn({ authMode: 'mobius', appOwnsSignOut: true, stores })
}

test('a sign-out the app takes over lets the app sign straight back in later', async () => {
  const stores = memoryStores()
  const { win } = appWindow(async () => null)
  assert.equal(await handOffExplicitSignOut({ win, stores }), true)
  assert.equal(redirectsAfterReload(stores), true)
})

test('a failed hand-off keeps the explicit sign-out across the fallback reload', async () => {
  // The app is still signed in to mobius.you: redirecting would undo the sign-out.
  const stores = memoryStores()
  const { win } = appWindow(async () => { throw new Error('bridge unavailable') })
  assert.equal(await handOffExplicitSignOut({ win, stores }), false)
  assert.equal(redirectsAfterReload(stores), false)
})

test('a browser sign-out is not handed anywhere', async () => {
  const stores = memoryStores()
  assert.equal(await handOffExplicitSignOut({ win: {}, stores }), false)
  assert.equal(stores.local.getItem('mobius_signed_out'), null)
})
