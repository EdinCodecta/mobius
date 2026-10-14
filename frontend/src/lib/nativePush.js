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
//   - re-register on every load once turned on, which re-arms rotation;
//   - re-register a rotated token (the server drops the old one once the
//     relay reports it invalid);
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

async function registrationError(response) {
  // apiFetch resolves a non-2xx Response instead of throwing; the server's
  // detail says why (for example, the account still needs linking).
  try {
    const { detail } = await response.json()
    if (typeof detail === 'string' && detail) return new Error(detail)
  } catch {
    // Fall through to the generic message.
  }
  return new Error(`Phone registration failed (${response?.status ?? 'no response'}).`)
}

async function registerWithServer(result, push) {
  if (!result || typeof result.token !== 'string' || !result.token || !result.platform) {
    throw new Error('The app returned no push token.')
  }
  const body = { token: result.token, platform: result.platform }
  if (result.apnsToken) {
    body.apnsToken = result.apnsToken
    body.apnsEnvironment = result.apnsEnvironment
  }
  const response = await push.devices.register(body)
  if (!response || response.ok === false) throw await registrationError(response)
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

async function registerReportingErrors(win, push) {
  try {
    return await register(win, push)
  } catch (error) {
    setStatus('error', error.message)
    return false
  }
}

/** Turn notifications on for this phone. Call from a tap: it can prompt. */
export async function enableNativePush({ win = globalThis.window, push = api.push } = {}) {
  if (!isNativePushAvailable(win)) return false
  setStatus('registering')
  return registerReportingErrors(win, push)
}

/**
 * Re-register on page load when the owner turned push on earlier. Permission
 * is already granted then, so no prompt appears. A failure keeps the choice so
 * the next load tries again.
 */
export async function resumeNativePush({ win = globalThis.window, push = api.push } = {}) {
  if (!isNativePushAvailable(win) || !readStored().enabled) return false
  return registerReportingErrors(win, push)
}

/**
 * Stop notifications to this phone (Turn off, and sign-out while the owner
 * credential still works). The OS permission itself is untouched.
 */
export async function disableNativePush({ push = api.push } = {}) {
  const { token } = readStored()
  writeStored({ enabled: false, token: null })
  setStatus('off')
  if (token) await push.devices.forget({ token })
}

export function resetNativePushForTests() {
  listenerTarget = null
  state = { status: 'idle', error: null }
  subscribers.clear()
}
