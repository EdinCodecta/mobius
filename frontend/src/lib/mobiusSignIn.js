// When this instance belongs to a mobius.you account, a signed-out visitor is
// sent straight to mobius.you to sign in instead of seeing a card that only
// holds a "Sign in with mobius.you" button. Three cases keep the card instead,
// so the redirect never traps anyone:
//   - an installed standalone app, whose storage is separate from the shell's:
//     the round trip would land the session in the wrong place;
//   - the owner just signed out here on purpose, so signing straight back in
//     would silently undo it;
//   - this tab already went to mobius.you and came back without signing in
//     (Back button, a failed round trip): the card is the way to retry.
// The marks live in sessionStorage, so they end with the tab and never leak
// across browsers or owners, and a completed sign-in clears them.

const SIGNED_OUT_KEY = 'mobius_signed_out'
const REDIRECTED_KEY = 'mobius_sign_in_redirected'
const PROBE_KEY = 'mobius_sign_in_probe'

function sessionStore() {
  try {
    return globalThis.sessionStorage ?? null
  } catch {
    return null
  }
}

export function markExplicitSignOut(storage = sessionStore()) {
  try { storage?.setItem(SIGNED_OUT_KEY, '1') } catch { /* private mode: card is the safe default */ }
}

/** Forget both marks once a sign-in has completed. */
export function clearSignInMarks(storage = sessionStore()) {
  try {
    storage?.removeItem(SIGNED_OUT_KEY)
    storage?.removeItem(REDIRECTED_KEY)
  } catch { /* ignore */ }
}

// Readable is not enough: with a full or read-only store the marks below would
// silently fail to persist, and a sign-out could be undone or a redirect could
// repeat. Only a store that takes a write counts as usable.
function storageAcceptsWrites(storage) {
  try {
    storage.setItem(PROBE_KEY, '1')
    const ok = storage.getItem(PROBE_KEY) === '1'
    storage.removeItem(PROBE_KEY)
    return ok
  } catch {
    return false
  }
}

export function shouldRedirectToMobiusSignIn({
  authMode,
  standalone = false,
  storage = sessionStore(),
}) {
  if (authMode !== 'mobius' || standalone) return false
  // Without a usable store we cannot tell a loop from a first visit, so the
  // card stays: it is always safe and one tap from the same place.
  if (!storage || !storageAcceptsWrites(storage)) return false
  try {
    return !storage.getItem(SIGNED_OUT_KEY) && !storage.getItem(REDIRECTED_KEY)
  } catch {
    return false
  }
}

/** Returns whether the mark was stored; navigate only when it was. */
export function recordSignInRedirect(storage = sessionStore()) {
  try {
    storage.setItem(REDIRECTED_KEY, '1')
    return storage.getItem(REDIRECTED_KEY) === '1'
  } catch {
    return false
  }
}
