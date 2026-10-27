/** Render the real Settings update view against isolated recovery snapshots. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { PlatformUpdatesView } = await vite.ssrLoadModule('/src/components/SettingsView/PlatformUpdates.jsx')
after(() => vite.close())

const target = 'c'.repeat(40)

for (const state of ['finish', 'settling', 'conflict', 'restart']) {
  test(`unresolved recovery owns Settings actions while source is ${state}`, () => {
    const platform = {
      state: state === 'conflict' ? 'conflict' : 'ready',
      contained_upstream_sha: target,
      activation: state === 'restart'
        ? { level: 'server_restart', required_actions: ['server_restart'] }
        : { level: 'image_rebuild', required_actions: ['image_rebuild'] },
      unfinished_update: { stage: state === 'restart' ? 'finish' : state,
        action: state === 'restart' ? 'restart' : 'replace', cancellable: true },
      conflict_chat_id: state === 'conflict' ? 'some-chat' : null,
    }
    const update = {
      platform, cachedPlatform: null,
      rebuild: { state: 'needs_recovery', expected_sha: target, request_nonce: 'e'.repeat(32) },
      version: null, phase: 'idle', busy: false, reconnecting: false,
      error: '', errorCode: '', checkResult: '',
    }
    const html = renderToStaticMarkup(createElement(PlatformUpdatesView, {
      update, onOpenChat: () => {},
    }))
    assert.match(html, /The replacement needs recovery before another update can start/)
    assert.match(html, /The previous replacement still needs recovery in your deployment/)
    assert.match(html, /<button[^>]*>Ask Möbius<\/button>/)
    assert.match(html, /<button[^>]*>Check recovery status<\/button>/)
    assert.match(html, /<button class="settings__btn settings__btn--outline settings__btn--sm">Restart<\/button>/)
    assert.doesNotMatch(html, />(?:Finish update|Finish in chat|Review update|Cancel update|Keep this version|Withdraw request|Updating…|Checking…)</)
    assert.doesNotMatch(html, /The update is still running|Confirming the new container/)
    assert.doesNotMatch(html, /Your changes are ready|Restart to finish/)
  })
}

test('the recovery status control is disabled while checking', () => {
  const html = renderToStaticMarkup(createElement(PlatformUpdatesView, {
    update: {
      platform: { state: 'ready', contained_upstream_sha: target },
      rebuild: { state: 'needs_recovery', expected_sha: target },
      phase: 'checking', busy: true, reconnecting: false,
    },
  }))
  assert.match(html, /<button[^>]*disabled=""[^>]*>Checking recovery status…<\/button>/)
  assert.doesNotMatch(html, />(?:Review update|Cancel update|Keep this version|Withdraw request|Updating…)</)
})

test('a stalled update offers Resume and Retry beside Cancel', () => {
  const platform = {
    state: 'conflict', contained_upstream_sha: target, conflict_chat_id: 'resolver',
    unfinished_update: { stage: 'resolve', action: 'replace', cancellable: true, stalled: {
      reason: 'resolver_idle', idle_hours: 7, actions: ['resume', 'retry', 'cancel'],
      resolver_chat_id: 'resolver', remaining_paths: ['backend/app/main.py'],
    } },
  }
  const update = {
    platform, cachedPlatform: null, rebuild: null, version: null, phase: 'idle',
    busy: false, reconnecting: false, error: '', errorCode: '', checkResult: '',
  }
  const html = renderToStaticMarkup(createElement(PlatformUpdatesView, { update, onOpenChat: () => {} }))
  assert.match(html, /Update paused for 7 h: resolver idle\./)
  assert.match(html, /<button[^>]*>Resume<\/button>/)
  assert.match(html, /<button[^>]*>Retry<\/button>/)
  assert.match(html, /<button[^>]*>Cancel update<\/button>/)

  const moving = { ...platform, unfinished_update: { ...platform.unfinished_update, stalled: undefined } }
  const quiet = renderToStaticMarkup(createElement(PlatformUpdatesView, {
    update: { ...update, platform: moving }, onOpenChat: () => {},
  }))
  assert.doesNotMatch(quiet, /Update paused|>Resume<|>Retry</)
})

test('Settings warns when the container installs older packages than the source declares', () => {
  const platform = {
    state: 'up_to_date', contained_upstream_sha: target,
    restart_blocker: {
      origin: 'official_newer', blocking: false, fix: { action: 'replace_container', target_sha: target },
      packages: [{ name: 'pillow', declared: '==13.0', installed: '12.3', kind: 'version', origin: 'official_newer' }],
    },
  }
  const update = {
    platform, cachedPlatform: null, rebuild: null, version: null, phase: 'idle',
    busy: false, reconnecting: false, error: '', errorCode: '', checkResult: '',
  }
  const html = renderToStaticMarkup(createElement(PlatformUpdatesView, { update, onOpenChat: () => {} }))
  assert.match(html, /Container packages are behind the platform source/)
  assert.match(html, /pillow \(source declares ==13\.0, container has 12\.3\)/)
  assert.match(html, /A container update \(Finish update\) installs them\./)

  const current = renderToStaticMarkup(createElement(PlatformUpdatesView, {
    update: { ...update, platform: { ...platform, restart_blocker: null } }, onOpenChat: () => {},
  }))
  assert.doesNotMatch(current, /packages are behind/)
})
