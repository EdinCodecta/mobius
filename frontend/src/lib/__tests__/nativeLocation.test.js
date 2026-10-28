/**
 * Native location tracking: resuming on page load is scoped to one app session.
 *
 * A reload inside the mobile app must re-arm tracking, but a relaunch after the
 * owner closed the app must not call startLocation (that would show the OS
 * permission prompt unasked). sessionStorage is what tells the two apart.
 */
import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'

const SESSION_ACTIVE_KEY = 'mobius.location.session-active.v1'
const ENABLED_KEY = 'mobius.location.enabled.v1'

class MemoryStorage {
  constructor(entries = {}) { this.map = new Map(Object.entries(entries)) }
  getItem(key) { return this.map.has(key) ? this.map.get(key) : null }
  setItem(key, value) { this.map.set(key, String(value)) }
  removeItem(key) { this.map.delete(key) }
}

const saved = {}
for (const key of ['window', 'document', 'localStorage', 'sessionStorage', 'fetch']) {
  saved[key] = Object.getOwnPropertyDescriptor(globalThis, key)
}

afterEach(() => {
  for (const [key, desc] of Object.entries(saved)) {
    if (desc) Object.defineProperty(globalThis, key, desc)
    else delete globalThis[key]
  }
})

function setGlobal(key, value) {
  Object.defineProperty(globalThis, key, { value, configurable: true, writable: true })
}

// Installs a fake mobile-app environment and loads a fresh controller, as a
// page load would. `session` is this web view's sessionStorage contents.
async function loadPage({
  serverEnabled = false,
  localEnabled = serverEnabled,
  session = {},
  startResult = 'started',
  native = true,
} = {}) {
  const calls = { start: [], stop: 0, puts: [] }
  const server = { enabled: serverEnabled, options: {} }
  const win = new EventTarget()
  if (native) {
    win.MobiusNative = {
      async startLocation(opts) { calls.start.push(opts); return startResult },
      async stopLocation() { calls.stop += 1; return 'stopped' },
    }
  }
  const sessionStorage = new MemoryStorage(session)
  const localStorage = new MemoryStorage(localEnabled ? { [ENABLED_KEY]: 'true' } : {})
  setGlobal('window', win)
  setGlobal('document', new EventTarget())
  setGlobal('localStorage', localStorage)
  setGlobal('sessionStorage', sessionStorage)
  setGlobal('fetch', async (url, init = {}) => {
    const path = String(url).replace(/^.*\/api/, '')
    if (path === '/location/tracking' && (init.method || 'GET') === 'PUT') {
      const body = JSON.parse(init.body)
      calls.puts.push(body.enabled)
      server.enabled = body.enabled
      server.options = body.options
      return new Response('{}', { status: 200 })
    }
    if (path === '/location/tracking') {
      return Response.json({ enabled: server.enabled, options: server.options })
    }
    return Response.json({})
  })
  const mod = await import(new URL(`../nativeLocation.js?t=${Math.random()}`, import.meta.url))
  return { mod, calls, server, sessionStorage, localStorage }
}

const flush = () => new Promise((resolve) => setImmediate(resolve))

test('a reload with tracking on in this app session resumes tracking', async () => {
  const page = await loadPage({ serverEnabled: true, session: { [SESSION_ACTIVE_KEY]: '1' } })
  assert.equal(page.mod.getLocationState().enabled, true)

  await page.mod.initLocationTracking()

  assert.equal(page.calls.start.length, 1)
  const state = page.mod.getLocationState()
  assert.equal(state.enabled, true)
  assert.equal(state.status, 'started')
  assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), '1')
})

test('a fresh launch with stale "on" state stays stopped and never calls startLocation', async () => {
  const page = await loadPage({ serverEnabled: true, session: {} })
  // Even before boot finishes, the UI must not show the stale "on".
  assert.equal(page.mod.getLocationState().enabled, false)

  await page.mod.initLocationTracking()
  await flush()

  assert.equal(page.calls.start.length, 0)
  const state = page.mod.getLocationState()
  assert.equal(state.enabled, false)
  assert.equal(state.status, 'stopped')
  // The stored state is corrected too, so nothing keeps claiming tracking is on.
  assert.deepEqual(page.calls.puts, [false])
  assert.equal(page.server.enabled, false)
  assert.equal(page.localStorage.getItem(ENABLED_KEY), 'false')
})

