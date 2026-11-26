import { useState } from 'react'
import Dialog from './Dialog'
import { ApiError, BACKEND_URL, api } from './api'

export interface BackendInfo {
  kind: 'checking' | 'connected' | 'unreachable'
  service?: string
  latencyMs?: number
  detail?: string
}

interface Props {
  backend: BackendInfo
  onRetryBackend: () => void
  onSaveToken: (token: string) => void
  onSignOut: () => void
  onClose: () => void
}

/** Connection diagnostics and access-token management. */
export default function SettingsDialog({ backend, onRetryBackend, onSaveToken, onSignOut, onClose }: Props) {
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  async function handleSave(event: React.FormEvent): Promise<void> {
    event.preventDefault()
    const token = draft.trim()
    if (!token || busy) return
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      await api.validateToken(token)
      onSaveToken(token)
      setDraft('')
      setSaved(true)
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setError(`Access denied (${err.status}): this token was rejected. The current session is unchanged.`)
      } else if (err instanceof ApiError && err.status === 0) {
        setError(`Cannot reach the backend at ${BACKEND_URL}. The current session is unchanged.`)
      } else {
        setError(err instanceof ApiError ? `Validation failed (${err.status}): ${err.message}` : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog title="Settings" onClose={onClose}>
      <h3>Connection</h3>
      <dl className="meta-grid">
        <dt>Status</dt>
        <dd>
          {backend.kind === 'checking' && 'Checking…'}
          {backend.kind === 'connected' && `Connected · ${backend.service ?? 'unknown service'}`}
          {backend.kind === 'unreachable' && 'Unreachable'}
        </dd>
        <dt>Backend URL</dt>
        <dd className="mono">{BACKEND_URL}</dd>
        {backend.kind === 'connected' && backend.latencyMs !== undefined && (
          <>
            <dt>Latency</dt>
            <dd>{backend.latencyMs} ms</dd>
          </>
        )}
        {backend.kind === 'unreachable' && backend.detail && (
          <>
            <dt>Detail</dt>
            <dd>{backend.detail}</dd>
          </>
        )}
      </dl>
      <div className="dialog-actions">
        <button type="button" onClick={onRetryBackend}>
          Retry connection
        </button>
      </div>

      <h3>Access token</h3>
      <p className="hint">
        Stored only in this tab&apos;s session storage. Changing it re-validates before replacing
        the current session.
      </p>
      <form className="form" onSubmit={(event) => void handleSave(event)}>
        <label>
          <span className="label-title">Change access token</span>
          <input
            type="password"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Paste new access token"
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {saved && <p className="hint">New token saved and validated.</p>}
        <div className="dialog-actions">
          <button type="submit" disabled={busy || draft.trim().length === 0}>
            {busy ? 'Validating…' : 'Save new token'}
          </button>
          <button
            type="button"
            className="btn-danger-ghost"
            onClick={() => {
              onSignOut()
              onClose()
            }}
          >
            Sign out
          </button>
        </div>
      </form>
    </Dialog>
  )
}
