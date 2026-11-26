import { useRef, useState } from 'react'
import { ApiError, api } from './api'
import type { Incident } from './types'
import ActivityPanel from './ActivityPanel'
import CandidatePanel from './CandidatePanel'
import EvidenceCapture, { type LocalItem } from './EvidenceCapture'
import InvestigationPanel from './InvestigationPanel'
import ProgressTracker from './ProgressTracker'
import { Disclosure, HashValue } from './Progressive'
import ReproPanel from './ReproPanel'
import StatusBadges from './StatusBadges'
import VerificationSection from './VerificationSection'

interface Props {
  token: string
  incident: Incident
  onChanged: (incident: Incident) => void
  onClose: () => void
  initialItems?: LocalItem[]
}

type TabId = 'evidence' | 'diagnosis' | 'patch' | 'verification'

const TABS: { id: TabId; label: string }[] = [
  { id: 'evidence', label: 'Evidence' },
  { id: 'diagnosis', label: 'Diagnosis' },
  { id: 'patch', label: 'Patch' },
  { id: 'verification', label: 'Verification' },
]

function tabForAnchor(anchor: string): TabId {
  switch (anchor) {
    case 'evidence':
      return 'evidence'
    case 'reproduce':
    case 'diagnose':
      return 'diagnosis'
    case 'patch':
      return 'patch'
    default:
      return 'verification'
  }
}

export default function IncidentDetail({ token, incident, onChanged, onClose, initialItems }: Props) {
  const [tab, setTab] = useState<TabId>('evidence')
  const [sha, setSha] = useState('')
  const [pinning, setPinning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([])

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

  function goToAnchor(anchor: string): void {
    setTab(tabForAnchor(anchor))
    document.getElementById('workspace-tabs')?.scrollIntoView({ block: 'start' })
  }

  function onTabKeyDown(event: React.KeyboardEvent): void {
    const order = TABS.map((t) => t.id)
    const index = order.indexOf(tab)
    let next: number | null = null
    if (event.key === 'ArrowRight') next = (index + 1) % order.length
    if (event.key === 'ArrowLeft') next = (index - 1 + order.length) % order.length
    if (event.key === 'Home') next = 0
    if (event.key === 'End') next = order.length - 1
    if (next === null) return
    event.preventDefault()
    setTab(order[next])
    tabRefs.current[next]?.focus()
  }

  return (
    <>
      <div className="investigation-head">
        <button type="button" className="btn-text" onClick={onClose}>
          ← Reports
        </button>
        <h2>{incident.title}</h2>
        <p className="investigation-sub">
          {incident.repo ?? 'no repository'} · {incident.evidence.length}{' '}
          {incident.evidence.length === 1 ? 'uploaded file' : 'uploaded files'}
        </p>
        <StatusBadges token={token} incidentId={incident.id} intakeStatus={incident.intake_status} />
        {incident.blockers.length > 0 && (
          <p className="hint">
            Still needed: {incident.blockers.join(' · ')}
          </p>
        )}
      </div>

      <ProgressTracker token={token} incident={incident} onNavigate={goToAnchor} />

      <Disclosure title="Report details">
        <dl className="meta-grid">
          <dt>Repository</dt>
          <dd>{incident.repo ?? 'not set'}</dd>
          <dt>Ref</dt>
          <dd>{incident.ref ?? 'not set'}</dd>
          <dt>Commit</dt>
          <dd>
            {incident.commit_sha ? (
              <HashValue value={incident.commit_sha} label="commit SHA" />
            ) : (
              'not pinned'
            )}
          </dd>
          <dt>Viewport</dt>
          <dd>{incident.browser_viewport ?? 'not set'}</dd>
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
          <dd>
            {incident.permitted_source_paths.length > 0
              ? incident.permitted_source_paths.join(', ')
              : 'none'}
          </dd>
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
      </Disclosure>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <div
        className="tabs"
        role="tablist"
        aria-label="Investigation sections"
        id="workspace-tabs"
        onKeyDown={onTabKeyDown}
      >
        {TABS.map((entry, index) => (
          <button
            key={entry.id}
            ref={(el) => {
              tabRefs.current[index] = el
            }}
            type="button"
            role="tab"
            id={`tab-${entry.id}`}
            aria-selected={tab === entry.id}
            aria-controls={`tabpanel-${entry.id}`}
            tabIndex={tab === entry.id ? 0 : -1}
            className="tab"
            onClick={() => setTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id="tabpanel-evidence"
        aria-labelledby="tab-evidence"
        hidden={tab !== 'evidence'}
        className="tabpanel"
      >
        <section className="panel" aria-label="Evidence">
          <EvidenceCapture
            key={incident.id}
            token={token}
            incidentId={incident.id}
            uploaded={incident.evidence}
            onChanged={() => void refresh()}
            initialItems={initialItems}
          />
        </section>
      </div>

      <div
        role="tabpanel"
        id="tabpanel-diagnosis"
        aria-labelledby="tab-diagnosis"
        hidden={tab !== 'diagnosis'}
        className="tabpanel"
      >
        <section className="panel" aria-label="Reproduction">
          <ReproPanel token={token} incident={incident} />
        </section>
        <section className="panel panel-spaced" aria-label="Investigation">
          <InvestigationPanel token={token} incident={incident} />
        </section>
      </div>

      <div
        role="tabpanel"
        id="tabpanel-patch"
        aria-labelledby="tab-patch"
        hidden={tab !== 'patch'}
        className="tabpanel"
      >
        <section className="panel" aria-label="Candidate patches">
          <CandidatePanel token={token} incident={incident} />
        </section>
      </div>

      <div
        role="tabpanel"
        id="tabpanel-verification"
        aria-labelledby="tab-verification"
        hidden={tab !== 'verification'}
        className="tabpanel"
      >
        <section className="panel" aria-label="Verification and publication">
          <VerificationSection token={token} incident={incident} />
        </section>
      </div>

      <Disclosure title="Activity log">
        <ActivityPanel token={token} incidentId={incident.id} />
      </Disclosure>
    </>
  )
}
