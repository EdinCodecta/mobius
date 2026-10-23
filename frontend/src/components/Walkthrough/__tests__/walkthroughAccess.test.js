import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { installReviewedApp, previewAppAccess } from '../walkthroughAccess.js'

const realFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = realFetch })

function stubFetch(status, body) {
  const calls = []
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), body: init?.body ? JSON.parse(init.body) : null })
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  }
  return calls
}

test('previewing_an_apps_access_only_reads_its_manifest', async () => {
  const calls = stubFetch(200, { capability_contract: { data: {} }, capability_digest: 'd1', access_rows: [{ id: 'manage_apps', title: 'Install and remove apps' }] })
  const preview = await previewAppAccess('https://example.test/mobius.json')
  assert.equal(preview.capability_digest, 'd1')
  assert.equal(preview.access_rows[0].title, 'Install and remove apps')
  assert.deepEqual(calls.map(call => call.url.replace(/^.*\/api/, '')), ['/apps/preview'])
  assert.deepEqual(calls[0].body, { manifest_url: 'https://example.test/mobius.json' })
})

test('installing_sends_exactly_the_digest_the_owner_reviewed', async () => {
  const calls = stubFetch(200, { id: 7 })
  assert.deepEqual(await installReviewedApp('https://example.test/mobius.json', 'd1'), { status: 'installed', warnings: [] })
  assert.deepEqual(calls[0].body, { manifest_url: 'https://example.test/mobius.json', reviewed_capability_digest: 'd1' })
})

test('an_app_whose_access_changed_after_review_comes_back_for_a_new_review', async () => {
  stubFetch(409, { detail: { code: 'capability_changed', capability_contract: { data: { manage_apps: true } }, capability_digest: 'd2', access_rows: [{ id: 'manage_apps', title: 'Install and remove apps' }] } })
  const result = await installReviewedApp('https://example.test/mobius.json', 'd1')
  assert.equal(result.status, 'changed')
  assert.equal(result.preview.capability_digest, 'd2')
  assert.deepEqual(result.preview.access_rows.map(row => row.id), ['manage_apps'])
})

test('a_failed_install_reports_the_servers_reason', async () => {
  stubFetch(400, { detail: 'The manifest is invalid.' })
  await assert.rejects(installReviewedApp('https://example.test/mobius.json', 'd1'), /manifest is invalid/)
})

test('install_warnings_are_passed_on_so_the_owner_can_see_them', async () => {
  stubFetch(200, { warnings: ['The app icon could not be fetched.'] })
  assert.deepEqual(await installReviewedApp('https://example.test/mobius.json', 'd1'), { status: 'installed', warnings: ['The app icon could not be fetched.'] })
})
