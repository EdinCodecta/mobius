// Native location tracking bridge.
//
// The Möbius mobile app injects `window.MobiusNative` into the top frame on the
// owner's own origin (never in iframes, never in a plain browser). It can keep
// tracking the device location while the app is backgrounded, but it only
// delivers positions to a page that has called `startLocation` in its current
// document, and the page is the ONLY route to the server. This module owns that
// whole page-side contract:
//
//   - feature-detect `MobiusNative`;
//   - start / stop / resume tracking and surface the OS status string. Only
//     the phone app can change tracking; other devices can only view it;
//   - receive `mobius:location` batches, dedupe them, and queue them durably in
//     localStorage;
//   - upload the queue to the backend in batches, retrying on failure, and drop
//     positions from the queue only once the server has accepted them;
//   - remember (on the server, mirrored in localStorage) whether the owner
//     turned tracking on, and (in sessionStorage) whether it is running in THIS
//     app session, so a reload re-arms it but a relaunch does not.
//
// Resuming is deliberately session-scoped. A reloaded page must call
// `startLocation` again to keep receiving positions, but after the owner closes
// the app the native layer stops tracking — and with Android's "Only this time"
// grant the permission is revoked too, so calling `startLocation` would put an
// unrequested permission prompt in front of them. sessionStorage survives a
// web-view reload and is empty after a relaunch, so it is the one signal that
// tells the two apart. Outside of that reload resume, `startLocation` is only
// ever called because someone tapped Start.
//
// It is a single shell-owned controller with a small getState surface; apps
// reach it through the `location.tracking` capability
// (locationTrackingProvider.js). All of its heavy lifting happens whether or
// not any UI is mounted, because positions can arrive at any time the app is
// open.

import { apiFetch } from '../api/client.js'

const QUEUE_KEY = 'mobius.location.queue.v1'
const ENABLED_KEY = 'mobius.location.enabled.v1'
const OPTIONS_KEY = 'mobius.location.options.v1'
// Recently delivered fix timestamps, so a re-delivered batch is dropped before
// it re-enters the queue (the server is idempotent too; this just avoids pointless
// uploads). Bounded ring — the app itself only retains 5000 positions.
const DELIVERED_KEY = 'mobius.location.delivered.v1'
const DELIVERED_MAX = 6000
// sessionStorage: present only while tracking was started in this app session.
const SESSION_ACTIVE_KEY = 'mobius.location.session-active.v1'
// A stable, opaque id for this phone, so the server keeps each phone's fixes
// apart.
const DEVICE_KEY = 'mobius.location.device.v1'

const UPLOAD_CHUNK = 500
const RETRY_BASE_MS = 2000
const RETRY_MAX_MS = 60_000

const DEFAULT_OPTIONS = { background: true, interval: 10000, distance: 0 }

// ---- storage helpers (every access is defensive: private mode, quota, etc.) --

function readJSON(key, fallback) {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    const value = JSON.parse(raw)
    return value == null ? fallback : value
  } catch {
    return fallback
  }
}

function writeJSON(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // Out of quota or storage denied — tracking still works in memory for this
    // session; durability across reloads is best-effort by contract.
  }
}

// The session flag: set when startLocation resolves "started", cleared on stop
// or on any other result.
function hasSessionActive() {
  try { return sessionStorage.getItem(SESSION_ACTIVE_KEY) === '1' } catch { return false }
}

function setSessionActive(active) {
  try {
    if (active) sessionStorage.setItem(SESSION_ACTIVE_KEY, '1')
    else sessionStorage.removeItem(SESSION_ACTIVE_KEY)
  } catch {
    // Without sessionStorage a reload cannot resume; the owner taps Start again.
  }
}

function getDeviceId() {
  let id = null
  try { id = localStorage.getItem(DEVICE_KEY) } catch { /* ignore */ }
  if (!id) {
    id = globalThis.crypto?.randomUUID?.()
      || `dev-${Date.now()}-${Math.random().toString(36).slice(2)}`
    try { localStorage.setItem(DEVICE_KEY, id) } catch { /* ignore */ }
  }
  return id
}

// ---- feature detection ------------------------------------------------------

