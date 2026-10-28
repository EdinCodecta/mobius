import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { appQueries } from '../../hooks/queries.js'
import UninstalledApps from '../../components/Drawer/UninstalledApps.jsx'
import { expiryCountdown, recoverErrorMessage } from '../../components/Drawer/uninstalledApps.js'

const NOW = Date.parse('2026-10-27T12:00:00Z')

const at = offset => new Date(NOW + offset).toISOString()
const SEC = 1000
const MIN = 60 * SEC
const HOUR = 60 * MIN
const DAY = 24 * HOUR
const label = offset => expiryCountdown(at(offset), NOW)?.label

test('the countdown shows whole days, then rough steps on the last day, and is gone at zero', () => {
  assert.equal(label(7 * DAY), '7 days left')
  assert.equal(label(DAY + 5 * HOUR), '1 day left')
  assert.equal(label(DAY), '1 day left')
  assert.equal(label(DAY - 1), '~1 day left')
  assert.equal(label(12 * HOUR + 1), '~1 day left')
  assert.equal(label(12 * HOUR), '~12 hrs left')
  assert.equal(label(6 * HOUR + 1), '~12 hrs left')
  assert.equal(label(6 * HOUR), '~6 hrs left')
  assert.equal(label(HOUR + 1), '~6 hrs left')
  assert.equal(label(HOUR), '~1 hr left')
  assert.equal(label(SEC), '~1 hr left')
  assert.equal(label(1), '~1 hr left')
  assert.equal(label(0), undefined)
  assert.equal(label(-HOUR), undefined)
  assert.equal(expiryCountdown('nonsense', NOW), null)
})

test('the pill turns amber under three days and red on the last day', () => {
  assert.equal(expiryCountdown(at(5 * DAY), NOW).tone, '')
  assert.equal(expiryCountdown(at(2 * DAY), NOW).tone, 'soon')
  assert.equal(expiryCountdown(at(8 * HOUR), NOW).tone, 'urgent')
})

test('the next wake lands exactly when the label changes', () => {
  for (const offset of [6 * DAY + 3 * HOUR, DAY, DAY - 1, 20 * HOUR, 12 * HOUR, 8 * HOUR, 6 * HOUR, 5 * HOUR + 7 * MIN, HOUR, 42 * MIN, 400]) {
    const { label: before, changesAt } = expiryCountdown(at(offset), NOW)
    const nextChangeIn = changesAt - NOW
    assert.ok(nextChangeIn > 0)
    assert.notEqual(label(offset - nextChangeIn), before)
    assert.ok(nextChangeIn <= offset)
    assert.equal(label(offset - nextChangeIn + 1), before)
  }
})

test('a failed recover shows the server reason, or a generic line', async () => {
  const reason = await recoverErrorMessage(new Response(JSON.stringify({ detail: 'Recovery window has expired.' }), { status: 410 }))
  assert.equal(reason, 'Recovery window has expired.')
  const generic = await recoverErrorMessage(new Response('boom', { status: 500 }))
  assert.equal(generic, 'Could not recover this app. Try again.')
})

function render(rows) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(appQueries.uninstalled.key, rows)
  return renderToStaticMarkup(
    React.createElement(QueryClientProvider, { client }, React.createElement(UninstalledApps)),
  )
}

test('the section lists each uninstalled app with a Recover button, and is absent when there are none', () => {
  assert.equal(render([]), '')
  const html = render([
    { id: 7, name: 'News', slug: 'news', version: '1.2.0', icon_data_url: null, background_color: '#336699', theme_color: null, deleted_at: '2026-10-26T12:00:00Z', expires_at: '2026-11-02T12:00:00Z' },
    { id: 9, name: 'Kanban', slug: 'kanban', version: null, deleted_at: '2026-10-27T08:00:00Z', expires_at: '2026-11-03T08:00:00Z' },
  ])
  assert.match(html, /Recently uninstalled/)
  assert.match(html, /News/)
  assert.match(html, /Kanban/)
  assert.match(html, /aria-label="Recover News"/)
  assert.match(html, /aria-label="Recover Kanban"/)
  assert.match(html, /recover an uninstalled app, with its data, for 7 days/)
  assert.doesNotMatch(html, /permission/)
  assert.doesNotMatch(html, /for good/)
})

test('an app whose countdown already reached zero is not listed', () => {
  const past = new Date(Date.now() - 1000).toISOString()
  assert.equal(render([{ id: 3, name: 'Gone', slug: 'gone', deleted_at: '2026-10-20T12:00:00Z', expires_at: past }]), '')
  const html = render([
    { id: 3, name: 'Gone', slug: 'gone', deleted_at: '2026-10-20T12:00:00Z', expires_at: past },
    { id: 4, name: 'Kept', slug: 'kept', deleted_at: new Date().toISOString(), expires_at: new Date(Date.now() + 2 * 86400000).toISOString() },
  ])
  assert.doesNotMatch(html, /Gone/)
  assert.match(html, /Kept/)
  assert.match(html, /1 day left|2 days left/)
  assert.match(html, /uninstalled-apps__count">1</)
})
