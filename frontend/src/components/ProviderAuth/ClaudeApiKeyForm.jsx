/* The owner types an Anthropic API key here and it posts straight to the
   backend. The key never passes through a chat or an agent, and the form keeps
   it only in local state until the save request completes. */
import { useId, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { authQueries, modelQueries, settingsQueries } from '../../hooks/queries.js'
import { detailToMessage } from '../../lib/errorDetail.js'

const CONSOLE_KEYS_URL = 'https://console.anthropic.com/settings/keys'

// Save one key and report a display-safe outcome. The backend checks the key
// with Anthropic before storing it, so `ok` means it is saved and usable.
export async function saveClaudeApiKey(client, value) {
  const apiKey = value.trim()
  if (!apiKey) return { ok: false, error: 'Paste your API key first.' }
  try {
    const response = await client.auth.provider.claude.saveApiKey(apiKey)
    if (response.ok) return { ok: true }
    const data = await response.json().catch(() => ({}))
    return { ok: false, error: detailToMessage(data.detail, 'Could not save the key. Please try again.') }
  } catch {
    return { ok: false, error: 'Network error.' }
  }
}

export default function ClaudeApiKeyForm({ subscriptionActive = false, onSaved, onCancel }) {
  const queryClient = useQueryClient()
  const [value, setValue] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const inputId = useId()

  async function submit(event) {
    event.preventDefault()
    setSaving(true)
    setError('')
    const outcome = await saveClaudeApiKey(api, value)
    setSaving(false)
    if (!outcome.ok) {
      setError(outcome.error)
      return
    }
    setValue('')
    authQueries.provider.statuses.markConnected(queryClient, 'claude')
    void modelQueries.registry.invalidate(queryClient)
    void settingsQueries.owner.invalidate(queryClient)
    onSaved?.()
  }

  return (
    <form className="pa__form" onSubmit={submit}>
      <label className="pa__field" htmlFor={inputId}>
        <span className="pa__input-label">Anthropic API key</span>
        <input
          id={inputId}
          name="anthropic-api-key"
          className="pa__input"
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="sk-ant-api…"
          autoFocus
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <p className="pa__muted">
        Create a key in the <a href={CONSOLE_KEYS_URL} target="_blank" rel="noopener noreferrer">Claude Console</a>.
        {' '}Claude chats and background tasks are then billed per use to that account.
        {subscriptionActive && ' While a Claude sign-in is connected it takes precedence, even if it stops working; to use the key instead, disconnect Claude and save the key again.'}
      </p>
      <div className="provider-connection__actions">
        {onCancel && (
          <button type="button" className="pa__btn pa__btn--sm" onClick={onCancel} disabled={saving}>Cancel</button>
        )}
        <button className="pa__btn pa__btn--sm" type="submit" disabled={saving || !value.trim()}>
          {saving ? 'Checking key…' : 'Save key'}
        </button>
      </div>
      {error && <p className="pa__error" role="alert">{error}</p>}
    </form>
  )
}
