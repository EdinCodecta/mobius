// Renders the pull-request comment for changed lockfile packages. The marker
// on the first line lets the workflow find and update its own comment.

export const REPORT_MARKER = '<!-- dependency-safety -->'
// Matches the Dependabot cooldown in .github/dependabot.yml; younger releases
// can still arrive as indirect dependencies or in hand-made lockfile changes.
export const MIN_RELEASE_AGE_DAYS = 7
const DAY_MS = 24 * 60 * 60 * 1000

const packageId = change => `${change.name}@${change.version}`

function ageInDays(publishedIso, now) {
  if (!publishedIso) return null
  return Math.floor((now - new Date(publishedIso)) / DAY_MS)
}

export function packageNotes(change, publishedIso, now) {
  const age = ageInDays(publishedIso, now)
  return [
    age === null && 'publish date unknown',
    age !== null && age < MIN_RELEASE_AGE_DAYS && `published ${age} day(s) ago`,
    change.newPackage && 'new package',
    change.newInstallScript && 'runs an install script',
    change.integrityChanged && 'same version, different contents',
    change.outsideRegistry && 'downloaded from outside the npm registry',
  ].filter(Boolean)
}

export function hasAdvisories(advisories) {
  return Object.values(advisories ?? {}).some(list => list.length > 0)
}

function advisoryLines(advisories, advisoryError, packageCount) {
  if (advisoryError) {
    return [`⚠️ Could not reach npm's advisory database (${advisoryError}), so malware and vulnerabilities were **not checked**. Re-run this check.`]
  }
  if (!hasAdvisories(advisories)) {
    return [`✅ No known malware or vulnerabilities in the ${packageCount} changed package(s).`]
  }
  const found = Object.entries(advisories).flatMap(([name, list]) => list.map(advisory =>
    `- \`${name}\`: [${advisory.title}](${advisory.url}) (${advisory.severity}, affects \`${advisory.vulnerable_versions}\`)`))
  return ['❌ **Known malware or vulnerabilities in changed packages. Do not merge until resolved.**', ...found]
}

function flagLines(changes, dates, now) {
  const flagged = changes
    .map(change => ({ change, notes: packageNotes(change, dates.get(packageId(change)), now) }))
    .filter(({ notes }) => notes.length > 0)
  if (flagged.length === 0) return ['Nothing else stood out.']
  return [
    `**Worth a look** (${flagged.length}):`,
    ...flagged.map(({ change, notes }) => `- \`${packageId(change)}\`: ${notes.join(' · ')}`),
  ]
}

function changeTable({ lockfile, changes }, dates) {
  const rows = changes.map(change => {
    const from = change.from.join(', ') || 'new'
    const published = dates.get(packageId(change))?.slice(0, 10) ?? 'unknown'
    return `| \`${change.name}\` | ${from} | ${change.version} | ${published} |`
  })
  return [
    `<details><summary>${changes.length} changed package(s) in <code>${lockfile}</code></summary>`,
    '',
    '| Package | From | To | Published |',
    '| --- | --- | --- | --- |',
    ...rows,
    '',
    '</details>',
  ]
}

export function renderReport({ sections, advisories, advisoryError, dates, now }) {
  const changes = sections.flatMap(section => section.changes)
  return [
    REPORT_MARKER,
    '### Dependency safety check',
    '',
    ...advisoryLines(advisories, advisoryError, changes.length),
    '',
    ...flagLines(changes, dates, now),
    '',
    ...sections.flatMap(section => changeTable(section, dates)),
    '',
    `<sub>Checks npm's advisory database (including GitHub malware advisories), releases younger than ${MIN_RELEASE_AGE_DAYS} days, new packages, install scripts and download sources. Updated on every push.</sub>`,
    '',
  ].join('\n')
}