test('a fresh launch falls back to the local mirror offline and still stays stopped', async () => {
  const page = await loadPage({ serverEnabled: false, localEnabled: true, session: {} })
  setGlobal('fetch', async () => { throw new Error('offline') })

  await page.mod.initLocationTracking()

  assert.equal(page.calls.start.length, 0)
  assert.equal(page.mod.getLocationState().enabled, false)
  assert.equal(page.localStorage.getItem(ENABLED_KEY), 'false')
})

test('tapping Start sets the session flag and Stop clears it', async () => {
  const page = await loadPage()
  await page.mod.initLocationTracking()
  assert.equal(page.calls.start.length, 0)

  assert.equal(await page.mod.startTracking({ background: true }), 'started')
  assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), '1')
  assert.equal(page.mod.getLocationState().enabled, true)

  assert.equal(await page.mod.stopTracking(), 'stopped')
  assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), null)
  assert.equal(page.calls.stop, 1)
  const state = page.mod.getLocationState()
  assert.equal(state.enabled, false)
  assert.equal(state.status, 'stopped')
})

for (const result of ['denied', 'disabled', 'unavailable']) {
  test(`a "${result}" start shows stopped with that status and clears the flag`, async () => {
    const page = await loadPage({ startResult: result, session: { [SESSION_ACTIVE_KEY]: '1' } })

    assert.equal(await page.mod.startTracking({ background: true }), result)

    const state = page.mod.getLocationState()
    assert.equal(state.enabled, false)
    assert.equal(state.status, result)
    assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), null)
  })
}

test('a reload resume that is refused ends stopped and records it', async () => {
  const page = await loadPage({
    serverEnabled: true,
    startResult: 'denied',
    session: { [SESSION_ACTIVE_KEY]: '1' },
  })

  await page.mod.initLocationTracking()
  await flush()

  assert.equal(page.calls.start.length, 1)
  const state = page.mod.getLocationState()
  assert.equal(state.enabled, false)
  assert.equal(state.status, 'denied')
  assert.equal(page.sessionStorage.getItem(SESSION_ACTIVE_KEY), null)
  assert.equal(page.server.enabled, false)
})

test('regaining focus after a fresh launch does not start tracking', async () => {
  const page = await loadPage({ serverEnabled: true, session: {} })
  await page.mod.initLocationTracking()
  // Simulate another phone (or a lost write) leaving the server preference on.
  page.server.enabled = true

  window.dispatchEvent(new Event('focus'))
  await flush()

  assert.equal(page.calls.start.length, 0)
})

test('a device without the phone app cannot stop tracking or change its interval', async () => {
  const page = await loadPage({ native: false, serverEnabled: true })
  await page.mod.initLocationTracking()

  assert.equal(await page.mod.stopTracking(), 'unavailable')
  await page.mod.setTrackingOptions({ interval: 60_000 })
  assert.equal(await page.mod.startTracking({ background: true }), 'unavailable')
  await flush()

  assert.deepEqual(page.calls.puts, [])
  assert.equal(page.server.enabled, true)
})

test('a phone keeps tracking when the saved setting is changed elsewhere', async () => {
  const page = await loadPage({ serverEnabled: true, session: { [SESSION_ACTIVE_KEY]: '1' } })
  await page.mod.initLocationTracking()
  page.server.enabled = false
  page.server.options = { interval: 60_000 }

  window.dispatchEvent(new Event('focus'))
  document.dispatchEvent(new Event('visibilitychange'))
  await flush()

  assert.equal(page.calls.stop, 0)
  assert.equal(page.calls.start.length, 1)
  assert.equal(page.mod.getLocationState().status, 'started')
})

test('a reload in the same app session resumes even if the saved setting says off', async () => {
  const page = await loadPage({ serverEnabled: false, session: { [SESSION_ACTIVE_KEY]: '1' } })
  await page.mod.initLocationTracking()

  assert.equal(page.calls.start.length, 1)
  assert.equal(page.mod.getLocationState().enabled, true)
})
