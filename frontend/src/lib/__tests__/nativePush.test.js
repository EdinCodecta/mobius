import { afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'

import {
  disableNativePush,
  enableNativePush,
  getNativePushState,
  isNativePushAvailable,
  isNativePushEnabled,
  resetNativePushForTests,
  resumeNativePush,
} from '../nativePush.js'

const originalLocalStorage = globalThis.localStorage
const values = new Map()

function fakePush({ ok = true } = {}) {
  const calls = []
  return {
    calls,
    devices: {
      register: async (body) => { calls.push(['register', body]); return { ok, status: ok ? 200 : 500 } },
      forget: async (body) => { calls.push(['forget', body]); return { ok: true, status: 204 } },
    },
  }
}

// A fake app web view: registerPush returns each result in turn, then repeats
// the last one.
function app(results) {
  const listeners = new Map()
  const win = {
    prompts: 0,
    MobiusNative: {
      registerPush: async () => {
        win.prompts += 1
        return results.length > 1 ? results.shift() : results[0]
      },
    },
    addEventListener(type, listener) { listeners.set(type, listener) },
    emit(type, detail) { listeners.get(type)?.({ detail }) },
  }
  return win
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

test('a plain browser without the app bridge does nothing', async () => {
  const push = fakePush()
  assert.equal(isNativePushAvailable({}), false)
  assert.equal(await enableNativePush({ win: {}, push }), false)
  assert.equal(await resumeNativePush({ win: {}, push }), false)
  assert.deepEqual(push.calls, [])
})

test('turning on registers the token and remembers the choice', async () => {
  const push = fakePush()
  const win = app([{ token: 'fcm-1', platform: 'android' }])

  assert.equal(await enableNativePush({ win, push }), true)

  assert.deepEqual(push.calls, [['register', { token: 'fcm-1', platform: 'android' }]])
  assert.equal(isNativePushEnabled(), true)
  assert.equal(getNativePushState().status, 'on')
})

test('optional APNs fields are forwarded for iOS', async () => {
  const push = fakePush()
  const win = app([{
    token: 'fcm-1', platform: 'ios', apnsToken: 'ab'.repeat(32), apnsEnvironment: 'sandbox',
  }])

  await enableNativePush({ win, push })

  assert.deepEqual(push.calls[0][1], {
    token: 'fcm-1', platform: 'ios', apnsToken: 'ab'.repeat(32), apnsEnvironment: 'sandbox',
  })
})

test('a denied prompt reports denied and registers nothing', async () => {
  const push = fakePush()

  assert.equal(await enableNativePush({ win: app([null]), push }), false)

  assert.equal(getNativePushState().status, 'denied')
  assert.equal(isNativePushEnabled(), false)
  assert.deepEqual(push.calls, [])
})

test('a failed server registration is an error, not a silent success', async () => {
  const push = fakePush({ ok: false })

  assert.equal(await enableNativePush({ win: app([{ token: 'fcm-1', platform: 'android' }]), push }), false)

  assert.equal(getNativePushState().status, 'error')
  assert.equal(isNativePushEnabled(), false)
})

test('the server explains a refused registration', async () => {
  const push = fakePush()
  push.devices.register = async () => ({
    ok: false,
    status: 409,
    json: async () => ({ detail: 'Link this Möbius to your mobius.you account.' }),
  })

  await enableNativePush({ win: app([{ token: 'fcm-1', platform: 'android' }]), push })

  assert.deepEqual(getNativePushState(), {
    status: 'error', error: 'Link this Möbius to your mobius.you account.',
  })
})

test('a page load re-registers only after the owner turned push on', async () => {
  const push = fakePush()
  const win = app([{ token: 'fcm-1', platform: 'android' }])

  assert.equal(await resumeNativePush({ win, push }), false)
  assert.equal(win.prompts, 0, 'never prompts without a tap')

  await enableNativePush({ win, push })
  resetNativePushForTests()
  assert.equal(await resumeNativePush({ win, push }), true)
  assert.equal(push.calls.length, 2)
})

test('a rotated token re-registers', async () => {
  const push = fakePush()
  const win = app([{ token: 'fcm-1', platform: 'android' }])
  await enableNativePush({ win, push })

  win.emit('mobius:push-token', { token: 'fcm-2', platform: 'android' })
  await settle()

  assert.deepEqual(push.calls[1], [
    'register', { token: 'fcm-2', platform: 'android' },
  ])
})

test('turning off forgets the token and ignores later rotations', async () => {
  const push = fakePush()
  const win = app([{ token: 'fcm-1', platform: 'android' }])
  await enableNativePush({ win, push })

  await disableNativePush({ push })
  win.emit('mobius:push-token', { token: 'fcm-2', platform: 'android' })
  await settle()

  assert.deepEqual(push.calls.slice(1), [['forget', { token: 'fcm-1' }]])
  assert.equal(isNativePushEnabled(), false)
})

test('turning off on a phone that never turned push on sends nothing', async () => {
  const push = fakePush()
  await disableNativePush({ push })
  assert.deepEqual(push.calls, [])
})
