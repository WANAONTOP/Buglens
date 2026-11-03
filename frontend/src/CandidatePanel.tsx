import { useCallback, useEffect, useState } from 'react'
import { ApiError, api } from './api'
import type {
  Candidate,
  CandidateSummary,
  Incident,
  Investigation,
  InvestigationSummary,
} from './types'

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
    case 'generating':
      return 'badge-local'
    case 'proposed':
      return 'badge-uploaded'
    case 'approved':
      return 'badge-intake_complete'
    default:
      return 'badge-blocked'
  }
}

function diffClass(line: string): string {
  if (line.startsWith('+') && !line.startsWith('+++')) return 'diff-add'
  if (line.startsWith('-') && !line.startsWith('---')) return 'diff-del'
  if (line.startsWith('@@')) return 'diff-hunk'
  return ''
}

export default function CandidatePanel({ token, incident }: Props) {
  const [invs, setInvs] = useState<InvestigationSummary[] | null>(null)
  const [invId, setInvId] = useState('')
  const [invDetail, setInvDetail] = useState<Investigation | null>(null)
  const [rank, setRank] = useState(1)
  const [cands, setCands] = useState<CandidateSummary[] | null>(null)
  const [selected, setSelected] = useState<Candidate | null>(null)
  const [note, setNote] = useState('')
  const [starting, setStarting] = useState(false)
  const [deciding, setDeciding] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadLists = useCallback(async () => {
    try {
      const [fetchedInvs, fetchedCands] = await Promise.all([
        api.listInvestigations(token, incident.id),
        api.listCandidates(token, incident.id),
      ])
      setInvs(fetchedInvs)
      setCands(fetchedCands)
      const done = fetchedInvs.find((i) => i.state === 'done' && i.hypothesis_count > 0)
      setInvId((prev) => prev || done?.id || '')
    } catch (err) {
      setError(err instanceof ApiError ? `Could not load (${err.status}): ${err.message}` : String(err))
    }
  }, [token, incident.id])

  useEffect(() => {
    setInvs(null)
    setCands(null)
    setSelected(null)
    setInvId('')
    setInvDetail(null)
    setError(null)
    void loadLists()
  }, [loadLists])

  useEffect(() => {
    setInvDetail(null)
    if (!invId) return
    api
      .getInvestigation(token, invId)
      .then((inv) => {
        setInvDetail(inv)
        setRank((prev) => {
          const ranks = (inv.wonder?.hypotheses ?? []).map((h) => h.rank)
          return ranks.includes(prev) ? prev : (ranks[0] ?? 1)
        })
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? `Investigation load failed (${err.status}): ${err.message}` : String(err))
      })
  }, [token, invId])

  const refreshSelected = useCallback(
    async (candidateId: string) => {
      try {
        const candidate = await api.getCandidate(token, candidateId)
        setSelected(candidate)
        return candidate
      } catch (err) {
        setError(err instanceof ApiError ? `Refresh failed (${err.status}): ${err.message}` : String(err))
        return null
      }
    },
    [token],
  )

  useEffect(() => {
    if (selected === null || (selected.state !== 'queued' && selected.state !== 'generating')) return
    const timer = window.setInterval(() => {
      void refreshSelected(selected.id).then((candidate) => {
        if (
          candidate === null ||
          (candidate.state !== 'queued' && candidate.state !== 'generating')
        ) {
          window.clearInterval(timer)
          void loadLists()
        }
      })
    }, 2500)
    return () => window.clearInterval(timer)
  }, [selected, refreshSelected, loadLists])

  async function handleStart(): Promise<void> {
    if (!invId) return
    setStarting(true)
    setError(null)
    try {
      const summary = await api.startCandidate(token, incident.id, invId, rank)
      setCands((prev) => (prev === null ? [summary] : [summary, ...prev]))
      await refreshSelected(summary.id)
    } catch (err) {
      setError(err instanceof ApiError ? `Start failed (${err.status}): ${err.message}` : String(err))
    } finally {
      setStarting(false)
    }
  }

  async function handleDecision(kind: 'approve' | 'reject'): Promise<void> {
    if (selected === null) return
    setDeciding(true)
    setError(null)
    try {
      if (kind === 'approve') {
        await api.approveCandidate(token, selected.id, selected.diff_sha256, note)
      } else {
        await api.rejectCandidate(token, selected.id, note)
      }
      setNote('')
      await refreshSelected(selected.id)
      await loadLists()
    } catch (err) {
      setError(err instanceof ApiError ? `Decision failed (${err.status}): ${err.message}` : String(err))
    } finally {
      setDeciding(false)
    }
  }

  const invReady =
    invDetail !== null &&
    invDetail.state === 'done' &&
    invDetail.wonder !== null &&
    invDetail.wonder.parsed &&
    invDetail.wonder.hypotheses.length > 0
  const active = selected !== null && (selected.state === 'queued' || selected.state === 'generating')

  return (
    <section>
      <h3>Candidate patches (review only — no verification yet)</h3>
      {invs === null ? (
        <p className="hint">Loading investigations…</p>
      ) : invs.length === 0 ? (
        <p className="hint">Complete an investigation before generating a candidate.</p>
      ) : (
        <div className="evidence-actions">
          <select value={invId} onChange={(e) => setInvId(e.target.value)} aria-label="Investigation">
            {invs.map((inv) => (
              <option key={inv.id} value={inv.id}>
                {shortHash(inv.id)} · {inv.state} · {inv.hypothesis_count}{' '}
                {inv.hypothesis_count === 1 ? 'hypothesis' : 'hypotheses'}
              </option>
            ))}
          </select>
          {invDetail?.wonder !== null && invDetail?.wonder !== undefined && (
            <select
              value={rank}
              onChange={(e) => setRank(Number(e.target.value))}
              aria-label="Hypothesis rank"
            >
              {invDetail.wonder.hypotheses.map((h) => (
                <option key={h.rank} value={h.rank}>
                  Hypothesis {h.rank}
                </option>
              ))}
            </select>
          )}
          <button
            type="button"
            onClick={() => void handleStart()}
            disabled={starting || active || !invReady}
          >
            {starting ? 'Starting…' : 'Generate candidate'}
          </button>
        </div>
      )}
      {invDetail !== null && !invReady && (
        <p className="hint">
          The selected investigation is {invDetail.state}; generation requires a completed,
          parsed investigation with at least one hypothesis.
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {cands !== null && cands.length > 0 && (
        <ul className="evidence-list">
          {cands.map((candidate) => (
            <li key={candidate.id} className="evidence-item">
              <span className="evidence-name">
                {shortHash(candidate.id)} · {candidate.state}
                {candidate.changed_paths.length > 0 && ` · ${candidate.changed_paths.join(', ')}`}
              </span>
              <span className="evidence-actions">
                <button type="button" onClick={() => void refreshSelected(candidate.id)}>
                  {selected?.id === candidate.id ? 'Refresh' : 'Open'}
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {selected !== null && (
        <div>
          <h4>Candidate {shortHash(selected.id)}</h4>
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
            <dt>Baseline</dt>
            <dd>{selected.baseline_sha}</dd>
            <dt>Contract</dt>
            <dd>{selected.contract_hash}</dd>
            {selected.diff_sha256 && (
              <>
                <dt>Diff hash</dt>
                <dd>{selected.diff_sha256}</dd>
              </>
            )}
          </dl>

          {selected.diff && (
            <>
              <h4>Proposed diff (not applied, not verified)</h4>
              <pre className="diff-view">
                {selected.diff.split('\n').map((line, i) => (
                  <span key={i} className={diffClass(line)}>
                    {line}
                    {'\n'}
                  </span>
                ))}
              </pre>
            </>
          )}

          {selected.explanations.length > 0 && (
            <>
              <h4>Changes</h4>
              <ul className="blocker-list">
                {selected.explanations.map((change, i) => (
                  <li key={i}>
                    {change.path}: {change.explanation}
                  </li>
                ))}
              </ul>
            </>
          )}
          {selected.evidence_refs.length > 0 && (
            <p className="hint">Supporting evidence: {selected.evidence_refs.join(', ')}</p>
          )}
          {selected.expected_effect && (
            <p className="hint">Expected effect: {selected.expected_effect}</p>
          )}
          {selected.regression_risks.length > 0 && (
            <>
              <h4>Regression risks</h4>
              <ul className="blocker-list">
                {selected.regression_risks.map((risk, i) => (
                  <li key={i}>{risk}</li>
                ))}
              </ul>
            </>
          )}

          {selected.validation !== null && (
            <>
              <h4>Validation {selected.validation.ok ? 'passed' : 'failed'}</h4>
              <ul className="evidence-list">
                {selected.validation.checks.map((check) => (
                  <li key={check.name} className="evidence-item">
                    <span className="evidence-name">
                      {check.ok ? 'PASS' : 'FAIL'} · {check.name}
                    </span>
                    {check.detail && <span className="evidence-meta">{check.detail}</span>}
                  </li>
                ))}
              </ul>
            </>
          )}

          {selected.approval !== null && (
            <p className="hint">
              Approved {selected.approval.approved_at} (bound to diff{' '}
              {shortHash(selected.approval.diff_sha256)}
              {selected.approval.note && `: ${selected.approval.note}`}). Approval authorizes a
              later verification run; it is not verification.
            </p>
          )}

          {selected.state === 'proposed' && (
            <div className="evidence-actions">
              <input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Review note (optional)"
                aria-label="Review note"
              />
              <button
                type="button"
                onClick={() => void handleDecision('approve')}
                disabled={deciding || selected.validation === null || !selected.validation.ok}
              >
                {deciding ? 'Working…' : 'Approve'}
              </button>
              <button type="button" onClick={() => void handleDecision('reject')} disabled={deciding}>
                {deciding ? 'Working…' : 'Reject'}
              </button>
            </div>
          )}
          {selected.state === 'proposed' &&
            (selected.validation === null || !selected.validation.ok) && (
              <p className="hint">
                Approval is disabled: the candidate did not pass validation.
              </p>
            )}
        </div>
      )}
    </section>
  )
}
