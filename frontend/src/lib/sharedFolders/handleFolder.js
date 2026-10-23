/* Commands on a folder Chrome or Edge remembers, through its File System Access handle. */
import {
  BIN_FOLDER_NAME, FolderError, joinPath, numberedName, pathSegments,
} from './folderPaths.js'
import {
  DEFAULT_IMAGE_EDGE, MAX_READ_BYTES, base64ToBytes, fileInfo, pageOf, readFileSlice, shrinkImage, sortEntries,
} from './fileReads.js'

const MAX_WRITE_BYTES = MAX_READ_BYTES
const ERROR_CODES = {
  NotFoundError: 'not_found',
  TypeMismatchError: 'wrong_kind',
  NotAllowedError: 'needs_permission',
  SecurityError: 'needs_permission',
  NoModificationAllowedError: 'locked',
  InvalidModificationError: 'locked',
  QuotaExceededError: 'no_space',
}
const ERROR_MESSAGES = {
  not_found: path => `Nothing is at "${path}" in this folder.`,
  wrong_kind: path => `"${path}" is a folder where a file was expected, or the other way round.`,
  needs_permission: () => 'The browser needs the owner to tap Reconnect for this folder in Settings → Shared folders.',
  locked: path => `"${path}" is in use or locked on the device.`,
  no_space: () => 'The device is out of space.',
}

/** Browser file errors as stable codes the agent can act on. */
export function asFolderError(error, path = '') {
  if (error instanceof FolderError) return error
  const code = ERROR_CODES[error?.name] || 'device_error'
  const message = ERROR_MESSAGES[code]?.(path) || String(error?.message || error).slice(0, 300)
  return new FolderError(code, message)
}

async function directoryAt(root, segments) {
  let directory = root
  for (const name of segments) directory = await directory.getDirectoryHandle(name)
  return directory
}

async function entryAt(root, segments) {
  if (!segments.length) return { kind: 'folder', handle: root, parent: null, name: root.name }
  const parent = await directoryAt(root, segments.slice(0, -1))
  const name = segments.at(-1)
  try {
    return { kind: 'file', handle: await parent.getFileHandle(name), parent, name }
  } catch (error) {
    if (error?.name !== 'TypeMismatchError') throw error
  }
  return { kind: 'folder', handle: await parent.getDirectoryHandle(name), parent, name }
}

async function exists(directory, name) {
  try {
    await directory.getFileHandle(name)
    return true
  } catch (error) {
    if (error?.name === 'TypeMismatchError') return true
    if (error?.name === 'NotFoundError') return false
    throw error
  }
}

async function describeEntry({ name, kind, handle }) {
  if (kind === 'folder') return { name, kind }
  const file = await handle.getFile()
  return { name, kind, size: file.size, modified: file.lastModified || null }
}

async function list(root, command) {
  const segments = pathSegments(command.path, { allowRoot: true })
  const directory = await directoryAt(root, segments)
  const all = []
  for await (const [name, handle] of directory.entries()) {
    all.push({ name, kind: handle.kind === 'directory' ? 'folder' : 'file', handle })
  }
  const { page, total, nextCursor } = pageOf(sortEntries(all), command)
  return { path: joinPath(segments), entries: await Promise.all(page.map(describeEntry)), total, nextCursor }
}

async function info(root, command) {
  const segments = pathSegments(command.path, { allowRoot: true })
  const entry = await entryAt(root, segments)
  if (entry.kind === 'file') return fileInfo(await entry.handle.getFile(), joinPath(segments))
  let entries = 0
  for await (const _ of entry.handle.keys()) entries += 1
  return { path: joinPath(segments), name: entry.name, kind: 'folder', entries }
}

async function read(root, command) {
  const segments = pathSegments(command.path)
  const entry = await entryAt(root, segments)
  if (entry.kind !== 'file') throw new FolderError('wrong_kind', `"${command.path}" is a folder. Use list.`)
  return readFileSlice(await entry.handle.getFile(), joinPath(segments), command)
}

async function image(root, command) {
  const segments = pathSegments(command.path)
  const entry = await entryAt(root, segments)
  if (entry.kind !== 'file') throw new FolderError('wrong_kind', `"${command.path}" is a folder. Use list.`)
  return shrinkImage(await entry.handle.getFile(), joinPath(segments), command.maxEdge || DEFAULT_IMAGE_EDGE)
}

