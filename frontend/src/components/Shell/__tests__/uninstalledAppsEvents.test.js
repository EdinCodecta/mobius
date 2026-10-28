import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')

// The recovery list lives under its own query key, so the apps-list refresh does not reach it. Every
// uninstall and recovery reaches the shell as one of these two events, whoever caused it (this tab, an
// agent, another browser or a notification), so they are the one place the list is refreshed from.
function branch(from, to) {
  const start = shell.indexOf(from)
  assert.ok(start >= 0, `${from} branch exists`)
  return shell.slice(start, shell.indexOf(to, start))
}

test('an app_deleted event refreshes the recently uninstalled list', () => {
  assert.match(branch("ev.type === 'app_deleted'", "ev.type === 'app_recovered'"), /appQueries\.uninstalled\.invalidate\(queryClient\)/)
})

test('an app_recovered event refreshes the recently uninstalled list', () => {
  assert.match(branch("ev.type === 'app_recovered'", "ev.type === 'app_updated'"), /appQueries\.uninstalled\.invalidate\(queryClient\)/)
})

test('the local uninstall handler leaves that refresh to the event, so there is one owner', () => {
  const handler = shell.slice(shell.indexOf('async function deleteApp'), shell.indexOf('// Wipes an app'))
  assert.doesNotMatch(handler, /uninstalled\.invalidate/)
})
