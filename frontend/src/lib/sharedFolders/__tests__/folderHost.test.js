import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { handleSharedFolderCommand } from '../folderHost.js'
import { createHandleFolder } from '../handleFolder.js'
import { hostStore } from '../hostState.js'
import { FakeDirectoryHandle } from './fakeDirectory.js'

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
