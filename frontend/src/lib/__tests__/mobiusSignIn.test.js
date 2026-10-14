import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clearSignInMarks,
  markExplicitSignOut,
  recordSignInRedirect,
  shouldRedirectToMobiusSignIn,
} from '../mobiusSignIn.js'

function memoryStorage(initial = {}) {
  const data = new Map(Object.entries(initial))
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => { data.set(key, String(value)) },
    removeItem: (key) => { data.delete(key) },
  }
}

test('redirects a signed-out visitor of a mobius.you instance on a fresh tab', () => {
  assert.equal(shouldRedirectToMobiusSignIn({ authMode: 'mobius', storage: memoryStorage() }), true)
})

test('never redirects a self-hosted instance that signs in with a password', () => {
  for (const authMode of ['local', undefined, null, '']) {
    assert.equal(shouldRedirectToMobiusSignIn({ authMode, storage: memoryStorage() }), false, String(authMode))
  }
})

test('an explicit sign-out keeps the card so the owner is not signed straight back in', () => {
  const storage = memoryStorage()
  markExplicitSignOut(storage)
  assert.equal(shouldRedirectToMobiusSignIn({ authMode: 'mobius', storage }), false)
})

test('coming back from mobius.you without signing in shows the card instead of looping', () => {
  const storage = memoryStorage()
  recordSignInRedirect(storage)
  // However long the visitor stayed away, only a completed sign-in clears it.
  assert.equal(shouldRedirectToMobiusSignIn({ authMode: 'mobius', storage }), false)
})

test('an installed standalone app keeps the card', () => {
  assert.equal(
    shouldRedirectToMobiusSignIn({ authMode: 'mobius', standalone: true, storage: memoryStorage() }),
    false,
  )
})

test('completing a sign-in clears both marks', () => {
  const storage = memoryStorage()
  markExplicitSignOut(storage)
  recordSignInRedirect(storage)
  clearSignInMarks(storage)
  assert.equal(shouldRedirectToMobiusSignIn({ authMode: 'mobius', storage }), true)
})

test('unreadable storage keeps the card, the always-safe choice', () => {
  assert.equal(shouldRedirectToMobiusSignIn({ authMode: 'mobius', storage: null }), false)
  const throwing = { getItem() { throw new Error('denied') }, setItem() { throw new Error('denied') } }
  assert.equal(shouldRedirectToMobiusSignIn({ authMode: 'mobius', storage: throwing }), false)
  assert.doesNotThrow(() => markExplicitSignOut(throwing))
  assert.doesNotThrow(() => recordSignInRedirect(throwing))
})

test('storage that reads but rejects writes keeps the card, so a sign-out cannot be undone', () => {
  const full = {
    getItem: () => null,
    setItem() { throw new DOMException('quota', 'QuotaExceededError') },
    removeItem() {},
  }
  assert.equal(shouldRedirectToMobiusSignIn({ authMode: 'mobius', storage: full }), false)
  assert.equal(recordSignInRedirect(full), false)
})

test('a successful redirect mark reports that it was stored', () => {
  assert.equal(recordSignInRedirect(memoryStorage()), true)
})
