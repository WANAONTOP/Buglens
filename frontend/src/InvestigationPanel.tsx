import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { HashValue } from './Progressive'
import { ApiError, api } from './api'
import type { Incident, Investigation, InvestigationSummary, ReproSummary } from './types'

interface Props {
  token: string
  incident: Incident
}

function shortHash(value: string): string {
  return value.length > 12 ? value.slice(0, 12) : value
}

function stateBadge(state: string): string {
  switch (state) {
    case 'queued':
      return 'badge-local'
    case 'running':
      return 'badge-uploaded'
    case 'done':
      return 'badge-intake_complete'
    case 'error':
      return 'badge-FAIL'
    default:
      return 'badge-blocked'
  }
}

function originLabel(origin: string): string {
  switch (origin) {
    case 'uploaded':
      return 'Uploaded evidence'
    case 'baseline':
      return 'Baseline capture'
    case 'broken':
      return 'Broken-route capture'
    case 'control':
      return 'Control-route capture'
    default:
      return origin
  }
}

function renderRefs(refs: string[]): ReactNode {
  if (refs.length === 0) return 'none cited'
  return refs.map((ref, index) => (
    <span key={`${ref}-${index}`}>
      {index > 0 && ', '}
      {/* Buttons, not hash links: the app router owns `location.hash`. */}
      <button
        type="button"
        className="evidence-ref"
        onClick={() => document.getElementById(`ev-${ref}`)?.scrollIntoView({ block: 'center' })}
      >
        {ref}
      </button>
    </span>
  ))
}

