/* Words Settings uses for shared folders, kept in one place so every list says the same thing. */

/** Only a folder Chrome or Edge remembers can be edited; everything else is read-only. */
export function rightsLabel(folder) {
  return folder.mode === 'edit' && folder.access === 'remembered' ? 'Read & edit' : 'Read only'
}

export function folderCount(count) {
  return `${count} folder${count === 1 ? '' : 's'}`
}

const NEEDS_YOU = new Set(['needs_permission', 'needs_pick'])

/** Folders on this browser that agents cannot use until the owner taps once. */
export function needsYouCount(folders) {
  return folders.filter(folder => NEEDS_YOU.has(folder.state)).length
}