export function isNativeLocationAvailable() {
  return typeof window !== 'undefined'
    && !!window.MobiusNative
    && typeof window.MobiusNative.startLocation === 'function'
}

// ---- controller -------------------------------------------------------------

let state = {
  available: isNativeLocationAvailable(),
  // A stored "on" without this session's flag is left over from a closed app.
  enabled: readJSON(ENABLED_KEY, false) === true && hasSessionActive(),
  status: null,        // last OS status string: started | denied | disabled | unavailable | stopped
  queueLength: readJSON(QUEUE_KEY, []).length,
  uploading: false,
  lastError: null,
  lastUploadAt: null,
}

let listenerBound = false
let draining = false
let drainRequested = false
let retryTimer = null
let retryDelay = RETRY_BASE_MS

function setState(patch) {
  state = { ...state, ...patch }
}

export function getLocationState() {
  return state
}

// ---- queue ------------------------------------------------------------------

function loadQueue() {
  const q = readJSON(QUEUE_KEY, [])
  return Array.isArray(q) ? q : []
}

function saveQueue(queue) {
  writeJSON(QUEUE_KEY, queue)
  setState({ queueLength: queue.length })
}

function loadDelivered() {
  const d = readJSON(DELIVERED_KEY, [])
  return Array.isArray(d) ? d : []
}

// Normalize a raw device position to exactly the fields the backend stores,
// dropping anything malformed. latitude/longitude are mandatory; timestamp is
// the dedup identity, so a fix without a usable one cannot be stored.
function normalizePosition(raw) {
  if (!raw || typeof raw !== 'object') return null
  const lat = Number(raw.latitude)
  const lon = Number(raw.longitude)
  const ts = Number(raw.timestamp)
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null
  if (!Number.isFinite(ts) || !Number.isInteger(ts)) return null
  const opt = (v) => (v == null || !Number.isFinite(Number(v)) ? null : Number(v))
  return {
    latitude: lat,
    longitude: lon,
    timestamp: ts,
    accuracy: opt(raw.accuracy),
    altitude: opt(raw.altitude),
    speed: opt(raw.speed),
    heading: opt(raw.heading),
  }
}

// Enqueue a freshly received batch, deduped by timestamp against both what is
// already queued and what was recently delivered.
function enqueuePositions(rawPositions) {
  if (!Array.isArray(rawPositions) || rawPositions.length === 0) return
  const queue = loadQueue()
  const delivered = new Set(loadDelivered())
  const queued = new Set(queue.map((p) => p.timestamp))
  let added = 0
  for (const raw of rawPositions) {
    const pos = normalizePosition(raw)
    if (!pos) continue
    if (delivered.has(pos.timestamp) || queued.has(pos.timestamp)) continue
    queued.add(pos.timestamp)
    queue.push(pos)
    added += 1
  }
  if (added === 0) return
  // Keep the queue ordered by fix time, not arrival time.
  queue.sort((a, b) => a.timestamp - b.timestamp)
  saveQueue(queue)
  void drain()
}

function markDelivered(timestamps) {
  if (!timestamps.length) return
  let delivered = loadDelivered().concat(timestamps)
  if (delivered.length > DELIVERED_MAX) {
    delivered = delivered.slice(delivered.length - DELIVERED_MAX)
  }
  writeJSON(DELIVERED_KEY, delivered)
}

// ---- upload -----------------------------------------------------------------

function scheduleRetry() {
  if (retryTimer) return
  retryTimer = setTimeout(() => {
    retryTimer = null
    void drain()
  }, retryDelay)
  retryDelay = Math.min(retryDelay * 2, RETRY_MAX_MS)
}

function clearRetry() {
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null }
  retryDelay = RETRY_BASE_MS
}

