import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { appQueries } from '../../hooks/queries.js'
import UninstalledApps from '../../components/Drawer/UninstalledApps.jsx'
import { expiryLabel, recoverErrorMessage } from '../../components/Drawer/uninstalledApps.js'

const NOW = Date.parse('2026-10-27T12:00:00Z')

test('the expiry says how long is left, in plain days', () => {
  assert.equal(expiryLabel('2026-11-03T12:00:00Z', NOW), 'Deleted for good in 7 days')
  assert.equal(expiryLabel('2026-10-28T13:00:00Z', NOW), 'Deleted for good in 1 day')
  assert.equal(expiryLabel('2026-10-27T20:00:00Z', NOW), 'Deleted for good in under a day')
  assert.equal(expiryLabel('2026-10-27T11:00:00Z', NOW), 'Deleted for good soon')
  assert.equal(expiryLabel('nonsense', NOW), 'Deleted for good soon')
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
    { id: 7, name: 'News', slug: 'news', version: '1.2.0', deleted_at: '2026-10-26T12:00:00Z', expires_at: '2026-11-02T12:00:00Z' },
    { id: 9, name: 'Kanban', slug: 'kanban', version: null, deleted_at: '2026-10-27T08:00:00Z', expires_at: '2026-11-03T08:00:00Z' },
  ])
  assert.match(html, /Recently uninstalled/)
  assert.match(html, /News/)
  assert.match(html, /Kanban/)
  assert.match(html, /aria-label="Recover News"/)
  assert.match(html, /aria-label="Recover Kanban"/)
  assert.match(html, /Kept for 7 days/)
})
