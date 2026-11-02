import type { IncidentSummary } from './types'

interface Props {
  incidents: IncidentSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
}

export default function IncidentList({ incidents, selectedId, onSelect }: Props) {
  return (
    <section className="card">
      <h2>Saved incidents ({incidents.length})</h2>
      {incidents.length === 0 ? (
        <p className="hint">No incidents yet. File the first report above.</p>
      ) : (
        <ul className="incident-list">
          {incidents.map((incident) => (
            <li key={incident.id}>
              <button
                type="button"
                className={incident.id === selectedId ? 'incident-item selected' : 'incident-item'}
                onClick={() => onSelect(incident.id)}
              >
                <span className="incident-title">{incident.title}</span>
                <span className="incident-meta">
                  {incident.repo ?? 'no repo'} @{' '}
                  {incident.commit_sha
                    ? incident.commit_sha.slice(0, 7)
                    : (incident.ref ?? 'no ref')}
                  {' · '}
                  {incident.evidence_count} evidence
                </span>
                <span className={`badge badge-${incident.intake_status}`}>
                  {incident.intake_status === 'blocked' ? 'Blocked' : 'Intake complete'}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