export default function InvestigationPanel({ token, incident }: Props) {
  const [runs, setRuns] = useState<ReproSummary[] | null>(null)
  const [runId, setRunId] = useState('')
  const [invs, setInvs] = useState<InvestigationSummary[] | null>(null)
  const [selected, setSelected] = useState<Investigation | null>(null)
  const [frameUrl, setFrameUrl] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadLists = useCallback(async () => {
    try {
      const [fetchedRuns, fetchedInvs] = await Promise.all([
        api.listReproductions(token, incident.id),
        api.listInvestigations(token, incident.id),
      ])
      setRuns(fetchedRuns)
      setInvs(fetchedInvs)
      const reproduced = fetchedRuns.find((r) => r.state === 'done' && r.verdict === 'REPRODUCED')
      setRunId((prev) => prev || reproduced?.id || '')
    } catch (err) {
      setError(err instanceof ApiError ? `Could not load runs (${err.status}): ${err.message}` : String(err))
    }
  }, [token, incident.id])

  useEffect(() => {
    setRuns(null)
    setInvs(null)
    setSelected(null)
    setRunId('')
    setError(null)
    void loadLists()
  }, [loadLists])

  const refreshSelected = useCallback(
    async (invId: string) => {
      try {
        const inv = await api.getInvestigation(token, invId)
        setSelected(inv)
        return inv
      } catch (err) {
        setError(err instanceof ApiError ? `Refresh failed (${err.status}): ${err.message}` : String(err))
        return null
      }
    },
    [token],
  )

  useEffect(() => {
    if (selected === null || (selected.state !== 'queued' && selected.state !== 'running')) return
    const timer = window.setInterval(() => {
      void refreshSelected(selected.id).then((inv) => {
        if (inv === null || (inv.state !== 'queued' && inv.state !== 'running')) {
          window.clearInterval(timer)
          void loadLists()
        }
      })
    }, 2500)
    return () => window.clearInterval(timer)
  }, [selected, refreshSelected, loadLists])

  // Load the segmented frame for display (revoked on change).
  useEffect(() => {
    setFrameUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev)
      return null
    })
    if (selected?.sam?.frame_artifact) {
      let live = true
      api
        .downloadReproArtifact(token, selected.run_id, selected.sam.frame_artifact)
        .then((blob) => {
          if (live) setFrameUrl(URL.createObjectURL(blob))
        })
        .catch((err: unknown) => {
          if (live) {
            setError(
              err instanceof ApiError
                ? `Frame load failed (${err.status}): ${err.message}`
                : String(err),
            )
          }
        })
      return () => {
        live = false
      }
    }
    return undefined
  }, [token, selected?.run_id, selected?.sam?.frame_artifact])

  async function handleStart(): Promise<void> {
    if (!runId) return
    setStarting(true)
    setError(null)
    try {
      const summary = await api.startInvestigation(token, incident.id, runId)
      setInvs((prev) => (prev === null ? [summary] : [summary, ...prev]))
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
      await api.cancelInvestigation(token, selected.id)
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof ApiError ? `Cancel failed (${err.status}): ${err.message}` : String(err))
    } finally {
      setCancelling(false)
    }
  }

  const chosenRun = runs?.find((r) => r.id === runId) ?? null
  const runReady = chosenRun !== null && chosenRun.state === 'done' && chosenRun.verdict === 'REPRODUCED'
  const active = selected !== null && (selected.state === 'queued' || selected.state === 'running')

  return (
    <section>
      <h3>Investigation (read-only)</h3>
      {runs === null ? (
        <p className="hint">Loading runs…</p>
      ) : runs.length === 0 ? (
        <p className="hint">Reproduce the incident in Modal before investigating.</p>
      ) : (
        <div className="evidence-actions">
          <select value={runId} onChange={(e) => setRunId(e.target.value)} aria-label="Reproduction run">
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                {shortHash(r.commit_sha)} · {r.state}
                {r.verdict ? ` · ${r.verdict}` : ''}
              </option>
            ))}
          </select>
          <button type="button" onClick={() => void handleStart()} disabled={starting || active || !runReady}>
            {starting ? 'Starting…' : 'Investigate run'}
          </button>
          {active && selected !== null && (
            <button type="button" onClick={() => void handleCancel()} disabled={cancelling}>
              {cancelling ? 'Cancelling…' : 'Cancel'}
            </button>
          )}
        </div>
      )}
      {chosenRun !== null && !runReady && (
        <p className="hint">
          The selected run is {chosenRun.state}
          {chosenRun.verdict ? `/${chosenRun.verdict}` : ''}; investigation requires a
          done/REPRODUCED run with complete evidence.
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {invs !== null && invs.length > 0 && (
        <ul className="evidence-list">
          {invs.map((inv) => (
            <li key={inv.id} className="evidence-item">
              <span className="evidence-name">
                {shortHash(inv.id)} · {inv.state} · {inv.hypothesis_count}{' '}
                {inv.hypothesis_count === 1 ? 'hypothesis' : 'hypotheses'}
              </span>
              <span className="evidence-actions">
                <button type="button" onClick={() => void refreshSelected(inv.id)}>
                  {selected?.id === inv.id ? 'Refresh' : 'Open'}
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {selected !== null && (
        <div>
          <h4>Investigation {shortHash(selected.id)}</h4>
          <dl className="meta-grid">
            <dt>State</dt>
            <dd>
              <span className={`badge ${stateBadge(selected.state)}`}>{selected.state}</span>
            </dd>
            {selected.detail && (
              <>
                <dt>Detail</dt>
                <dd>{selected.detail}</dd>
              </>
            )}
            <dt>Commit</dt>
            <dd>
              <HashValue value={selected.commit_sha} label="commit SHA" />
            </dd>
            <dt>Contract</dt>
            <dd>
              <HashValue value={selected.contract_hash} label="contract hash" />
            </dd>
          </dl>

          {selected.sam !== null && frameUrl !== null && (
            <>
              <h4>
                Reproduction frame {selected.sam.frame_ref} ({selected.sam.frame_w}×
                {selected.sam.frame_h})
              </h4>
              <p className="hint">
                Captured by reproduction run {shortHash(selected.run_id)} — a reproduced image,
                not an uploaded file. Boxes below are the recorded segmentation matches only.
              </p>
              <div className="frame-wrap">
                <img src={frameUrl} alt={`Segmented frame ${selected.sam.frame_ref}`} />
                {selected.sam.matches.map((box) => (
                  <span
                    key={box.object_id}
                    className="sam-box"
                    style={{
                      left: `${(box.x1 / selected.sam!.frame_w) * 100}%`,
                      top: `${(box.y1 / selected.sam!.frame_h) * 100}%`,
                      width: `${((box.x2 - box.x1 + 1) / selected.sam!.frame_w) * 100}%`,
                      height: `${((box.y2 - box.y1 + 1) / selected.sam!.frame_h) * 100}%`,
                    }}
                    title={`id${box.object_id} (${box.x1},${box.y1})-(${box.x2},${box.y2})`}
                  />
                ))}
              </div>
              <p className="hint">{selected.sam.note}</p>
            </>
          )}

          {selected.spark !== null && (
            <>
              <h4>Visual interpretation ({selected.spark.call.model})</h4>
              {selected.spark.parsed ? (
                <>
                  <h4>Observations</h4>
                  <ul className="blocker-list">
                    {selected.spark.observations.map((line, i) => (
                      <li key={i}>{line}</li>
                    ))}
                  </ul>
                  <h4>Causal guesses</h4>
                  <ul className="blocker-list">
                    {selected.spark.hypotheses.map((line, i) => (
                      <li key={i}>{line}</li>
                    ))}
                  </ul>
                </>
              ) : (
                <p className="hint">
                  Unparsed model reply ({selected.spark.parse_note}): {selected.spark.raw_text}
                </p>
              )}
              <p className="hint">
                {selected.spark.call.latency_ms}ms · {selected.spark.call.attempts} attempt(s)
                {selected.spark.frames.length > 0 && ` · frames ${selected.spark.frames.join(', ')}`}
              </p>
            </>
          )}

          {selected.mappings.length > 0 && (
            <>
              <h4>Source mappings (pinned commit)</h4>
              <ul className="evidence-list">
                {selected.mappings.map((m) => (
                  <li key={m.target} className="evidence-item">
                    <span className="evidence-name">
                      {m.target} · {m.certainty}
                      {m.file && ` · ${m.file}:${m.start_line}-${m.end_line}`}
                    </span>
                    <span className="evidence-meta">{m.note}</span>
                  </li>
                ))}
              </ul>
            </>
          )}

          {selected.wonder !== null && (
            <>
              <h4>Root-cause hypotheses ({selected.wonder.call.model})</h4>
              {selected.wonder.parsed ? (
                <div>
                  {selected.wonder.hypotheses.map((h) => (
                    <article key={h.rank} className="hypothesis">
                      <h5 className="hypothesis-title">
                        #{h.rank} {h.cause}
                      </h5>
                      <dl>
                        <dt>Mechanism</dt>
                        <dd>{h.mechanism}</dd>
                        <dt>Supporting</dt>
                        <dd>{renderRefs(h.supporting_evidence)}</dd>
                        {h.conflicting_or_missing.length > 0 && (
                          <>
                            <dt>Conflicting/missing</dt>
                            <dd>{h.conflicting_or_missing.join('; ')}</dd>
                          </>
                        )}
                        {h.locations.length > 0 && (
                          <>
                            <dt>Locations</dt>
                            <dd>
                              {h.locations
                                .map((l) => `${l.file}:${l.start_line}-${l.end_line} (${l.certainty})`)
                                .join(', ')}
                            </dd>
                          </>
                        )}
                        {h.unvalidated_citations.length > 0 && (
                          <>
                            <dt>Unvalidated</dt>
                            <dd>{h.unvalidated_citations.join('; ')}</dd>
                          </>
                        )}
                        {h.repair_direction && (
                          <>
                            <dt>Repair direction</dt>
                            <dd>{h.repair_direction}</dd>
                          </>
                        )}
                      </dl>
                    </article>
                  ))}
                </div>
              ) : (
                <p className="hint">
                  Unparsed model reply ({selected.wonder.parse_note}): {selected.wonder.raw_text}
                </p>
              )}
            </>
          )}

          {selected.evidence.length > 0 && (
            <>
              <h4>Evidence package</h4>
              <ul className="evidence-list">
                {selected.evidence.map((item) => (
                  <li key={item.ref} id={`ev-${item.ref}`} className="evidence-item">
                    <span className="evidence-name">
                      {item.ref} · {item.summary}
                    </span>
                    <span className="evidence-meta">
                      <span
                        className={`origin-tag${item.origin === 'uploaded' ? ' origin-uploaded' : ''}`}
                      >
                        {originLabel(item.origin)}
                      </span>{' '}
                      {item.kind}
                      {item.route && ` · ${item.route}`}
                      {item.artifact && ` · ${item.artifact}`}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {selected.absent_evidence.length > 0 && (
            <>
              <h4>Not captured</h4>
              <ul className="blocker-list">
                {selected.absent_evidence.map((note, i) => (
                  <li key={i}>{note}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </section>
  )
}
