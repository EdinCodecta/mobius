/* Read-only commands on a folder picked for one visit (Safari, Firefox, Brave): a list of File objects. */
import { FolderError, joinPath, pathSegments } from './folderPaths.js'
import {
  DEFAULT_IMAGE_EDGE, fileInfo, pageOf, readFileSlice, shrinkImage, sortEntries,
} from './fileReads.js'

function readOnlyError() {
  return new FolderError(
    'read_only',
    'This browser shares folders read-only. Changing files needs Chrome or Edge.',
  )
}

function addChild(children, parentPath, child) {
  if (!children.has(parentPath)) children.set(parentPath, new Map())
  children.get(parentPath).set(child.name, child)
}

/** Index a picked folder's files by their path inside it. */
function indexFiles(files) {
  const filesByPath = new Map()
  const children = new Map([['', new Map()]])
  let rootName = ''
  for (const file of files) {
    const parts = (file.webkitRelativePath || file.name).split('/').filter(Boolean)
    if (parts.length > 1 && !rootName) rootName = parts[0]
    const inner = parts.length > 1 ? parts.slice(1) : parts
    filesByPath.set(inner.join('/'), file)
    inner.forEach((name, index) => {
      const parentPath = inner.slice(0, index).join('/')
      const isFile = index === inner.length - 1
      addChild(children, parentPath, isFile ? { name, kind: 'file', file } : { name, kind: 'folder' })
    })
  }
  return { filesByPath, children, rootName }
}

function describe(entry) {
  if (entry.kind === 'folder') return { name: entry.name, kind: 'folder' }
  return { name: entry.name, kind: 'file', size: entry.file.size, modified: entry.file.lastModified || null }
}

/** A one-visit folder: list, info and read only, from the files the browser handed over. */
export function createSnapshotFolder(files) {
  const { filesByPath, children, rootName } = indexFiles(files)

  function fileAt(segments) {
    const file = filesByPath.get(joinPath(segments))
    if (file) return file
    const kind = children.has(joinPath(segments)) ? 'wrong_kind' : 'not_found'
    throw new FolderError(kind, kind === 'wrong_kind'
      ? `"${joinPath(segments)}" is a folder. Use list.`
      : `Nothing is at "${joinPath(segments)}" in this folder.`)
  }

  function folderAt(segments) {
    const entries = children.get(joinPath(segments))
    if (entries) return entries
    const kind = filesByPath.has(joinPath(segments)) ? 'wrong_kind' : 'not_found'
    throw new FolderError(kind, kind === 'wrong_kind'
      ? `"${joinPath(segments)}" is a file. Use read or info.`
      : `Nothing is at "${joinPath(segments)}" in this folder.`)
  }

  const operations = {
    list(command) {
      const segments = pathSegments(command.path, { allowRoot: true })
      const { page, total, nextCursor } = pageOf(sortEntries([...folderAt(segments).values()]), command)
      return { path: joinPath(segments), entries: page.map(describe), total, nextCursor }
    },
    info(command) {
      const segments = pathSegments(command.path, { allowRoot: true })
      if (children.has(joinPath(segments))) {
        const name = segments.at(-1) || rootName
        return { path: joinPath(segments), name, kind: 'folder', entries: folderAt(segments).size }
      }
      return fileInfo(fileAt(segments), joinPath(segments))
    },
    read(command) {
      const segments = pathSegments(command.path)
      return readFileSlice(fileAt(segments), joinPath(segments), command)
    },
    image(command) {
      const segments = pathSegments(command.path)
      return shrinkImage(fileAt(segments), joinPath(segments), command.maxEdge || DEFAULT_IMAGE_EDGE)
    },
  }

  return {
    rootName,
    fileCount: filesByPath.size,
    async run(command) {
      const operation = operations[command.op]
      if (!operation) throw readOnlyError()
      return operation(command)
    },
  }
}
