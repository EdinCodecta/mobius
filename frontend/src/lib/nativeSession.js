// Session-end bridge for the Möbius mobile app.
//
// The app shows this workspace in a web view and keeps its own mobius.you
// sign-in, separate from the workspace session. It injects
// `window.MobiusNative` into the top frame on the owner's own origin (never in
// mini-app iframes, never in a plain browser). `signOut()` tells it the
// workspace session ended: the app signs out of its own account, closes the web
// view and shows its native start screen. Without this call the app keeps
// reopening the workspace's sign-in page on every launch.
//
// Call it only after the workspace's own sign-out or expiry cleanup has
// finished (the app removes the page as soon as it gets the call) and before
// any redirect or reload (which would prevent the call).

import { markExplicitSignOut } from './mobiusSignIn.js'

let notification = null

export function isNativeSignOutAvailable(win = globalThis.window) {
  return typeof win?.MobiusNative?.signOut === 'function'
}

/**
 * In the Möbius app, tell it the workspace session ended. Resolves to true when
 * the app took over, so the caller must not redirect or reload. Resolves to
 * false in a normal browser, or when the app's call failed, so the caller keeps
 * its ordinary handling. Concurrent callers share one call to the app.
 */
export function notifyAppSignedOut({ win = globalThis.window } = {}) {
  if (!isNativeSignOutAvailable(win)) return Promise.resolve(false)
  if (!notification) {
    // Call the bridge in a continuation so a synchronous throw is handled after
    // `notification` is stored, and the reset below is not overwritten.
    notification = Promise.resolve()
      .then(() => win.MobiusNative.signOut())
      .then(() => true, () => {
        // The app did not take over; let a later session end try again.
        notification = null
        return false
      })
  }
  return notification
}

/**
 * Settings sign-out, after the workspace cleanup: hand it to the app. The mark
 * records the hand-off first, because the app may close the page before the
 * call resolves; a failed hand-off restores the ordinary explicit sign-out
 * mark, so the fallback reload cannot sign straight back in through the app's
 * still-signed-in mobius.you account. Resolves like notifyAppSignedOut.
 */
export async function handOffExplicitSignOut({ win = globalThis.window, stores } = {}) {
  if (!isNativeSignOutAvailable(win)) return false
  markExplicitSignOut(stores, { handedToApp: true })
  if (await notifyAppSignedOut({ win })) return true
  markExplicitSignOut(stores)
  return false
}

export function resetNativeSessionForTests() {
  notification = null
}
