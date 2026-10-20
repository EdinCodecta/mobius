/* Requests behind the Möbius provider page: model access, trial activation and
   picker visibility. The full account (profile, hosting, deployments) stays in
   the Möbius · You app; this page only reads the provider-facing slice. */
import { BASE, getAuthHeaders } from '../../api/client.js'

export class MobiusAccessError extends Error {
  constructor(message, status = 0, code = '') {
    super(message)
    this.name = 'MobiusAccessError'
    this.status = status
    this.code = code
  }
}

function exactKeys(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const allowed = new Set([...required, ...optional])
  const keys = Object.keys(value)
  return required.every(key => keys.includes(key))
    && keys.every(key => allowed.has(key))
}

/** Validate GET /api/identity/agent; signed-out or unavailable access exposes no model data. */
export function parseAgentAccess(value) {
  const accessStates = ['signed_out', 'unavailable', 'available']
  if (
    !exactKeys(value, ['agent_access', 'models', 'balance', 'trial', 'retention'])
    || !accessStates.includes(value.agent_access)
    || !Array.isArray(value.models)
    || value.models.length > 50
    || !value.balance || typeof value.balance !== 'object' || Array.isArray(value.balance)
    || !value.trial || typeof value.trial !== 'object' || Array.isArray(value.trial)
    || !value.retention || typeof value.retention !== 'object' || Array.isArray(value.retention)
  ) throw new Error('Möbius returned invalid model access.')
  if (value.agent_access !== 'available') {
    if (
      value.models.length
      || [value.balance, value.trial, value.retention]
        .some(section => Object.keys(section).length)
    ) throw new Error('Möbius exposed model data without account access.')
    return value
  }
  const modelIds = new Set()
  for (const model of value.models) {
    const pricing = model?.pricing
    if (
      !exactKeys(model, ['id', 'name', 'pricing'], ['context_window'])
      || typeof model?.id !== 'string'
      || !/^[a-z0-9][a-z0-9_-]{0,79}$/.test(model.id)
      || typeof model?.name !== 'string'
      || !model.name
      || model.name.length > 80
      || !pricing || typeof pricing !== 'object' || Array.isArray(pricing)
      || !exactKeys(pricing, ['input', 'cached_input', 'output'])
      || !['input', 'cached_input', 'output'].every(kind => (
        Number.isFinite(pricing[kind]) && pricing[kind] >= 0
      ))
      || (model.context_window !== undefined && (
        !Number.isSafeInteger(model.context_window) || model.context_window < 1
      ))
    ) throw new Error('Möbius returned invalid model prices.')
    if (modelIds.has(model.id)) throw new Error('Möbius returned duplicate model aliases.')
    modelIds.add(model.id)
  }
  if (
    !exactKeys(value.trial, ['state'])
    || !['ready', 'active', 'expired', 'ineligible'].includes(value.trial.state)
  ) throw new Error('Möbius returned invalid trial state.')
  if (
    !exactKeys(value.balance, ['available_units'], ['available_usd'])
    || !Number.isSafeInteger(value.balance.available_units)
    || value.balance.available_units < 0
    || (value.balance.available_usd !== undefined && (
      typeof value.balance.available_usd !== 'string'
      || !/^\d+(?:\.\d{1,6})?$/.test(value.balance.available_usd)
    ))
  ) throw new Error('Möbius returned an invalid model balance.')
  if (
    !exactKeys(value.retention, ['policy', 'notice'])
    || value.retention.policy !== 'local-testing-v1'
    || typeof value.retention.notice !== 'string'
    || value.retention.notice.length < 20
    || value.retention.notice.length > 500
  ) throw new Error('Möbius returned invalid model retention state.')
  return value
}

/** One owner-authenticated request; agent-access responses are validated before use. */
export async function mobiusAccessRequest(path, options = {}) {
  let response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...options,
      headers: getAuthHeaders(options.headers),
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new MobiusAccessError(
      'This Möbius could not reach its identity service. Check your connection and try again.',
    )
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = body.detail
    throw new MobiusAccessError(
      typeof detail === 'string' ? detail : detail?.message || 'Möbius access is unavailable right now.',
      response.status,
      typeof detail?.code === 'string' ? detail.code : '',
    )
  }
  const pathname = path.split(/[?#]/, 1)[0]
  return pathname === '/api/identity/agent' || pathname === '/api/identity/agent/trial'
    ? parseAgentAccess(body)
    : body
}
