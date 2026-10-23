/* A `mobius-app` fence in an assistant reply names an app destination and
   carries a conversation-authored snapshot. It has no authority of its own:
   opening it shows the app's ordinary view, which applies the app's checks to
   anything it offers to do. Everything here is pure validation, so a block that
   fails it stays an ordinary code block. */

import { safeSourceUrl } from '../messageSources.js'

// Units: every text bound is a JavaScript string length (UTF-16 code units).
// An intent is `kind:payload` where kind is 1-32 lowercase characters and the
// payload is 1-256 printable ASCII characters without whitespace, so for an
// intent characters, code units and bytes are the same count. The same bound
// applies to the root, its action and every item and item action. An app that
// needs to name a larger selection uses a short handle it resolves itself.
export const APP_BLOCK_LIMITS = Object.freeze({
  json: 16384, intentKind: 32, intentPayload: 256, title: 240,
  facts: 8, factLabel: 80, factValue: 240, itemFacts: 4,
  badges: 3, badgeLabel: 40, items: 12, actionLabel: 40, expandLabel: 40,
})
const INTENT = new RegExp(
  `^[a-z][a-z0-9-]{0,${APP_BLOCK_LIMITS.intentKind - 1}}:[\\x21-\\x7e]{1,${APP_BLOCK_LIMITS.intentPayload}}$`)
const APP_SLUG = /^[a-z][a-z0-9-]{0,63}$/
export const BADGE_TONES = new Set(['success', 'attention', 'danger', 'accent', 'neutral'])

export const appIntent = value => typeof value === 'string' && INTENT.test(value) ? value : null
export const shortText = (value, max) => typeof value === 'string' && value.trim() ? value.trim().slice(0, max) : ''
export const appBlockHref = (app, intent) => `/shell/?${new URLSearchParams({ app, intent })}`

// The first entry wins for a repeated key, so rendered rows stay distinct.
const firstBy = key => (item, index, list) => list.findIndex(other => key(other) === key(item)) === index

export function blockBadges(value) {
  return (Array.isArray(value) ? value : [])
    .filter(badge => shortText(badge?.label, APP_BLOCK_LIMITS.badgeLabel))
    .filter(firstBy(badge => shortText(badge.label, APP_BLOCK_LIMITS.badgeLabel))).slice(0, APP_BLOCK_LIMITS.badges)
    .map(badge => ({ label: shortText(badge.label, APP_BLOCK_LIMITS.badgeLabel),
      tone: BADGE_TONES.has(badge.tone) ? badge.tone : 'neutral' }))
}

function blockFacts(value, max) {
  // Filter before bounding, so an invalid fact never takes a valid one's slot.
  return (Array.isArray(value) ? value : [])
    .filter(fact => shortText(fact?.label, APP_BLOCK_LIMITS.factLabel) && typeof fact.value === 'string')
    .slice(0, max)
    .map(fact => {
      // An unsafe source link stays plain snapshot text.
      const href = safeSourceUrl(fact.href, { httpsOnly: true })
      return { label: shortText(fact.label, APP_BLOCK_LIMITS.factLabel),
        value: fact.value.slice(0, APP_BLOCK_LIMITS.factValue), ...(href ? { href } : {}) }
    })
}

function blockAction(raw) {
  const label = shortText(raw?.label, APP_BLOCK_LIMITS.actionLabel)
  const intent = appIntent(raw?.intent)
  return label && intent ? { label, intent } : null
}

// A title is identity, so it is validated like the root title: an over-limit
// title drops its item rather than being shortened.
const blockTitle = value => typeof value === 'string' && value.length <= APP_BLOCK_LIMITS.title ? value.trim() : ''

function blockItem(raw, app, inline) {
  const title = blockTitle(raw?.title)
  const intent = appIntent(raw?.intent)
  if (!title || !intent) return null
  return { title, intent, href: appBlockHref(app, intent),
    facts: blockFacts(raw.facts, APP_BLOCK_LIMITS.itemFacts), badges: blockBadges(raw.badges),
    action: inline ? blockAction(raw.action) : null }
}

export function appBlockFromToken(token) {
  if (token?.type !== 'code' || token.lang !== 'mobius-app'
    || typeof token.text !== 'string' || token.text.length > APP_BLOCK_LIMITS.json) return null
  let value
  try { value = JSON.parse(token.text) } catch { return null }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const app = APP_SLUG.test(value.app || '') ? value.app : null
  const intent = appIntent(value.intent)
  const title = blockTitle(value.title)
  if (!app || !intent || !title) return null
  const inline = value.inline !== false
  return {
    app, intent, title, inline, href: appBlockHref(app, intent),
    facts: blockFacts(value.facts, APP_BLOCK_LIMITS.facts),
    badges: blockBadges(value.badges),
    // Invalid items and actions are dropped; they never widen another field.
    items: (Array.isArray(value.items) ? value.items : [])
      .map(item => blockItem(item, app, inline)).filter(Boolean).filter(firstBy(item => item.intent))
      .slice(0, APP_BLOCK_LIMITS.items),
    // An action only opens the app's own view with its intent.
    action: inline ? blockAction(value.action) : null,
    expandLabel: shortText(value.expand_label, APP_BLOCK_LIMITS.expandLabel),
    height: Math.max(240, Math.min(640, Number(value.height) || 480)),
  }
}

/** A transcript is not a workspace navigation owner or a chat-control
 *  authority, so an embedded view may only open a conversation or an app. */
export function transcriptHostRequest(request, { navigate }) {
  if (request?.type === 'moebius:open-chat' && request.chatId) {
    navigate(`/shell/?${new URLSearchParams({ chat: request.chatId })}`)
  } else if (request?.type === 'moebius:open-app' && request.appId) {
    navigate(`/shell/?${new URLSearchParams({ app: request.appId, ...(request.intent ? { intent: request.intent } : {}) })}`)
  } else {
    throw new Error('Open the app to use workspace controls. This transcript view can only open conversations and apps.')
  }
}
