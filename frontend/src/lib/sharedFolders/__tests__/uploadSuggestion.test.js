import { test } from 'node:test'
import assert from 'node:assert/strict'
import { shouldSuggestSharedFolder } from '../uploadSuggestion.js'

const ready = { status: 'ready', canRemember: true, canPickForVisit: true }
const files = (count, size = 1000) => Array.from({ length: count }, () => ({ size }))
const MB = 1024 * 1024

test('more than 5 files or more than 10 MB suggests sharing the folder', () => {
  assert.equal(shouldSuggestSharedFolder(files(5), ready), false)
  assert.equal(shouldSuggestSharedFolder(files(6), ready), true)
  assert.equal(shouldSuggestSharedFolder(files(1, 10 * MB), ready), false)
  assert.equal(shouldSuggestSharedFolder(files(1, 10 * MB + 1), ready), true)
  assert.equal(shouldSuggestSharedFolder(files(2, 6 * MB), ready), true)
})

test('no suggestion when this browser cannot share a folder now', () => {
  assert.equal(shouldSuggestSharedFolder(files(9), { ...ready, status: 'restart' }), false)
  assert.equal(shouldSuggestSharedFolder(files(9), { ...ready, status: 'unavailable' }), false)
  assert.equal(shouldSuggestSharedFolder(files(9), { ...ready, canRemember: false, canPickForVisit: false }), false)
  assert.equal(shouldSuggestSharedFolder(files(9), { ...ready, canRemember: false }), true)
})

test('the phone app keeps its normal upload until it can share folders', () => {
  globalThis.MobiusNative = {}
  try {
    assert.equal(shouldSuggestSharedFolder(files(9), ready), false)
  } finally {
    delete globalThis.MobiusNative
  }
})
