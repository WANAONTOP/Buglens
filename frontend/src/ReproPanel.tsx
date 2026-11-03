import { useCallback, useEffect, useState } from 'react'
import { ApiError, api, formatBytes } from './api'
import type { Incident, ReproRun, ReproState, ReproSummary, ReproVerdict } from './types'

interface Props {
  token: string
  incident: Incident
}

function stateBadge(state: ReproState): string {
  switch (state) {
    case 'queued':
      return 'badge-local'
    case 'running':
      return 'badge-uploaded'
    case 'done':
      return 'badge-intake_complete'
    default:
      return 'badge-blocked'
  }
}

function verdictLabel(verdict: ReproVerdict): string {
  switch (verdict) {
    case 'REPRODUCED':
      return 'Reproduced (evaluation only — not repaired)'
    case 'NEEDS_REPRODUCTION':
      return 'Failure not established'
    case 'BLOCKED':
      return 'Blocked'
  }
}

function shortHash(value: string): string {
  return value.length > 12 ? value.slice(0, 12) : value
}

export default function ReproPanel({ token, incident }: Props) {
  const [runs, setRuns] = useState<ReproSummary[] | null>(null)
  const [selected, setSelected] = useState<ReproRun | null>(null)
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [downloading, setDownloading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const readyToStart = incident.repo !== null && incident.ref !== null

  const loadRuns = useCallback(async () => {
    try {
      setRuns(await api.listReproductions(token, incident.id))
    } catch (err) {
      setError(err instanceof ApiError ? `Could not list reproductions (${err.status}): ${err.message}` : String(err))
    }
  }, [token, incident.id])

  useEffect(() => {
    setRuns(null)
    setSelected(null)
    setError(null)
    void loadRuns()
  }, [loadRuns])

  const refreshSelected = useCallback(
    async (runId: string) => {
      try {
        const run = await api.getReproduction(token, runId)
        setSelected(run)
        setRuns((prev) =>
          prev === null
            ? prev
            : prev.map((entry) =>
                entry.id === run.id
                  ? {
                      id: run.id,
                      incident_id: run.incident_id,
                      repo: run.repo,
                      commit_sha: run.commit_sha,
                      contract_hash: run.contract_hash,
                      state: run.state,
                      verdict: run.verdict,
                      detail: run.detail,
                      artifact_count: run.artifacts.length,
                      created_at: run.created_at,
                      updated_at: run.updated_at,
                    }
                  : entry,
              ),
        )
        return run
      } catch (err) {
        setError(err instanceof ApiError ? `Refresh failed (${err.status}): ${err.message}` : String(err))
        return null
      }
    },
    [token],
  )

  // Poll the selected run while it is active.
  useEffect(() => {
    if (selected === null || (selected.state !== 'queued' && selected.state !== 'running')) return
    const timer = window.setInterval(() => {
      void refreshSelected(selected.id).then((run) => {
        if (run === null || (run.state !== 'queued' && run.state !== 'running')) {
          window.clearInterval(timer)
          void loadRuns()
        }
      })
    }, 2000)
    return () => window.clearInterval(timer)
  }, [selected, refreshSelected, loadRuns])

  async function handleStart(): Promise<void> {
    setStarting(true)
    setError(null)
    try {
      const summary = await api.startReproduction(token, incident.id)
      setRuns((prev) => (prev === null ? [summary] : [summary, ...prev]))
      await refreshSelected(summary.id)
    } catch (err) {
      setError(err instanceof ApiError ? `Start failed (${err.status}): ${err.message}` : String(err))
    } finally {
      setStarting(false)
    }
  }

  async function handleCancel(): Promise<void> {
    if (selected === null) return
    setCancelling(true)
    setError(null)
    try {
      await api.cancelReproduction(token, selected.id)
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof ApiError ? `Cancel failed (${err.status}): ${err.message}` : String(err))
    } finally {
      setCancelling(false)
    }
  }

  async function handleDownload(name: string): Promise<void> {
    if (selected === null) return
    setDownloading(name)
    setError(null)
    try {
      const blob = await api.downloadReproArtifact(token, selected.id, name)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = name
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 5000)
    } catch (err) {
      setError(err instanceof ApiError ? `Download failed (${err.status}): ${err.message}` : String(err))
    } finally {
      setDownloading(null)
    }
  }

  const active = selected !== null && (selected.state === 'queued' || selected.state === 'running')

  return (
    <section>
      <h3>Modal reproduction</h3>
      {!readyToStart ? (
        <p className="hint">
          Set the repository and ref before reproducing; drafts can store evidence but cannot start a
          reproduction.
        </p>
      ) : (
        <div className="evidence-actions">
          <button type="button" onClick={() => void handleStart()} disabled={starting || active}>
            {starting ? 'Starting…' : 'Reproduce in Modal'}
          </button>
          {active && selected !== null && (
            <button type="button" onClick={() => void handleCancel()} disabled={cancelling}>
              {cancelling ? 'Cancelling…' : 'Cancel run'}
            </button>
          )}
        </div>
      )}

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {runs === null ? (
        <p className="hint">Loading reproductions…</p>
      ) : runs.length === 0 ? (
        <p className="hint">No reproductions yet for this incident.</p>
      ) : (
        <ul className="evidence-list">
          {runs.map((run) => (
            <li key={run.id} className="evidence-item">
              <span className="evidence-name">
                {shortHash(run.commit_sha)} · {run.state}
                {run.verdict !== null && ` · ${run.verdict}`}
              </span>
              <span className="evidence-meta">
                contract {shortHash(run.contract_hash)} · {run.artifact_count} artifact
                {run.artifact_count === 1 ? '' : 's'}
              </span>
              <span className="evidence-actions">
                <button type="button" onClick={() => void refreshSelected(run.id)}>
                  {selected?.id === run.id ? 'Refresh' : 'Open'}
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {selected !== null && (
        <div>
          <h4>Run {shortHash(selected.id)}</h4>
          <dl className="meta-grid">
            <dt>State</dt>
            <dd>
              <span className={`badge ${stateBadge(selected.state)}`}>{selected.state}</span>
            </dd>
            <dt>Verdict</dt>
            <dd>
              {selected.verdict === null ? (
                'pending'
              ) : (
                <span className={`badge badge-${selected.verdict}`}>{verdictLabel(selected.verdict)}</span>
              )}
            </dd>
            {selected.detail && (
              <>
                <dt>Detail</dt>
                <dd>{selected.detail}</dd>
              </>
            )}
            <dt>Commit</dt>
            <dd>{selected.commit_sha}</dd>
            <dt>Contract</dt>
            <dd>{selected.contract_hash}</dd>
          </dl>

          {selected.events.length > 0 && (
            <>
              <h4>Events</h4>
              <ol className="steps-list">
                {selected.events.map((event, index) => (
                  <li key={index}>
                    [{event.phase}] {event.message}
                  </li>
                ))}
              </ol>
            </>
          )}

          {selected.artifacts.length > 0 && (
            <>
              <h4>Artifacts</h4>
              <ul className="evidence-list">
                {selected.artifacts.map((artifact) => (
                  <li key={artifact.name} className="evidence-item">
                    <span className="evidence-name">{artifact.name}</span>
                    <span className="evidence-meta">
                      {formatBytes(artifact.size_bytes)} · {artifact.content_type}
                    </span>
                    <span className="evidence-actions">
                      <button
                        type="button"
                        onClick={() => void handleDownload(artifact.name)}
                        disabled={downloading !== null}
                      >
                        {downloading === artifact.name ? 'Downloading…' : 'Download'}
                      </button>
                    </span>
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