// Upload the queue in chunks, oldest first, removing each chunk only after the
// server accepts it. Coalesces concurrent requests into one in-flight drain.
async function drain() {
  drainRequested = true
  if (draining) return
  draining = true
  setState({ uploading: true })
  try {
    while (drainRequested) {
      drainRequested = false
      let queue = loadQueue()
      if (queue.length === 0) { clearRetry(); break }
      if (typeof navigator !== 'undefined' && navigator.onLine === false) {
        scheduleRetry()
        break
      }
      const chunk = queue.slice(0, UPLOAD_CHUNK)
      let res
      try {
        res = await apiFetch('/location/batch', {
          method: 'POST',
          body: JSON.stringify({ positions: chunk, device_id: getDeviceId() }),
        })
      } catch (err) {
        setState({ lastError: String(err?.message || err) })
        scheduleRetry()
        break
      }
      if (res.ok) {
        // Re-read: a concurrent enqueue may have appended while we were in flight.
        const timestamps = new Set(chunk.map((p) => p.timestamp))
        const remaining = loadQueue().filter((p) => !timestamps.has(p.timestamp))
        saveQueue(remaining)
        markDelivered([...timestamps])
        clearRetry()
        setState({ lastError: null, lastUploadAt: Date.now() })
        drainRequested = true // keep draining any remaining chunks
      } else if (res.status === 422) {
        // The server rejected the whole chunk's shape. Dropping malformed fixes
        // is correct: retrying forever would wedge the queue behind bad data.
        const timestamps = new Set(chunk.map((p) => p.timestamp))
        const remaining = loadQueue().filter((p) => !timestamps.has(p.timestamp))
        saveQueue(remaining)
        setState({ lastError: 'Some positions were rejected and dropped.' })
        drainRequested = true
      } else {
        // 5xx / 429 / transient: keep the chunk and back off.
        setState({ lastError: `Upload failed (${res.status}).` })
        scheduleRetry()
        break
      }
    }
  } finally {
    draining = false
    setState({ uploading: false })
  }
}

// ---- server-side tracking preference ---------------------------------------

async function putTrackingState(enabled, options) {
  try {
    await apiFetch('/location/tracking', {
      method: 'PUT',
      body: JSON.stringify({ enabled, options: options || {} }),
    })
  } catch {
    // Best-effort record for other surfaces; this phone's own state is local.
  }
}

async function fetchTrackingState() {
  try {
    const res = await apiFetch('/location/tracking')
    if (!res.ok) return null
    return await res.json()
  } catch {
    return null
  }
}

// ---- public control surface -------------------------------------------------

function bindListener() {
  if (listenerBound || typeof window === 'undefined') return
  window.addEventListener('mobius:location', (e) => {
    const positions = e?.detail?.positions
    if (Array.isArray(positions)) enqueuePositions(positions)
  })
  listenerBound = true
}

function persistEnabled(enabled, options) {
  writeJSON(ENABLED_KEY, enabled)
  if (options) writeJSON(OPTIONS_KEY, options)
  setState({ enabled })
}

function savedOptions() {
  const opts = readJSON(OPTIONS_KEY, DEFAULT_OPTIONS)
  return { ...DEFAULT_OPTIONS, ...(opts && typeof opts === 'object' ? opts : {}) }
}

// Clamp caller-supplied cadence to sane bounds: 1s..1h between fixes, and a
// non-negative minimum-distance filter in metres. Unknown keys are dropped.
const MIN_INTERVAL_MS = 1000
const MAX_INTERVAL_MS = 3_600_000
function sanitizeOptions(options) {
  const out = {}
  const interval = Number(options?.interval)
  if (Number.isFinite(interval)) {
    out.interval = Math.max(MIN_INTERVAL_MS, Math.min(MAX_INTERVAL_MS, Math.round(interval)))
  }
  const distance = Number(options?.distance)
  if (Number.isFinite(distance)) {
    out.distance = Math.max(0, Math.round(distance))
  }
  return out
}

export function getTrackingOptions() {
  return savedOptions()
}

// Change the recording cadence (and/or distance filter). Only the phone app can
// change tracking: on any other device this does nothing and returns the
// unchanged options. The choice is saved, and if this phone is tracking, native
// tracking is re-armed so the new cadence takes effect live.
export async function setTrackingOptions(options) {
  if (!isNativeLocationAvailable()) return savedOptions()
  const merged = { ...savedOptions(), ...sanitizeOptions(options) }
  writeJSON(OPTIONS_KEY, merged)
  await putTrackingState(state.enabled === true, merged)
  if (state.status === 'started') {
    await callStartLocation({ ...DEFAULT_OPTIONS, ...merged })
  }
  return merged
}

