// Settings › Keys gives each pending app request its own answer form.
//
// A request id is the app id and key name, so a request that replaces another
// (an app that reused a removed app's id) arrives under the same id with a new
// ticket. Its form must start empty: otherwise React keeps the value typed for
// the old request and Allow sends it with the replacement's ticket.
//
// The frontend unit suite deliberately has no DOM renderer (no jsdom), so this
// checks the form identity KeysSection keys each request card by. The
// behaviour itself was verified with the real KeysSection and KeyRequest in a
// local JSDOM harness: before the fix the old draft stayed in the field and
// Allow sent it with the new ticket; after it the field is empty, Allow is
// disabled, and nothing is sent.
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { requestFormKey } = await vite.ssrLoadModule('/src/components/SettingsView/KeysSection.jsx')
after(() => vite.close())

const request = ticket => ({ id: '5-weather_api', app: 5, key: 'weather_api', status: 'pending', ticket })

test('a request that replaces another under the same id gets a new form', () => {
  assert.notEqual(requestFormKey(request('first-ticket')), requestFormKey(request('second-ticket')))
})

test('the same request keeps its form across refreshes', () => {
  assert.equal(requestFormKey(request('first-ticket')), requestFormKey({ ...request('first-ticket') }))
})

test('requests for different keys never share a form', () => {
  assert.notEqual(
    requestFormKey({ ...request('t'), id: '5-weather_api' }),
    requestFormKey({ ...request('t'), id: '5-maps_api' }),
  )
})
