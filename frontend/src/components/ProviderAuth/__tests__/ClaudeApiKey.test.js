/* Claude offers "sign in" or "use an API key"; the key posts straight to the
   backend from an owner-typed password field. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createServer } from 'vite'

const vite = await createServer({
  appType: 'custom', logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { default: ProviderAuth } = await vite.ssrLoadModule('/src/components/ProviderAuth/ProviderAuth.jsx')
const { default: ClaudeApiKeyForm, saveClaudeApiKey } = await vite.ssrLoadModule('/src/components/ProviderAuth/ClaudeApiKeyForm.jsx')
const { creditsCopy } = await vite.ssrLoadModule('/src/components/ChatView/ErrorCard.jsx')
after(() => vite.close())

function render(component, props) {
  const queryClient = new QueryClient()
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient },
    createElement(component, props)))
  queryClient.clear()
  return html
}

function fakeClient(response) {
  const calls = []
  return {
    calls,
    auth: { provider: { claude: { saveApiKey: async (key) => { calls.push(key); return response() } } } },
  }
}

test('an unconnected Claude offers both sign-in and an API key', () => {
  const html = render(ProviderAuth, { authenticated: false, compact: true })
  assert.match(html, />Sign in with Claude</)
  assert.match(html, />Use an API key</)
  assert.match(html, /billed per use/)
})

test('the key field is a labelled password input that browsers do not autofill', () => {
  const html = render(ClaudeApiKeyForm, {})
  assert.match(html, /type="password"/)
  assert.match(html, /autoComplete="off"|autocomplete="off"/)
  assert.match(html, /<span class="pa__input-label">Anthropic API key<\/span>/)
  assert.match(html, /console\.anthropic\.com\/settings\/keys/)
})

test('a key added beside a sign-in explains that the sign-in keeps precedence', () => {
  assert.match(render(ClaudeApiKeyForm, { subscriptionActive: true }), /takes precedence, even if it stops working/)
  assert.doesNotMatch(render(ClaudeApiKeyForm, {}), /used only without it/)
})

test('a connected API key names its method and can be replaced or removed', () => {
  const html = render(ProviderAuth, {
    authenticated: true, compact: true, authMethod: 'api_key', apiKeySaved: true,
  })
  assert.match(html, /with an Anthropic API key/)
  assert.match(html, />Replace API key</)
  assert.match(html, />Remove API key</)
  assert.match(html, />Sign in with Claude</)
})

test('a sign-in with a standby key says the key is only a fallback', () => {
  const html = render(ProviderAuth, {
    authenticated: true, compact: true, authMethod: 'subscription', apiKeySaved: true,
  })
  assert.match(html, /with your Claude sign-in/)
  assert.match(html, /takes precedence while it is connected/)
  assert.match(html, />Remove API key</)
})

test('saving trims the pasted key and reports success only on a 2xx', async () => {
  const client = fakeClient(() => ({ ok: true, json: async () => ({ ok: true }) }))
  assert.deepEqual(await saveClaudeApiKey(client, '  sk-ant-api03-key \n'), { ok: true })
  assert.deepEqual(client.calls, ['sk-ant-api03-key'])
})

test('a refused key shows the backend reason and an empty field sends nothing', async () => {
  const refused = fakeClient(() => ({ ok: false, json: async () => ({ detail: 'Anthropic rejected this API key.' }) }))
  assert.deepEqual(await saveClaudeApiKey(refused, 'sk-ant-api03-key'), {
    ok: false, error: 'Anthropic rejected this API key.',
  })
  const empty = fakeClient(() => { throw new Error('must not send') })
  const outcome = await saveClaudeApiKey(empty, '   ')
  assert.equal(outcome.ok, false)
  assert.deepEqual(empty.calls, [])
})

test('a network failure is reported without claiming the key was saved', async () => {
  const offline = { auth: { provider: { claude: { saveApiKey: async () => { throw new TypeError('offline') } } } } }
  assert.deepEqual(await saveClaudeApiKey(offline, 'sk-ant-api03-key'), { ok: false, error: 'Network error.' })
})

test('an Anthropic credits pause points at Console billing, Codex keeps workspace copy', () => {
  assert.match(creditsCopy('claude'), /Claude Console/)
  assert.match(creditsCopy('claude'), /then Resume/)
  assert.match(creditsCopy('codex'), /workspace is out of credits/)
  assert.match(creditsCopy(undefined), /workspace is out of credits/)
})