async function write(root, command) {
  const segments = pathSegments(command.path)
  const parent = await directoryAt(root, segments.slice(0, -1))
  const name = segments.at(-1)
  const existed = await exists(parent, name)
  if (existed && !command.overwrite) {
    throw new FolderError('exists', `"${joinPath(segments)}" already exists. Use --overwrite to replace it.`)
  }
  const bytes = command.encoding === 'base64'
    ? base64ToBytes(command.content)
    : new TextEncoder().encode(command.content)
  if (bytes.length > MAX_WRITE_BYTES) throw new FolderError('too_large', 'Write at most 4 MB at a time.')
  const writable = await (await parent.getFileHandle(name, { create: true })).createWritable()
  await writable.write(bytes)
  await writable.close()
  return { path: joinPath(segments), size: bytes.length, created: !existed }
}

async function copyThenRemove(source, targetParent, targetName) {
  const copy = await targetParent.getFileHandle(targetName, { create: true })
  try {
    await (await source.handle.getFile()).stream().pipeTo(await copy.createWritable())
  } catch (error) {
    await targetParent.removeEntry(targetName).catch(() => {})
    throw error
  }
  // Only the original of a completed copy is removed: this finishes a move.
  await source.parent.removeEntry(source.name)
}

async function moveFile(source, targetParent, targetName) {
  if (typeof source.handle.move === 'function') {
    try {
      await source.handle.move(targetParent, targetName)
      return
    } catch (error) {
      if (['NotFoundError', 'QuotaExceededError'].includes(error?.name)) throw error
      // Some browsers move only private-storage files; copy and remove instead.
    }
  }
  await copyThenRemove(source, targetParent, targetName)
}

async function fileEntry(root, path) {
  const entry = await entryAt(root, pathSegments(path))
  if (entry.kind === 'folder') {
    throw new FolderError(
      'folder_move_unsupported',
      'Browsers can move files but not whole folders. Move the files inside it one by one.',
    )
  }
  return entry
}

async function move(root, command) {
  const source = await fileEntry(root, command.path)
  const target = pathSegments(command.to)
  const targetParent = await directoryAt(root, target.slice(0, -1))
  if (await exists(targetParent, target.at(-1))) {
    throw new FolderError('exists', `"${joinPath(target)}" already exists. Choose another name.`)
  }
  await moveFile(source, targetParent, target.at(-1))
  return { path: pathSegments(command.path).join('/'), to: joinPath(target) }
}

async function mkdir(root, command) {
  const segments = pathSegments(command.path)
  const parent = await directoryAt(root, segments.slice(0, -1))
  const existed = await exists(parent, segments.at(-1))
  await parent.getDirectoryHandle(segments.at(-1), { create: true })
  return { path: joinPath(segments), created: !existed }
}

async function freeBinName(bin, name) {
  if (!(await exists(bin, name))) return name
  for (let number = 2; number < 1000; number += 1) {
    const candidate = numberedName(name, number)
    if (!(await exists(bin, candidate))) return candidate
  }
  throw new FolderError('exists', 'The Möbius bin has too many files with this name. Ask the owner to empty it.')
}

async function bin(root, command) {
  const segments = pathSegments(command.path)
  if (segments[0] === BIN_FOLDER_NAME) {
    throw new FolderError('already_in_bin', 'This file is already in the Möbius bin. Only the owner empties it.')
  }
  const source = await fileEntry(root, command.path)
  const binFolder = await root.getDirectoryHandle(BIN_FOLDER_NAME, { create: true })
  const name = await freeBinName(binFolder, source.name)
  await moveFile(source, binFolder, name)
  return { path: joinPath(segments), to: `${BIN_FOLDER_NAME}/${name}` }
}

const OPERATIONS = { list, info, read, image, write, move, mkdir, bin }

/** A remembered folder: every operation, confined to `root`. */
export function createHandleFolder(root) {
  return {
    async run(command) {
      const operation = OPERATIONS[command.op]
      if (!operation) throw new FolderError('invalid_request', `Unknown folder operation "${command.op}".`)
      try {
        return await operation(root, command)
      } catch (error) {
        throw asFolderError(error, command.path)
      }
    },
  }
}
