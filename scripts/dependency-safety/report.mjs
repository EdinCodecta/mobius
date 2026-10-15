// The pull-request comment. Its first line is the marker the workflow uses to
// find and update its own comment. Lockfile text is untrusted, so package
// names are shown as inline code that cannot break out or mention anyone.

export const REPORT_MARKER = '<!-- dependency-safety -->'
const MAX_LISTED = 50

const code = text => `\`${String(text).replace(/[`\r\n]/g, '')}\``

export const hasAdvisories = advisories => Object.values(advisories ?? {}).some(list => list.length > 0)

export function packageNotes(change) {
  return [
    change.newPackage && 'new package',
    change.newInstallScript && 'runs an install script',
    change.changedDownload && 'same version, different download',
    change.outsideRegistry && 'downloaded from outside the npm registry',
  ].filter(Boolean)
}

function truncated(lines) {
  const hidden = lines.length - MAX_LISTED
  return hidden > 0 ? [...lines.slice(0, MAX_LISTED), `- …and ${hidden} more`] : lines
}

function advisoryLines(advisories, advisoryError, packageCount) {
  if (advisoryError) return [`❌ **Could not check npm's advisory database** (${advisoryError}). Re-run this check.`]
  if (!hasAdvisories(advisories)) return [`✅ No known malware or vulnerabilities in the ${packageCount} changed package(s).`]
  const found = Object.entries(advisories).flatMap(([name, list]) => list.map(advisory =>
    `- ${code(name)}: [${advisory.title}](${advisory.url}) (${advisory.severity}, affects ${code(advisory.vulnerable_versions)})`))
  return ['❌ **Known malware or vulnerabilities in changed packages. Do not merge until resolved.**', ...truncated(found)]
}

function flagLines(changes) {
  const flagged = changes
    .map(change => ({ id: `${change.name}@${change.version}`, notes: packageNotes(change) }))
    .filter(({ notes }) => notes.length > 0)
    .map(({ id, notes }) => `- ${code(id)}: ${notes.join(' · ')}`)
  if (flagged.length === 0) return ['Nothing else stood out.']
  return [`**Worth a look** (${flagged.length}):`, ...truncated(flagged)]
}

export function renderReport({ changes, advisories, advisoryError }) {
  return [
    REPORT_MARKER,
    '### Dependency safety check',
    '',
    ...advisoryLines(advisories, advisoryError, changes.length),
    '',
    ...flagLines(changes),
    '',
    "<sub>Checks changed lockfile entries against npm's advisory database (including GitHub malware advisories) and flags new packages, install scripts and unexpected downloads. Updated on every push.</sub>",
    '',
  ].join('\n')
}
