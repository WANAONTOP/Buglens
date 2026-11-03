import { useCallback, useEffect, useState } from 'react'
import { ApiError, api } from './api'
import type {
  Candidate,
  Publication,
  PublicationPreview,
  VerificationSummary,
} from './types'

interface Props {
  token: string
  candidate: Candidate
}

function shortHash(value: string): string {
  return value.length > 12 ? value.slice(0, 12) : value
}

export default function PublishPanel({ token, candidate }: Props) {
  const [runs, setRuns] = useState<VerificationSummary[] | null>(null)
  const [runId, setRunId] = useState('')
  const [title, setTitle] = useState('')
  const [preview, setPreview] = useState<PublicationPreview | null>(null)
  const [previewKey, setPreviewKey] = useState('')
  const [records, setRecords] = useState<Publication[] | null>(null)
  const [selected, setSelected] = useState<Publication | null>(null)
  const [loading, setLoading] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadLists = useCallback(async () => {
    try {
      const [found, pubs] = await Promise.all([
        api.listVerifications(token, candidate.id),
        api.listPublications(token, candidate.id),
      ])
      setRuns(found)
      setRecords(pubs)
      const verified = found.find((r) => r.state === 'done' && r.outcome === 'VERIFIED')
      if (verified) setRunId((prev) => prev || verified.id)
    } catch (err) {
      setError(err instanceof ApiError ? `Could not load runs (${err.status}): ${err.message}` : String(err))
    }
  }, [token, candidate.id])

  useEffect(() => {
    setRuns(null)
    setRunId('')
    setPreview(null)
    setPreviewKey('')
    setRecords(null)
    setSelected(null)
    setError(null)
    void loadLists()
  }, [loadLists])

  async function handlePreview(): Promise<void> {
    if (!runId) return
    setLoading(true)
    setError(null)
    try {
      const result = await api.previewPublication(token, candidate.id, runId, title.trim())
      setPreview(result)
      setPreviewKey(`${runId}::${title.trim()}`)
    } catch (err) {
      setPreview(null)
      setPreviewKey('')
      setError(err instanceof ApiError ? `Preview failed (${err.status}): ${err.message}` : String(err))
    } finally {
      setLoading(false)
    }
  }

  async function handlePublish(): Promise<void> {
    if (!preview || !preview.preflight_ok) return
    const confirmed = window.confirm(
      `Publish this exact preview to GitHub?\n${preview.repo} ${preview.branch} -> ${preview.base_branch}\nPR title: ${preview.pr_title}`,
    )
    if (!confirmed) return
    setPublishing(true)
    setError(null)
    try {
      const record = await api.publishRepair(token, candidate.id, preview.verification_id, title.trim())
      setSelected(record)
      setRecords((prev) => (prev === null ? [record] : [record, ...prev.filter((r) => r.id !== record.id)]))
    } catch (err) {
      setError(err instanceof ApiError ? `Publish failed (${err.status}): ${err.message}` : String(err))
      try {
        setRecords(await api.listPublications(token, candidate.id))
      } catch {
        // Keep the publish error; the list refresh is best-effort.
      }
    } finally {
      setPublishing(false)
    }
  }

  async function handleOpen(publicationId: string): Promise<void> {
    setError(null)
    try {
      setSelected(await api.getPublication(token, publicationId))
    } catch (err) {
      setError(err instanceof ApiError ? `Open failed (${err.status}): ${err.message}` : String(err))
    }
  }

  const previewStale = preview !== null && previewKey !== `${runId}::${title.trim()}`
  const canPublish = preview !== null && preview.preflight_ok && !previewStale && !publishing && !loading

  return (
    <section>
      <h4>Verified GitHub PR publication (never auto-merges)</h4>
      <p className="hint">
        Preview shows the exact branch, diff and PR body after a server-side preflight. Publish is
        enabled only when every preflight check passes, and it files the previewed change only.
      </p>

      <div className="evidence-actions">
        <label>
          Verified run{' '}
          <select value={runId} onChange={(event) => setRunId(event.target.value)}>
            <option value="">Select a run…</option>
            {(runs ?? []).map((run) => (
              <option key={run.id} value={run.id}>
                {shortHash(run.id)} · {run.state}
                {run.outcome !== null && ` · ${run.outcome}`} · {run.gates_passed}/7 gates
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="evidence-actions">
        <label>
          PR title{' '}
          <input
            type="text"
            value={title}
            maxLength={200}
            size={52}
            placeholder="Leave blank to use the incident title"
            onChange={(event) => setTitle(event.target.value)}
          />
        </label>
      </div>
      <div className="evidence-actions">
        <button type="button" onClick={() => void handlePreview()} disabled={loading || !runId}>
          {loading ? 'Loading preview…' : 'Load preview & preflight'}
        </button>
        <button type="button" onClick={() => void handlePublish()} disabled={!canPublish}>
          {publishing ? 'Publishing…' : 'Publish this exact preview to GitHub'}
        </button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {preview !== null && (
        <div>
          <h4>Publication preview</h4>
          {previewStale && (
            <p className="hint">The run or title changed; reload the preview before publishing.</p>
          )}
          <dl className="meta-grid">
            <dt>Repository</dt>
            <dd>{preview.repo}</dd>
            <dt>Base</dt>
            <dd>
              {preview.base_branch} @ {preview.base_sha}
            </dd>
            <dt>Repair branch</dt>
            <dd>{preview.branch}</dd>
            <dt>Changed files</dt>
            <dd>{preview.changed_files.join(', ') || '(none)'}</dd>
            <dt>Diff SHA-256</dt>
            <dd>{preview.diff_sha256}</dd>
            <dt>PR title</dt>
            <dd>{preview.pr_title}</dd>
            <dt>Verification</dt>
            <dd>{preview.verification_id}</dd>
            <dt>Preflight</dt>
            <dd>{preview.preflight_ok ? 'all checks passed' : 'BLOCKED — see failed checks'}</dd>
          </dl>

          <h4>Server preflight</h4>
          <ul className="evidence-list">
            {preview.preflight.map((check) => (
              <li key={check.name} className="evidence-item">
                <span className="evidence-name">
                  {check.name}{' '}
                  <span className={`badge ${check.ok ? 'badge-intake_complete' : 'badge-blocked'}`}>
                    {check.ok ? 'PASS' : 'FAIL'}
                  </span>
                </span>
                {check.detail && <span className="evidence-meta">{check.detail}</span>}
              </li>
            ))}
          </ul>

          <h4>Exact diff</h4>
          <pre className="diff-view">{preview.diff}</pre>

          <h4>PR body</h4>
          <pre className="diff-view">{preview.pr_body}</pre>
        </div>
      )}

      {records !== null && records.length > 0 && (
        <>
          <h4>Publications</h4>
          <ul className="evidence-list">
            {records.map((record) => (
              <li key={record.id} className="evidence-item">
                <span className="evidence-name">
                  {shortHash(record.id)} · {record.state} · {record.branch}
                  {record.state === 'published' && record.pr_number > 0 && ` · PR #${record.pr_number}`}
                </span>
                <span className="evidence-actions">
                  <button type="button" onClick={() => void handleOpen(record.id)}>
                    {selected?.id === record.id ? 'Refresh' : 'Open'}
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {selected !== null && (
        <div>
          <h4>Publication {shortHash(selected.id)}</h4>
          <dl className="meta-grid">
            <dt>State</dt>
            <dd>{selected.state}</dd>
            {selected.detail && (
              <>
                <dt>Detail</dt>
                <dd>{selected.detail}</dd>
              </>
            )}
            <dt>Branch</dt>
            <dd>{selected.branch}</dd>
            {selected.commit_sha && (
              <>
                <dt>Commit</dt>
                <dd>{selected.commit_sha}</dd>
              </>
            )}
            {selected.state === 'published' && selected.pr_url && (
              <>
                <dt>Pull request</dt>
                <dd>
                  <a href={selected.pr_url} target="_blank" rel="noreferrer">
                    {selected.pr_url}
                  </a>
                </dd>
              </>
            )}
          </dl>
          {selected.events.length > 0 && (
            <>
              <h4>Publication events</h4>
              <ul className="evidence-list">
                {selected.events.map((event, index) => (
                  <li key={`${event.at}-${index}`} className="evidence-item">
                    <span className="evidence-name">{event.phase}</span>
                    <span className="evidence-meta">{event.message}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </section>
  )
}
