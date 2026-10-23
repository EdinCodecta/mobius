import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import AppBlock from '../markdown/AppBlock.jsx'
import { AppBlockView } from '../markdown/AppBlockParts.jsx'
import { appBlockFromToken } from '../markdown/appBlock.js'
import { appQueries } from '../../../hooks/queries.js'

const installed = [{ id: 80, slug: 'example', name: 'Example' }]

function render(value, apps = installed) {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify(value) })
  const client = new QueryClient()
  if (apps) client.setQueryData(appQueries.list.key, apps)
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(AppBlock, { block })))
}

const base = { app: 'example', intent: 'open:item-42', title: 'Item 42' }

test('a block renders its snapshot and app link without mounting the app', () => {
  const html = render({ ...base, facts: [{ label: 'Author', value: 'octocat' },
    { label: 'Source', value: 'owner/repo', href: 'https://github.com/owner/repo' }],
  badges: [{ label: 'Ready', tone: 'success' }] })
  assert.match(html, /<strong>Item 42<\/strong>/)
  assert.match(html, /<dt>Author<\/dt><dd>octocat<\/dd>/)
  assert.match(html, /<a href="https:\/\/github\.com\/owner\/repo" target="_blank" rel="noopener noreferrer">owner\/repo<\/a>/)
  assert.match(html, /md-app-block__badge is-success">Ready</)
  assert.match(html, /href="\/shell\/\?app=example&amp;intent=open%3Aitem-42"[^>]*>Open in Example/)
  assert.match(html, /aria-expanded="false"[^>]*>.*Show details here<\/button>/)
  assert.doesNotMatch(html, /md-app-block__view|<iframe/)
})

test('an action and the expand control only name views the reader can open', () => {
  const html = render({ ...base, expand_label: 'Review here', action: { label: 'Send', intent: 'send:item-42' } })
  assert.match(html, />Review here<\/button>/)
  assert.match(html, /class="md-app-block__action" aria-pressed="false">Send<\/button>/)
  assert.doesNotMatch(html, /md-app-block__view/)
})

test('link-only blocks have no expand control or action', () => {
  const html = render({ ...base, inline: false, action: { label: 'Send', intent: 'send:1' } })
  assert.doesNotMatch(html, /md-app-block__toggle|md-app-block__action/)
  assert.match(html, /Open in Example/)
})

test('items render as generic linked rows with their own badges, facts and actions', () => {
  const html = render({ ...base, title: 'Ready to contribute', action: { label: 'Contribute all', intent: 'send-batch:sel-1' },
    items: [
      { title: 'First change', intent: 'open:a', badges: [{ label: 'All clear', tone: 'success' }],
        facts: [{ label: 'Repository', value: 'owner/repo' }], action: { label: 'Contribute', intent: 'send:a' } },
      { title: 'Second change', intent: 'open:b' },
    ] })
  assert.match(html, /intent=open%3Aa"[^>]*>First change</)
  assert.match(html, /intent=open%3Ab"[^>]*>Second change</)
  assert.match(html, /is-success">All clear</)
  assert.equal((html.match(/class="md-app-block__action"/g) || []).length, 2)
  assert.match(html, />Contribute all<\/button>/)
  // The platform has no app-specific widget: no PR-row markup or label colours.
  assert.doesNotMatch(html, /md-app-pull|--md-label/)
})

test('every shape explains a missing or deleted app and offers no view', () => {
  const shapes = [base, { ...base, inline: false },
    { ...base, action: { label: 'Send', intent: 'send:1' } },
    { ...base, items: [{ title: 'Row', intent: 'open:1', action: { label: 'Go', intent: 'go:1' } }] }]
  for (const apps of [[], [{ ...installed[0], deleted_at: '2026-10-01T00:00:00Z' }]]) {
    for (const shape of shapes) {
      const html = render(shape, apps)
      assert.match(html, /example is not installed\. The saved snapshot remains here\./)
      assert.doesNotMatch(html, /md-app-block__toggle|md-app-block__action|md-app-block__view/)
    }
  }
  assert.match(render(base, null), /Checking installed apps…/)
})

test('the explicit view mounts the ordinary app canvas lazily', () => {
  const html = renderToStaticMarkup(createElement(AppBlockView, { app: installed[0], intent: 'open:1', height: 480, onHostRequest() {} }))
  assert.match(html, /class="md-app-block__view" style="height:480px"/)
  assert.match(html, /Opening Example…/)
})
