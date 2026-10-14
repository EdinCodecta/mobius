// Native push bridge for the Möbius mobile app.
//
// The app injects `window.MobiusNative` into the top frame on the owner's own
// origin (never in mini-app iframes, never in a plain browser). Its
// `registerPush()` raises the OS permission prompt the first time and resolves
// to `{ token, platform, apnsToken?, apnsEnvironment? }`, or `null` when push
// is denied or unavailable. After one successful call in a page, a rotated
// token arrives as a `mobius:push-token` event. The server sends through the
// mobius.you push relay; tapping a notification opens its `url` in the app.
//
// This module owns the page side of that contract:
//
//   - feature-detect the bridge;
//   - turn notifications on from a user action (the call can prompt);
//   - re-register on every load once turned on, so rotation is re-armed and
//     the server sees the phone is still in use;
//   - re-register a rotated token, retiring the previous one;
//   - forget the token on sign-out or when the owner turns it off.
//
// A tiny subscribe/getState surface feeds the Settings row.

import { api } from '../api/client.js'

const STATE_KEY = 'mobius.nativePush.v1'

let listenerTarget = null
let state = { status: 'idle', error: null }
const subscribers = new Set()

function setStatus(status, error = null) {
  state = { status, error }
  for (const notify of subscribers) notify()
}

export function subscribeNativePush(notify) {
  subscribers.add(notify)
  return () => subscribers.delete(notify)
}

export function getNativePushState() {
  return state
}

function readStored() {
  try {
    const value = JSON.parse(localStorage.getItem(STATE_KEY) || 'null')
    if (value && typeof value === 'object') {
      return { enabled: value.enabled === true, token: typeof value.token === 'string' ? value.token : null }
    }
  } catch {
    // Unreadable storage behaves like a phone that never turned push on.
  }
  return { enabled: false, token: null }
}

function writeStored(value) {
  try {
    localStorage.setItem(STATE_KEY, JSON.stringify(value))
  } catch {
    // Without storage the owner turns it on again after a reload.
  }
}

export function isNativePushAvailable(win = globalThis.window) {
  return typeof win?.MobiusNative?.registerPush === 'function'
}

export function isNativePushEnabled() {
  return readStored().enabled
}

async function registerWithServer(result, push) {
  if (!result || typeof result.token !== 'string' || !result.token || !result.platform) {
    throw new Error('The app returned no push token.')
  }
  const previous = readStored().token
  const body = { token: result.token, platform: result.platform }
  if (result.apnsToken) {
    body.apnsToken = result.apnsToken
    body.apnsEnvironment = result.apnsEnvironment
  }
  if (previous && previous !== result.token) body.replacesToken = previous
  const response = await push.devices.register(body)
  // apiFetch resolves a non-2xx Response instead of throwing.
  if (!response || response.ok === false) {
    throw new Error(`Phone registration failed (${response?.status ?? 'no response'}).`)
  }
  writeStored({ enabled: true, token: result.token })
}

function listenForRotation(win, push) {
  if (listenerTarget === win) return
  listenerTarget = win
  win.addEventListener('mobius:push-token', (event) => {
    if (!readStored().enabled) return
    registerWithServer(event?.detail, push).then(
      () => setStatus('on'),
      (error) => setStatus('error', error.message),
    )
  })
}

async function register(win, push) {
  const result = await win.MobiusNative.registerPush()
  if (!result) {
    writeStored({ enabled: false, token: null })
    setStatus('denied')
    return false
  }
  listenForRotation(win, push)
  await registerWithServer(result, push)
  setStatus('on')
  return true
}

/** Turn notifications on for this phone. Call from a tap: it can prompt. */
export async function enableNativePush({ win = globalThis.window, push = api.push } = {}) {
  if (!isNativePushAvailable(win)) return false
  setStatus('registering')
  try {
    return await register(win, push)
  } catch (error) {
    setStatus('error', error.message)
    return false
  }
}

/**
 * Re-register on page load when the owner turned push on earlier. Permission
 * is already granted then, so no prompt appears. A failure keeps the choice so
 * the next load tries again.
 */
export async function resumeNativePush({ win = globalThis.window, push = api.push } = {}) {
  if (!isNativePushAvailable(win) || !readStored().enabled) return false
  try {
    return await register(win, push)
  } catch (error) {
    setStatus('error', error.message)
    return false
  }
}

/** Stop notifications to this phone; the OS permission itself is untouched. */
export async function disableNativePush({ push = api.push } = {}) {
  const { token } = readStored()
  writeStored({ enabled: false, token: null })
  setStatus('off')
  if (token) await push.devices.forget({ token })
}

/** Sign-out step: forget this phone while the owner credential still works. */
export async function forgetNativePushForSignOut({ push = api.push } = {}) {
  const { token } = readStored()
  writeStored({ enabled: false, token: null })
  if (token) await push.devices.forget({ token })
}

export function resetNativePushForTests() {
  listenerTarget = null
  state = { status: 'idle', error: null }
  subscribers.clear()
}
