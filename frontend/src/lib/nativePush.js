// Native push bridge for the Möbius mobile app.
//
// The app injects `window.MobiusNative` into the top frame on the owner's own
// origin (never in mini-app iframes, never in a plain browser). Its
// `registerPush()` raises the OS permission prompt the first time and resolves
// to `{ token, ... }`, or `null` when push is denied or unavailable. After one
// call in a page, a rotated token arrives as a `mobius:push-token` event. The
// server sends through the mobius.you push relay; tapping a notification opens
// its `url` in the app.
//
// The shell registers on every load: that keeps the relay binding fresh and
// re-arms rotation. Sign-out forgets the phone while the credential still
// works. To stop notifications otherwise, the owner turns them off for the
// app in the phone's settings.

import { api } from '../api/client.js'

// The last token the server accepted, so sign-out knows what to forget.
const TOKEN_KEY = 'mobius.nativePush.token'

let listeningOn = null
let pending = Promise.resolve()
let signedOut = false

function storedToken() {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

function storeToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    // Without storage, sign-out cannot forget this phone; the next owner to
    // sign in on it takes it over.
  }
}

function save(result, push) {
  const token = result?.token
  if (signedOut || typeof token !== 'string' || !token) return pending
  // Serialized, so sign-out can wait for a registration already in flight.
  pending = pending.catch(() => {}).then(async () => {
    if (signedOut) return
    const response = await push.devices.register({ token })
    if (response?.ok) storeToken(token)
  })
  return pending
}

export function isNativePushAvailable(win = globalThis.window) {
  return typeof win?.MobiusNative?.registerPush === 'function'
}

/** Whether a registered device (by its token's last characters) is this phone. */
export function isThisPhone(tokenTail) {
  const token = storedToken()
  return !!token && !!tokenTail && token.endsWith(tokenTail)
}

/** Register this phone for owner notifications; a no-op outside the app. */
export async function registerNativePush({ win = globalThis.window, push = api.push } = {}) {
  if (!isNativePushAvailable(win)) return
  if (listeningOn !== win) {
    listeningOn = win
    win.addEventListener('mobius:push-token', (event) => {
      save(event?.detail, push).catch(() => {})
    })
  }
  await save(await win.MobiusNative.registerPush(), push)
}

/** Sign-out: stop notifications to this phone while the credential works. */
export async function forgetNativePush({ push = api.push } = {}) {
  signedOut = true
  await pending.catch(() => {})
  const token = storedToken()
  if (!token) return
  const response = await push.devices.forget({ token })
  if (response?.ok) storeToken(null)
}

export function resetNativePushForTests() {
  listeningOn = null
  pending = Promise.resolve()
  signedOut = false
}
