/* Mounted markup contracts for quiet handoffs, source context and recovery. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement as h } from 'react'
import { renderToStaticMarkup as render } from 'react-dom/server'
import { createServer } from 'vite'
const previousWindow = globalThis.window
globalThis.window = { location: { href: 'http://localhost/' } }
after(() => { globalThis.window = previousWindow })
const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
after(() => vite.close())
const load = async path => (await vite.ssrLoadModule('/src/components/' + path + '.jsx')).default
const Marker = await load('Drawer/DrawerStatusMarker')
const Backlinks = await load('ChatView/DependencyBacklinks')
const Details = await load('ChatView/GoalPlanDetails')
const Recovery = await load('ChatView/ErrorCard')
const History = await load('ChatView/WaitHistoryCard')
const Question = await load('ChatView/QuestionCard')
const goal = { chat_id: 'dependent', title: 'Finish blocked update' }

test('hold has its own square shape and name without changing marker precedence', () => {
  const waiting = render(h(Marker, { waiting: true }))
  const hold = render(h(Marker, { onHold: true }))
  assert.match(hold, /drawer__hold-icon/)
  assert.match(hold, /aria-label="On hold — continue when ready"/)
  assert.notEqual(hold.match(/<svg.*<\/svg>/)?.[0], waiting.match(/<svg.*<\/svg>/)?.[0])
  assert.match(waiting, /Waiting to resume/)
  assert.match(render(h(Marker, { ownerRequired: true, streaming: true, waiting: true, onHold: true })), /drawer__owner-input-dot/)
  assert.match(render(h(Marker, { streaming: true, waiting: true, onHold: true })), /drawer__streaming-dot/)
  assert.match(render(h(Marker, { waiting: true, onHold: true })), /drawer__waiting-icon/)
  assert.match(render(h(Marker, { onHold: true, recovery: true, failed: true, attentionDot: true })), /drawer__hold-icon/)
  assert.match(render(h(Marker, { recovery: true, recoveryLabel: 'Manual recovery needed', failed: true })), /drawer__recovery-icon/ )
  const failed = render(h(Marker, { failed: true, attentionDot: true }))
  assert.match(failed, /drawer__failure-dot/)
  assert.match(failed, /aria-label="Latest run failed"/)
  assert.match(render(h(Marker, { attentionDot: true })), /drawer__attention-dot/)
})

test('single and multiple backlinks are quiet links, not requests or status announcements', () => {
  assert.equal(render(h(Backlinks, { chats: [] })), '')
  const single = render(h(Backlinks, { chats: [goal] }))
  assert.match(single, /Also needed by <a href="\/shell\/\?chat=dependent">Finish blocked update/)
  assert.doesNotMatch(single, /role="(?:alert|status)"|<button/)
  const many = render(h(Backlinks, { chats: [goal, { chat_id: 'b', title: 'Prepare launch' }] }))
  assert.match(many, /<details/)
  assert.match(many, /Also needed by 2 chats/)
  assert.match(many, /Prepare launch/)
  assert.match(render(h(Details, { neededBy: [goal] })), /Also needed by/)
  assert.match(render(h(Question, { chatId: 'source', questionId: 'choice', questions: [{ question: 'Which release?' }], neededBy: [goal] })), /<details class="qcard__related-work"><summary>Related work/)
})

test('full dependency condition precedes secondary source, with no invented checks', () => {
  const condition = 'Choose the exact release to install after preserving the local work and checking the host'
  const dependency = { id: 'd', target_kind: 'owner_card', condition, source_chat_id: 'source', source_question_id: 'choice', source_chat_title: 'Repair', automatic: true }
  const html = render(h(Details, { dependencies: [dependency] }))
  assert.ok(html.indexOf(condition) < html.indexOf('Decision in Repair'))
  assert.match(html, /Continues automatically when eligible/)
  assert.doesNotMatch(html, /next check|deadline|Möbius checks|<input/)
  for (const blocker of ['goal_held', 'source_held', 'source_failed']) {
    const held = render(h(Details, { dependencies: [{ ...dependency, automatic: false, resume_blocker: blocker }] }))
    assert.doesNotMatch(held, /Continues automatically/)
  }
})

test('settlement code is calm recovery while unrelated resumable failures stay alerts', () => {
  const html = render(h(Recovery, { block: { code: 'goal_settlement_unfinished', message: 'old automatic settlement internals', resumable: true } }, h('button', {}, 'Resume')))
  assert.match(html, /chat__text--parked/)
  assert.match(html, /Work interrupted/)
  assert.match(html, /Your progress is saved. This Goal is unfinished. Resume to continue./)
  assert.match(html, />Resume<\/button>/)
  assert.doesNotMatch(html, /role="alert"|automatic settlement internals/)
  const failed = render(h(Recovery, { block: { code: 'execution_failed', message: 'Permission denied', resumable: true } }))
  assert.match(failed, /role="alert"/)
  assert.match(failed, /Permission denied/)
  assert.doesNotMatch(failed, /chat__text--parked/)
})

for (const [kind, label] of [['completed', 'Work completed'], ['released', 'Work released'], ['owner_deleted', 'Work owner deleted'], ['answered', 'Answered'], ['closed_without_reply', 'Answered without a source reply'], ['superseded', 'Replaced'], ['cancelled', 'Cancelled by Stop'], ['source_deleted', 'Source chat deleted']]) {
  test(`settled ${kind} retains clock, typed outcome and expandable full condition`, () => {
    const html = render(h(History, { summary: { id: 'd', target_kind: ['completed', 'released', 'owner_deleted'].includes(kind) ? 'claim_acquisition' : 'owner_card', condition: 'Host repair', disposition: 'delivered', result_disposition: 'delivered', source_chat_title: 'Repair', outcome: { kind } } }))
    assert.match(html, /chat__wait-history/)
    assert.match(html, /chat__lifecycle-icon/)
    assert.match(html, /<details/)
    assert.ok(html.includes(label))
    assert.doesNotMatch(html, /Checks finished|next check|deadline|Continues automatically|chat__lifecycle-outcome/)
  })
}
