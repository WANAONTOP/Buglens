import { useState } from 'react'
import { ApiError, api } from './api'
import type { Incident } from './types'
import CandidatePanel from './CandidatePanel'
import EvidenceCapture, { type LocalItem } from './EvidenceCapture'
import InvestigationPanel from './InvestigationPanel'
import ReproPanel from './ReproPanel'

interface Props {
  token: string
  incident: Incident
  onChanged: (incident: Incident) => void
  onClose: () => void
  initialItems?: LocalItem[]
}

export default function IncidentDetail({ token, incident, onChanged, onClose, initialItems }: Props) {
  const [sha, setSha] = useState('')
  const [pinning, setPinning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function refresh(): Promise<void> {
    try {
      onChanged(await api.getIncident(token, incident.id))
    } catch (err) {
      setError(err instanceof ApiError ? `Refresh failed (${err.status}): ${err.message}` : String(err))
    }
  }

  async function handlePin(event: React.FormEvent): Promise<void> {
    event.preventDefault()
    setPinning(true)
    setError(null)
    try {
      onChanged(await api.pinCommit(token, incident.id, sha.trim()))
      setSha('')
    } catch (err) {
      setError(err instanceof ApiError ? `Pin failed (${err.status}): ${err.message}` : String(err))
    } finally {
      setPinning(false)
    }
  }

  return (
    <section className="card">
      <div className="detail-header">
        <h2>{incident.title}</h2>
        <button type="button" onClick={onClose}>
          Back to list
        </button>
      </div>

      <dl className="meta-grid">
        <dt>Status</dt>
        <dd>
          <span className={`badge badge-${incident.intake_status}`}>
            {incident.intake_status === 'blocked' ? 'Blocked' : 'Intake complete'}
          </span>
        </dd>
        {incident.blockers.length > 0 && (
          <>
            <dt>Blockers</dt>
            <dd>
              <ul className="blocker-list">
                {incident.blockers.map((blocker) => (
                  <li key={blocker}>{blocker}</li>
                ))}
              </ul>
            </dd>
          </>
        )}
        <dt>Repository</dt>
        <dd>{incident.repo ?? 'not set'}</dd>
        <dt>Ref</dt>
        <dd>{incident.ref ?? 'not set'}</dd>
        <dt>Commit</dt>
        <dd>{incident.commit_sha ?? 'not pinned'}</dd>
        <dt>Viewport</dt>
        <dd>{incident.browser_viewport ?? 'not set'}</dd>
        <dt>Investigation</dt>
        <dd>
          {incident.investigation_blockers.length === 0 ? (
            'Prerequisites recorded.'
          ) : (
            <>
              Cannot run yet — still needed:
              <ul className="blocker-list">
                {incident.investigation_blockers.map((blocker) => (
                  <li key={blocker}>{blocker}</li>
                ))}
              </ul>
            </>
          )}
        </dd>
        <dt>Observed</dt>
        <dd>{incident.observed_behavior}</dd>
        <dt>Expected</dt>
        <dd>{incident.expected_behavior}</dd>
        <dt>Steps</dt>
        <dd>
          <ol className="steps-list">
            {incident.reproduction_steps.map((step, index) => (
              <li key={index}>{step}</li>
            ))}
          </ol>
        </dd>
        <dt>Permitted paths</dt>
        <dd>{incident.permitted_source_paths.length > 0 ? incident.permitted_source_paths.join(', ') : 'none'}</dd>
      </dl>

      {!incident.commit_sha && (
        <form className="pin-form" onSubmit={(event) => void handlePin(event)}>
          <label>
            Pin commit SHA
            <input
              value={sha}
              onChange={(e) => setSha(e.target.value)}
              placeholder="40-character hex SHA"
              spellCheck={false}
            />
          </label>
          <button type="submit" disabled={pinning || sha.trim().length === 0}>
            {pinning ? 'Pinning…' : 'Pin commit'}
          </button>
        </form>
      )}

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <ReproPanel token={token} incident={incident} />

      <InvestigationPanel token={token} incident={incident} />

      <CandidatePanel token={token} incident={incident} />

      <EvidenceCapture
        key={incident.id}
        token={token}
        incidentId={incident.id}
        uploaded={incident.evidence}
        onChanged={() => void refresh()}
        initialItems={initialItems}
      />
    </section>
  )
}
