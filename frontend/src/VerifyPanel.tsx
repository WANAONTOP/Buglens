import { useCallback, useEffect, useState } from 'react'
import { HashValue } from './Progressive'
import { ApiError, api } from './api'
import type { Candidate, GateVerdict, Verification, VerificationSummary } from './types'

interface Props {
  token: string
  candidate: Candidate
}

const SHOTS = ['bl-broken-before.png', 'cd-broken-before.png', 'bl-broken-after.png', 'cd-broken-after.png']

function shortHash(value: string): string {
  return value.length > 12 ? value.slice(0, 12) : value
}

function verdictBadge(verdict: GateVerdict): string {
  switch (verdict) {
    case 'PASS':
      return 'badge-PASS'
    case 'FAIL':
      return 'badge-FAIL'
    default:
      return 'badge-BLOCKED'
  }
}

const SHOT_LABELS: Record<string, string> = {
  'bl-broken-before.png': 'Baseline · broken route · before click',
  'cd-broken-before.png': 'Candidate · broken route · before click',
  'bl-broken-after.png': 'Baseline · broken route · after click',
  'cd-broken-after.png': 'Candidate · broken route · after click',
}

function formatMeasure(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean' || value === null) return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

export default function VerifyPanel({ token, candidate }: Props) {
  const [runs, setRuns] = useState<VerificationSummary[] | null>(null)
  const [selected, setSelected] = useState<Verification | null>(null)
  const [openGates, setOpenGates] = useState<Set<number>>(new Set())
  const [shots, setShots] = useState<Record<string, string>>({})
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [downloading, setDownloading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadRuns = useCallback(async () => {
    try {
      setRuns(await api.listVerifications(token, candidate.id))
    } catch (err) {
      setError(err instanceof ApiError ? `Could not list verifications (${err.status}): ${err.message}` : String(err))
    }
  }, [token, candidate.id])

  useEffect(() => {
    setRuns(null)
    setSelected(null)
    setOpenGates(new Set())
    setShots({})
    setError(null)
    void loadRuns()
  }, [loadRuns])

  // Expand gates that need attention when a different run is opened.
  const selectedId = selected?.id
  useEffect(() => {
    if (!selected) return
    setOpenGates(new Set(selected.gates.filter((g) => g.verdict !== 'PASS').map((g) => g.gate)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId])

  function toggleGate(gate: number): void {
    setOpenGates((prev) => {
      const next = new Set(prev)
      if (next.has(gate)) {
        next.delete(gate)
      } else {
        next.add(gate)
      }
      return next
    })
  }

  const refreshSelected = useCallback(
    async (verificationId: string) => {
      try {
        const run = await api.getVerification(token, verificationId)
        setSelected(run)
        return run
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
      void refreshSelected(selected.id).then((run) => {
        if (run === null || (run.state !== 'queued' && run.state !== 'running')) {
          window.clearInterval(timer)
          void loadRuns()
        }
      })
    }, 5000)
    return () => window.clearInterval(timer)
  }, [selected, refreshSelected, loadRuns])

  // Load comparison screenshots for the selected run.
  useEffect(() => {
    setShots((prev) => {
      for (const url of Object.values(prev)) URL.revokeObjectURL(url)
      return {}
    })
    if (selected === null) return undefined
    let live = true
    const names = selected.artifacts.map((a) => a.name).filter((n) => SHOTS.includes(n))
    void Promise.all(
      names.map(async (name) => {
        try {
          const blob = await api.downloadVerifyArtifact(token, selected.id, name)
          if (live) {
            const url = URL.createObjectURL(blob)
            setShots((prev) => ({ ...prev, [name]: url }))
          }
        } catch {
          // Missing comparison shots stay absent; gates explain why.
        }
      }),
    )
    return () => {
      live = false
    }
  }, [token, selected])

  async function handleStart(): Promise<void> {
    setStarting(true)
    setError(null)
    try {
      const summary = await api.startVerification(token, candidate.id)
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
      await api.cancelVerification(token, selected.id)
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
      const blob = await api.downloadVerifyArtifact(token, selected.id, name)
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

  const approved = candidate.state === 'approved'
  const active = selected !== null && (selected.state === 'queued' || selected.state === 'running')

  return (
    <section id="verify">
      <h4>Seven-gate verification</h4>
      {!approved ? (
        <p className="hint">
          Verification requires developer approval of this candidate; the current state is{' '}
          {candidate.state}.
        </p>
      ) : (
        <div className="evidence-actions">
          <button type="button" onClick={() => void handleStart()} disabled={starting || active}>
            {starting ? 'Starting…' : 'Verify approved candidate'}
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

      {runs !== null && runs.length > 0 && (
        <ul className="evidence-list">
          {runs.map((run) => (
            <li key={run.id} className="evidence-item">
              <span className="evidence-name">
                {shortHash(run.id)} · {run.state}
                {run.outcome !== null && ` · ${run.outcome}`} · {run.gates_passed}/7 gates
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
            <dd>{selected.state}</dd>
            <dt>Outcome</dt>
            <dd>
              {selected.outcome === null ? (
                'pending'
              ) : (
                <span
                  className={`badge ${
                    selected.outcome === 'VERIFIED'
                      ? 'badge-PASS'
                      : selected.outcome === 'NOT_VERIFIED'
                        ? 'badge-FAIL'
                        : 'badge-BLOCKED'
                  }`}
                >
                  {selected.outcome}
                </span>
              )}
            </dd>
            {selected.detail && (
              <>
                <dt>Detail</dt>
                <dd>{selected.detail}</dd>
              </>
            )}
            <dt>Baseline</dt>
            <dd>
              <HashValue value={candidate.baseline_sha} label="baseline SHA" />
            </dd>
            <dt>Policy</dt>
            <dd>
              {selected.policy_hash ? (
                <HashValue value={selected.policy_hash} label="policy hash" />
              ) : (
                'not frozen yet'
              )}
            </dd>
            {selected.tested !== null && (
              <>
                <dt>Tested diff</dt>
                <dd>
                  <HashValue value={selected.tested.diff_sha256} label="tested diff hash" />
                </dd>
              </>
            )}
          </dl>

          {selected.gates.length > 0 && (
            <>
              <h4>Gate results</h4>
              <ul className="gate-list">
                {selected.gates.map((gate) => {
                  const open = openGates.has(gate.gate)
                  const stored = new Set(selected.artifacts.map((a) => a.name))
                  const measures = Object.entries(gate.measurements)
                  return (
                    <li key={gate.gate} className="gate-card">
                      <button
                        type="button"
                        className="gate-head"
                        aria-expanded={open}
                        aria-controls={`gate-body-${gate.gate}`}
                        onClick={() => toggleGate(gate.gate)}
                      >
                        <span className="gate-num">Gate {gate.gate}</span>
                        <span className="gate-name">{gate.name}</span>
                        <span className={`badge ${verdictBadge(gate.verdict)}`}>{gate.verdict}</span>
                        <span className="gate-toggle" aria-hidden="true">
                          {open ? '▾' : '▸'}
                        </span>
                      </button>
                      {open && (
                        <div className="gate-body" id={`gate-body-${gate.gate}`}>
                          <p className="gate-detail">{gate.detail}</p>
                          {measures.length > 0 && (
                            <table className="gate-measures">
                              <tbody>
                                {measures.map(([key, value]) => (
                                  <tr key={key}>
                                    <th scope="row">{key}</th>
                                    <td>{formatMeasure(value)}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}
                          {gate.artifacts.length > 0 && (
                            <ul className="gate-artifacts">
                              {gate.artifacts.map((name) =>
                                stored.has(name) ? (
                                  <li key={name}>
                                    <button
                                      type="button"
                                      onClick={() => void handleDownload(name)}
                                      disabled={downloading !== null}
                                    >
                                      {downloading === name ? 'Downloading…' : name}
                                    </button>
                                  </li>
                                ) : (
                                  <li key={name} className="gate-artifact-missing">
                                    {name} (not stored in this run)
                                  </li>
                                ),
                              )}
                            </ul>
                          )}
                        </div>
                      )}
                    </li>
                  )
                })}
              </ul>
            </>
          )}

          {Object.keys(shots).length > 0 && (
            <>
              <h4>Before/after comparison (baseline vs candidate)</h4>
              <div className="shot-grid">
                {SHOTS.filter((name) => shots[name]).map((name) => (
                  <figure key={name}>
                    <img src={shots[name]} alt={SHOT_LABELS[name] ?? name} />
                    <figcaption>
                      {SHOT_LABELS[name] ?? name}
                      <br />
                      {name}
                    </figcaption>
                  </figure>
                ))}
              </div>
            </>
          )}

          {selected.artifacts.length > 0 && (
            <>
              <h4>Evidence</h4>
              <ul className="evidence-list">
                {selected.artifacts.map((artifact) => (
                  <li key={artifact.name} className="evidence-item">
                    <span className="evidence-name">{artifact.name}</span>
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
