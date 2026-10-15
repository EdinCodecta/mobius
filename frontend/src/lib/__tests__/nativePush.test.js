import { afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'

import {
  forgetNativePush,
  isThisPhone,
  registerNativePush,
  resetNativePushForTests,
} from '../nativePush.js'

const originalLocalStorage = globalThis.localStorage
const values = new Map()
const TOKEN_KEY = 'mobius.nativePush.token'

function fakePush({ ok = true, forgetOk = true } = {}) {
  const calls = []
  return {
    calls,
    devices: {
      register: async (body) => { calls.push(['register', body]); return { ok, status: ok ? 200 : 409 } },
      forget: async (body) => { calls.push(['forget', body]); return { ok: forgetOk, status: forgetOk ? 204 : 500 } },
    },
  }
}

// A fake app web view whose registerPush resolves to `result`.
function app(result) {
  const listeners = new Map()
  return {
    MobiusNative: { registerPush: async () => result },
    addEventListener(type, listener) { listeners.set(type, listener) },
    emit(type, detail) { listeners.get(type)?.({ detail }) },
  }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

beforeEach(() => {
  values.clear()
  globalThis.localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, String(value)) },
    removeItem: (key) => { values.delete(key) },
  }
  resetNativePushForTests()
})

afterEach(() => {
  globalThis.localStorage = originalLocalStorage
})

test('outside the mobile app nothing is registered', async () => {
  const push = fakePush()
  await registerNativePush({ win: {}, push })
  assert.deepEqual(push.calls, [])
})

test('inside the app the token is registered and remembered for sign-out', async () => {
  const push = fakePush()
  await registerNativePush({ win: app({ token: 'tok-1', platform: 'android' }), push })

  // Only the token leaves the page; the relay delivers through FCM.
  assert.deepEqual(push.calls, [['register', { token: 'tok-1' }]])
  assert.equal(values.get(TOKEN_KEY), 'tok-1')
})

test('denied permission registers nothing', async () => {
  const push = fakePush()
  await registerNativePush({ win: app(null), push })
  assert.deepEqual(push.calls, [])
  assert.equal(values.has(TOKEN_KEY), false)
})

test('a refused registration is not remembered', async () => {
  const push = fakePush({ ok: false })
  await registerNativePush({ win: app({ token: 'tok-1' }), push })
  assert.equal(values.has(TOKEN_KEY), false)
})

test('a rotated token is registered', async () => {
  const push = fakePush()
  const win = app({ token: 'tok-1' })
  await registerNativePush({ win, push })

  win.emit('mobius:push-token', { token: 'tok-2' })
  await settle()

  assert.deepEqual(push.calls.at(-1), ['register', { token: 'tok-2' }])
  assert.equal(values.get(TOKEN_KEY), 'tok-2')
})

test('sign-out forgets the registered token', async () => {
  const push = fakePush()
  await registerNativePush({ win: app({ token: 'tok-1' }), push })

  await forgetNativePush({ push })

  assert.deepEqual(push.calls.at(-1), ['forget', { token: 'tok-1' }])
  assert.equal(values.has(TOKEN_KEY), false)
})

test('a failed forget keeps the token so it is not lost', async () => {
  const push = fakePush({ forgetOk: false })
  await registerNativePush({ win: app({ token: 'tok-1' }), push })

  await forgetNativePush({ push })

  assert.equal(values.get(TOKEN_KEY), 'tok-1')
})

test('sign-out waits for a registration in flight and blocks later ones', async () => {
  const calls = []
  let finishRegister
  const push = {
    devices: {
      register: (body) => {
        calls.push(['register', body])
        return new Promise((resolve) => { finishRegister = () => resolve({ ok: true }) })
      },
      forget: async (body) => { calls.push(['forget', body]); return { ok: true } },
    },
  }
  const win = app({ token: 'tok-1' })
  const registering = registerNativePush({ win, push })
  await settle()

  const forgetting = forgetNativePush({ push })
  finishRegister()
  await Promise.all([registering, forgetting])
  win.emit('mobius:push-token', { token: 'tok-2' })
  await settle()

  assert.deepEqual(calls, [['register', { token: 'tok-1' }], ['forget', { token: 'tok-1' }]])
})

test('Settings recognizes this phone by the end of its registered token', async () => {
  assert.equal(isThisPhone('ab12cd'), false)
  await registerNativePush({ win: app({ token: 'token-ab12cd' }), push: fakePush() })

  assert.equal(isThisPhone('ab12cd'), true)
  assert.equal(isThisPhone('zz99zz'), false)
  assert.equal(isThisPhone(''), false)
})
