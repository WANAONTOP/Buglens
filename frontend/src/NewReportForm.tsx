import { useCallback, useRef, useState } from 'react'
import { ApiError, api } from './api'
import type { Incident } from './types'
import EvidenceCapture, { uploadLocalItem, type LocalItem } from './EvidenceCapture'

interface Props {
  token: string
  onSaved: (incident: Incident, leftoverItems: LocalItem[]) => void
}

function splitLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
}

export default function NewReportForm({ token, onSaved }: Props) {
  const [title, setTitle] = useState('')
  const [observed, setObserved] = useState('')
  const [expected, setExpected] = useState('')
  const [stepsText, setStepsText] = useState('')
  const [repo, setRepo] = useState('')
  const [ref, setRef] = useState('')
  const [commitSha, setCommitSha] = useState('')
  const [viewport, setViewport] = useState('')
  const [pathsText, setPathsText] = useState('')
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const pendingRef = useRef<LocalItem[]>([])
  const handlePendingChange = useCallback((items: LocalItem[]) => {
    pendingRef.current = items
  }, [])

  async function handleSubmit(event: React.FormEvent): Promise<void> {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setStatus('Creating incident…')
    let incident: Incident
    try {
      const trimmedRepo = repo.trim()
      const trimmedRef = ref.trim()
      const trimmedSha = commitSha.trim()
      const trimmedViewport = viewport.trim()
      incident = await api.createIncident(token, {
        title: title.trim(),
        observed_behavior: observed.trim(),
        expected_behavior: expected.trim(),
        reproduction_steps: splitLines(stepsText),
        ...(trimmedRepo ? { repo: trimmedRepo } : {}),
        ...(trimmedRef ? { ref: trimmedRef } : {}),
        ...(trimmedSha ? { commit_sha: trimmedSha } : {}),
        ...(trimmedViewport ? { browser_viewport: trimmedViewport } : {}),
        permitted_source_paths: splitLines(pathsText),
      })
    } catch (err) {
      setError(
        err instanceof ApiError ? `Save failed (${err.status}): ${err.message}` : String(err),
      )
      setSaving(false)
      setStatus(null)
      return
    }

    // Upload pending evidence against the incident that now exists. Every
    // item is attempted even if an earlier one fails; failures ride along
    // to the detail screen for retry instead of losing files or duplicating
    // the incident.
    const pending = pendingRef.current
    const leftovers: LocalItem[] = []
    for (let index = 0; index < pending.length; index++) {
      const item = pending[index]
      setStatus(`Uploading evidence ${index + 1} of ${pending.length}…`)
      try {
        await uploadLocalItem(token, incident.id, item, () => {})
      } catch (err) {
        leftovers.push({
          ...item,
          uploading: false,
          progress: null,
          error:
            err instanceof ApiError
              ? `Upload failed (${err.status}): ${err.message}`
              : String(err),
        })
      }
    }
    // Hand leftovers (with errors attached) to the detail screen for retry.
    // The detail panel mints fresh preview URLs; the blobs survive unmount.
    setSaving(false)
    setStatus(null)
    onSaved(incident, leftovers)
  }

  return (
    <section className="card">
      <h2>Report a bug</h2>
      <p className="hint">
        Attach evidence first or file a bare draft — only the top four fields are required. A
        saved report is intake only, not a reproduced or verified bug.
      </p>
      <EvidenceCapture
        token={token}
        incidentId={null}
        uploaded={[]}
        onChanged={() => {}}
        onPendingChange={handlePendingChange}
      />
      <form className="form" onSubmit={(event) => void handleSubmit(event)}>
        <label>
          <span className="label-title">Title</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={200} />
        </label>
        <label>
          <span className="label-title">Observed behavior</span>
          <textarea value={observed} onChange={(e) => setObserved(e.target.value)} required rows={3} />
        </label>
        <label>
          <span className="label-title">Expected behavior</span>
          <textarea value={expected} onChange={(e) => setExpected(e.target.value)} required rows={3} />
        </label>
        <label>
          <span className="label-title">Reproduction steps</span>
          <span className="label-hint">One step per line.</span>
          <textarea
            value={stepsText}
            onChange={(e) => setStepsText(e.target.value)}
            required
            rows={4}
            placeholder={'Open the page\nScroll down 500px'}
          />
        </label>
        <div className="form-row">
          <label>
            <span className="label-title">GitHub repository</span>
            <span className="label-hint">Optional for drafts; required before investigation.</span>
            <input
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="owner/name"
            />
          </label>
          <label>
            <span className="label-title">Branch / ref</span>
            <span className="label-hint">Optional for drafts; required before investigation.</span>
            <input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="main" />
          </label>
        </div>
        <label>
          <span className="label-title">Pinned commit SHA</span>
          <span className="label-hint">Optional now; pin it (here or later) to leave blocked.</span>
          <input
            value={commitSha}
            onChange={(e) => setCommitSha(e.target.value)}
            placeholder="40-character hex SHA"
            spellCheck={false}
          />
        </label>
        <label>
          <span className="label-title">Browser viewport</span>
          <span className="input-with-button">
            <input
              value={viewport}
              onChange={(e) => setViewport(e.target.value)}
              placeholder="1440x900"
            />
            <button
              type="button"
              onClick={() => setViewport(`${window.innerWidth}x${window.innerHeight}`)}
            >
              Use current window
            </button>
          </span>
        </label>
        <label>
          <span className="label-title">Permitted source paths</span>
          <span className="label-hint">
            One per line, relative to the repo root. Optional for drafts; required before
            investigation.
          </span>
          <textarea
            value={pathsText}
            onChange={(e) => setPathsText(e.target.value)}
            rows={2}
            placeholder={'src/nav.tsx'}
          />
        </label>
        {status && <p className="hint">{status}</p>}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" disabled={saving}>
          {saving ? 'Saving…' : 'Save report'}
        </button>
      </form>
    </section>
  )
}
