#!/usr/bin/env node
// Compares the npm lockfiles of two commits and writes the safety report. It
// only reads lockfile text from Git, so untrusted commits are safe inputs.
// Exits 1 when a changed package has a known advisory or the lookup failed;
// writes no report when no lockfile entry changed.

import { execFileSync, spawnSync } from 'node:child_process'
import { writeFileSync } from 'node:fs'
import { posix } from 'node:path'
import { parseArgs } from 'node:util'

import { changedPackages } from './lockfile-changes.mjs'
import { findAdvisories } from './registry.mjs'
import { hasAdvisories, renderReport } from './report.mjs'

const git = (...args) => execFileSync('git', args, { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 })

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
  options: { base: { type: 'string' }, head: { type: 'string' }, out: { type: 'string' } },
})
const changes = git('diff', '--name-only', options.base, options.head)
  .split('\n')
  .filter(file => posix.basename(file) === 'package-lock.json')
  .flatMap(file => changedPackages(readLock(options.base, file), readLock(options.head, file)))

if (changes.length === 0) {
  console.log('No lockfile entry changed.')
  process.exit(0)
}

const { advisories, advisoryError } = await lookUpAdvisories(changes)
const report = renderReport({ changes, advisories, advisoryError })
if (options.out) writeFileSync(options.out, report)
console.log(report)
process.exitCode = advisoryError || hasAdvisories(advisories) ? 1 : 0
