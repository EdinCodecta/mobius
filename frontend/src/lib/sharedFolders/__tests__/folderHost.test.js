import { test, beforeEach, mock } from 'node:test'
import assert from 'node:assert/strict'
import { handleSharedFolderCommand } from '../folderHost.js'
import { createHandleFolder } from '../handleFolder.js'
import { hostStore } from '../hostState.js'
import { FakeDirectoryHandle, fileText } from './fakeDirectory.js'

let answers = []

beforeEach(() => {
  answers = []
  globalThis.fetch = async (url, options) => {
    if (String(url).endsWith('/api/shared-folders/responses')) answers.push(JSON.parse(options.body))
    return new Response('{}', { status: 200 })
  }
  const store = hostStore()
  const root = new FakeDirectoryHandle('Downloads')
  root.addFile('a.txt', 'hello')
  store.records = new Map([
    ['edit', { id: 'edit', name: 'Downloads', mode: 'edit', access: 'remembered', paused: false }],
    ['read', { id: 'read', name: 'Docs', mode: 'read', access: 'remembered', paused: false }],
    ['paused', { id: 'paused', name: 'Old', mode: 'edit', access: 'remembered', paused: true }],
  ])
  store.access = new Map([['edit', createHandleFolder(root)], ['read', createHandleFolder(root)], ['paused', createHandleFolder(root)]])
  store.permissions = new Map([['edit', 'granted'], ['read', 'granted'], ['paused', 'granted']])
  store.subscriptionId = 'stream-1'
})

function command(folderId, op, extra = {}) {
  return { type: 'shared_folder_command', commandId: `c-${op}`, folderId, op, path: 'a.txt', ...extra }
}

test('an agent command is answered on the stream that delivered it', async () => {
  await handleSharedFolderCommand(command('edit', 'read', { length: 5 }))
  assert.equal(answers.length, 1)
  assert.equal(answers[0].subscriptionId, 'stream-1')
  assert.equal(answers[0].commandId, 'c-read')
  assert.equal(answers[0].ok, true)
  assert.equal(answers[0].result.text, 'hello')
  assert.equal(hostStore().state.activity.folderName, 'Downloads')
})

test('the browser itself refuses changes to a Read only folder', async () => {
  await handleSharedFolderCommand(command('read', 'bin'))
  assert.deepEqual([answers[0].ok, answers[0].code], [false, 'read_only'])
})

test('a paused folder refuses every command until the owner resumes it', async () => {
  await handleSharedFolderCommand(command('paused', 'list', { path: '' }))
  assert.deepEqual([answers[0].ok, answers[0].code], [false, 'paused'])
})

test('a folder this browser no longer shares answers not found', async () => {
  await handleSharedFolderCommand(command('gone', 'list', { path: '' }))
  assert.deepEqual([answers[0].ok, answers[0].code], [false, 'not_found'])
})

function editFolder(root) {
  hostStore().access.set('edit', createHandleFolder(root))
}

test('two changes at the same moment cannot both claim one new name', async () => {
  const root = new FakeDirectoryHandle('Downloads')
  editFolder(root)
  await Promise.all(['first', 'second'].map(text => handleSharedFolderCommand(
    command('edit', 'write', { commandId: `c-${text}`, path: 'new.txt', content: text }),
  )))
  assert.deepEqual(answers.map(answer => answer.ok ? 'ok' : answer.code).sort(), ['exists', 'ok'])
  assert.equal(await fileText(root, 'new.txt'), 'first')
})

const withdrawals = {
  paused: store => { store.records.get('edit').paused = true },
  not_found: store => { store.records.delete('edit'); store.access.delete('edit') },
  read_only: store => { store.records.get('edit').mode = 'read' },
}

for (const [code, withdraw] of Object.entries(withdrawals)) {
  test(`a change waiting its turn is refused if the owner withdraws rights meanwhile (${code})`, async () => {
    const root = new FakeDirectoryHandle('Downloads')
    editFolder(root)
    let releaseLock
    const holding = new Promise(resolve => { releaseLock = resolve })
    let lockHeld
    const held = new Promise(resolve => { lockHeld = resolve })
    const earlier = navigator.locks.request('mobius-shared-folder:edit', () => { lockHeld(); return holding })
    await held
    const waiting = handleSharedFolderCommand(command('edit', 'write', { path: 'late.txt', content: 'late' }))
    withdraw(hostStore())
    releaseLock()
    await Promise.all([earlier, waiting])
    assert.deepEqual([answers[0].ok, answers[0].code], [false, code])
    assert.equal(root.children.has('late.txt'), false)
  })
}

test('binning two same-named files at once keeps both in the bin', async () => {
  const root = new FakeDirectoryHandle('Downloads')
  root.addFolder('a').addFile('x.txt', 'from a')
  root.addFolder('b').addFile('x.txt', 'from b')
  editFolder(root)
  await Promise.all(['a', 'b'].map(folder => handleSharedFolderCommand(
    command('edit', 'bin', { commandId: `c-bin-${folder}`, path: `${folder}/x.txt` }),
  )))
  const bin = root.children.get('Möbius bin')
  assert.deepEqual([...bin.children.keys()].sort(), ['x (2).txt', 'x.txt'])
})

test('the activity pill stays while a command runs and leaves a few seconds after', async () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    let finish
    hostStore().access.set('edit', { run: () => new Promise(resolve => { finish = resolve }) })
    const running = handleSharedFolderCommand(command('edit', 'move', { to: 'b.txt' }))
    mock.timers.tick(60_000)
    assert.equal(hostStore().state.activity?.op, 'move')
    finish({ path: 'a.txt', to: 'b.txt' })
    await running
    assert.equal(hostStore().state.activity?.op, 'move')
    mock.timers.tick(6_000)
    assert.equal(hostStore().state.activity, null)
  } finally {
    mock.timers.reset()
  }
})