// The single place that calls MobiusNative.startLocation, so every result is
// handled the same way. "started" marks this session as tracking; anything else
// ("denied", "disabled", "unavailable", or a throw) leaves tracking stopped and
// clears the flag, and the status drives the UI's explanation.
async function callStartLocation(opts) {
  let status
  try {
    status = await window.MobiusNative.startLocation(opts)
  } catch (err) {
    status = 'denied'
    setState({ lastError: String(err?.message || err) })
  }
  if (status === 'started') {
    setSessionActive(true)
    setState({ status, available: true })
    return status
  }
  setSessionActive(false)
  const wasEnabled = state.enabled
  persistEnabled(false, null)
  setState({ status, available: true })
  // If this device was tracking (a reload resume or a cadence re-arm that failed),
  // record that it has stopped so no surface keeps showing it as on.
  if (wasEnabled) void putTrackingState(false, savedOptions())
  return status
}

// Start (or change) tracking. Returns the OS status string. Only persists the
// "enabled" preference when tracking actually started.
export async function startTracking(options = savedOptions()) {
  bindListener()
  if (!isNativeLocationAvailable()) {
    setState({ status: 'unavailable', available: false })
    return 'unavailable'
  }
  const opts = { ...DEFAULT_OPTIONS, ...options }
  const status = await callStartLocation(opts)
  if (status === 'started') {
    persistEnabled(true, opts)
    void putTrackingState(true, opts)
    // Positions recorded while the page was gone are delivered right after this
    // call; make sure any still-queued ones from a prior session go out too.
    void drain()
  }
  return status
}

// Only the phone app can stop tracking: on any other device this changes
// nothing (the phone that is tracking keeps tracking) and reports 'unavailable'.
export async function stopTracking() {
  if (!isNativeLocationAvailable()) {
    setState({ status: 'unavailable', available: false })
    return 'unavailable'
  }
  setSessionActive(false)
  persistEnabled(false, null)
  void putTrackingState(false, savedOptions())
  let status = 'stopped'
  try {
    status = await window.MobiusNative.stopLocation()
  } catch {
    // Treat a failed stop as stopped locally; we have already cleared the
    // preference so no reload will re-arm it.
  }
  setState({ status })
  return status
}

// ---- boot ---------------------------------------------------------------------
//
// Tracking is controlled only on the phone that tracks. The saved server
// preference is a record of what that phone last did (other surfaces display
// it); it never starts, stops, or re-paces a phone. So there is no polling of the
// server here — a phone acts only on its owner's taps and on its own reload.

// Called once at shell boot. Registers the listener immediately, flushes any
// queue left from a prior session, and — if tracking was on in this app session
// (a reload) — re-arms it so the app resumes delivering positions to this new
// document. A relaunch after the app was closed starts stopped.
let initialized = false

export async function initLocationTracking() {
  if (typeof window === 'undefined' || initialized) return
  initialized = true
  bindListener()
  setState({ available: isNativeLocationAvailable() })
  // Always try to flush a leftover queue, even in a plain browser where an old
  // backlog might still be sitting in localStorage.
  void drain()

  if (typeof window !== 'undefined') {
    window.addEventListener('online', () => { clearRetry(); void drain() })
  }

  if (!isNativeLocationAvailable()) return

  // Resume only a reload within the same app session: the session flag alone
  // says this phone was tracking before the reload. A stored "on" without it
  // means the app was closed while tracking — the native side has stopped (and
  // may have lost permission) — so record it as stopped rather than calling
  // startLocation and prompting unasked.
  if (hasSessionActive()) {
    setState({ enabled: true })
    // The page is visible right after load, so an Android background start is
    // allowed here. Resume with this phone's own saved cadence.
    await startTracking(savedOptions())
    return
  }
  const server = await fetchTrackingState()
  const storedOn = server ? server.enabled === true : readJSON(ENABLED_KEY, false) === true
  persistEnabled(false, null)
  if (storedOn) {
    setState({ status: 'stopped' })
    void putTrackingState(false, savedOptions())
  }
}
