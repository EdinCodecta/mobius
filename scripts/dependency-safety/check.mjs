#!/usr/bin/env node
// Compares every npm lockfile a pull request changes against its base and
// writes the safety report. Exits 1 when a changed package has a known
// advisory; writes no report when no lockfile installs anything new.

import { execFileSync, spawnSync } from 'node:child_process'
import { writeFileSync } from 'node:fs'
import { posix } from 'node:path'
import { parseArgs } from 'node:util'

import { changedPackages } from './lockfile-changes.mjs'
import { findAdvisories, publishDates } from './registry.mjs'
import { hasAdvisories, renderReport } from './report.mjs'

const git = (...args) => execFileSync('git', args, { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 })

function changedLockfiles(base) {
  return git('diff', '--name-only', base, 'HEAD')
    .split('\n')
    .filter(file => posix.basename(file) === 'package-lock.json')
}

function readLock(ref, file) {
  const exists = spawnSync('git', ['cat-file', '-e', `${ref}:${file}`]).status === 0
  return exists ? JSON.parse(git('show', `${ref}:${file}`)) : null
}

async function lookUpAdvisories(changes) {
  try {
    return { advisories: await findAdvisories(changes), advisoryError: null }
  } catch (error) {
    return { advisories: null, advisoryError: error.message }
  }
}

const { values: options } = parseArgs({
  options: { base: { type: 'string', default: 'HEAD^1' }, out: { type: 'string' } },
})
const sections = changedLockfiles(options.base)
  .map(lockfile => ({
    lockfile,
    changes: changedPackages(readLock(options.base, lockfile), readLock('HEAD', lockfile)),
  }))
  .filter(section => section.changes.length > 0)

if (sections.length === 0) {
  console.log('No lockfile installs a new package version.')
  process.exit(0)
}

const changes = sections.flatMap(section => section.changes)
const uniqueChanges = [...new Map(changes.map(change => [`${change.name}@${change.version}`, change])).values()]
const { advisories, advisoryError } = await lookUpAdvisories(uniqueChanges)
const dates = await publishDates(uniqueChanges)
const report = renderReport({ sections, advisories, advisoryError, dates, now: new Date() })

if (options.out) writeFileSync(options.out, report)
console.log(report)
if (advisoryError) console.log(`::warning::Dependency safety check could not reach npm advisories: ${advisoryError}`)
process.exitCode = hasAdvisories(advisories) ? 1 : 0
