import { useState } from 'react'
import Dialog from './Dialog'
import { ApiError, BACKEND_URL, api } from './api'

interface Props {
  notice: string | null
  onConnected: (token: string) => void
  onClose: () => void
}

/** Masked token entry; validates against a protected endpoint before saving. */
export default function ConnectDialog({ notice, onConnected, onClose }: Props) {
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: React.FormEvent): Promise<void> {
    event.preventDefault()
    const token = draft.trim()
    if (!token || busy) return
    setBusy(true)
    setError(null)
    try {
      await api.validateToken(token)
      onConnected(token)
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setError(`Access denied (${err.status}): this token was rejected. Check the value and try again.`)
      } else if (err instanceof ApiError && err.status === 0) {
        setError(`Cannot reach the backend at ${BACKEND_URL}. Check that it is running, then retry.`)
      } else {
        setError(err instanceof ApiError ? `Validation failed (${err.status}): ${err.message}` : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog title="Connect to workspace" onClose={onClose}>
      {notice && (
        <p className="dialog-notice" role="alert">
          {notice}
        </p>
      )}
      <p className="hint">
        Enter the workspace access token. It is checked against the backend, then kept only in
        this tab&apos;s session storage.
      </p>
      <form className="form" onSubmit={(event) => void handleSubmit(event)}>
        <label>
          <span className="label-title">Access token</span>
          <input
            type="password"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Paste access token"
            autoComplete="off"
            spellCheck={false}
            data-autofocus
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <button type="submit" className="btn-primary" disabled={busy || draft.trim().length === 0}>
            {busy ? 'Connecting…' : 'Connect'}
          </button>
          <button type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  )
}
