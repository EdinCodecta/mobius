/**
 * A mini-app crash must reach the owner with its real message.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/frameUncaughtError.test.js
 *
 * React reports errors through the browser's window.reportError. The frame's
 * classic error script once declared its own global `reportError` (the
 * "Report to agent" button), which silently replaced that API: every error
 * React reported became an empty crash report and the real message was lost.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const frameSource = readFileSync(
  new URL('../../../public/app-frame.html', import.meta.url),
  'utf8',
)

function classicErrorScript() {
  const scripts = [...frameSource.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1])
  const script = scripts.find((body) => body.includes('function showErr('))
  assert.ok(script, 'the frame keeps a classic error-panel script')
  return script
}

test("the frame's error script leaves the browser's reportError intact", () => {
  const nativeReportError = function reportError() {}
  const window = {
    reportError: nativeReportError,
    addEventListener() {},
    parent: { postMessage() {} },
    location: { origin: 'null' },
  }
  window.window = window
  const context = vm.createContext(window)
  vm.runInContext(classicErrorScript(), context)

  assert.equal(context.reportError, nativeReportError)
  assert.equal(typeof context.reportErrorToAgent, 'function')
})

test('the Report to agent button calls the renamed handler', () => {
  assert.match(frameSource, /onclick="reportErrorToAgent\(\)">Report to agent</)
  assert.doesNotMatch(frameSource, /onclick="reportError\(\)"/)
})

test('an uncaught React error is recorded and shown as a terminal error', () => {
  assert.match(
    frameSource,
    /createRoot\(document\.getElementById\('root'\), \{\s*onUncaughtError: handleUncaughtAppError,/,
  )
  const start = frameSource.indexOf('function handleUncaughtAppError(')
  const end = frameSource.indexOf('async function loadModule()', start)
  assert.ok(start >= 0 && end > start, 'the uncaught-error handler exists')
  assert.match(
    frameSource.slice(start, end),
    /reportAppError\(\s*'app:react-uncaught'[\s\S]*showErr\('Runtime error', detail\)/,
    'it records the error, then shows the panel',
  )
})
