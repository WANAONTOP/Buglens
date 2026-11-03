import { useEffect, useState } from 'react'
import StatusBadges from './StatusBadges'
import { api } from './api'
import type { IncidentSummary } from './types'

interface Props {
  token: string
  incidents: IncidentSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
}

type Filter = 'all' | 'verified' | 'setup' | 'evidence'

function formatDate(iso: string): string | null {
  const time = new Date(iso).getTime()
  if (Number.isNaN(time)) return null
  return new Date(iso).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

export default function IncidentList({ token, incidents, selectedId, onSelect }: Props) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [verifiedMap, setVerifiedMap] = useState<Record<string, boolean>>({})

  // One bounded lookup per report so filters and badges share real data.
  useEffect(() => {
    let live = true
    void (async () => {
      const entries: Record<string, boolean> = {}
      for (const incident of incidents) {
        try {
          const cands = await api.listCandidates(token, incident.id)
          let found = false
          for (const cand of cands) {
            const verifs = await api.listVerifications(token, cand.id)
            if (verifs.some((v) => v.outcome === 'VERIFIED')) {
              found = true
              break
            }
          }
          entries[incident.id] = found
        } catch {
          entries[incident.id] = false
        }
      }
      if (live) setVerifiedMap(entries)
    })()
    return () => {
      live = false
    }
  }, [token, incidents])

  const needle = query.trim().toLowerCase()
  const visible = incidents.filter((incident) => {
    if (
      needle.length > 0 &&
      !incident.title.toLowerCase().includes(needle) &&
      !(incident.repo ?? '').toLowerCase().includes(needle)
    ) {
      return false
    }
    if (filter === 'verified' && verifiedMap[incident.id] !== true) return false
    if (filter === 'setup' && incident.intake_status !== 'blocked') return false
    if (filter === 'evidence' && incident.evidence_count === 0) return false
    return true
  })

  return (
    <section className="card" id="incidents" aria-label="Saved reports">
      <h2>Reports ({incidents.length})</h2>
      <div className="report-toolbar">
        <input
          className="search-input"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search by title or repository"
          aria-label="Search reports"
        />
        <label className="filter-label">
          Show{' '}
          <select
            value={filter}
            onChange={(event) => setFilter(event.target.value as Filter)}
            aria-label="Filter reports by status"
          >
            <option value="all">All reports</option>
            <option value="verified">Verification passed</option>
            <option value="setup">Setup incomplete</option>
            <option value="evidence">Has uploaded files</option>
          </select>
        </label>
      </div>
      {incidents.length === 0 ? (
        <p className="hint">No reports yet. Create the first one with New report.</p>
      ) : visible.length === 0 ? (
        <p className="hint">
          No reports match{needle ? ` “${query.trim()}”` : ''} with this filter. Clear the search
          or choose “All reports”.
        </p>
      ) : (
        <ul className="incident-list">
          {visible.map((incident) => {
            const created = formatDate(incident.created_at)
            const firstBlocker = incident.blockers[0]
            const missing =
              incident.blockers.length > 0
                ? `${firstBlocker.startsWith('missing ') ? firstBlocker : `Missing: ${firstBlocker}`}${incident.blockers.length > 1 ? ` (+${incident.blockers.length - 1} more)` : ''}`
                : null
            return (
              <li key={incident.id}>
                <button
                  type="button"
                  className={incident.id === selectedId ? 'incident-item selected' : 'incident-item'}
                  onClick={() => onSelect(incident.id)}
                  aria-current={incident.id === selectedId ? 'true' : undefined}
                >
                  <span className="incident-title">{incident.title}</span>
                  <span className="incident-meta">
                    {incident.repo ?? 'Repository not selected'} · {incident.evidence_count}{' '}
                    {incident.evidence_count === 1 ? 'uploaded file' : 'uploaded files'}
                    {created && ` · ${created}`} · #{incident.id.slice(0, 8)}
                  </span>
                  {missing && <span className="incident-missing">{missing}</span>}
                  <span className="incident-badges">
                    <StatusBadges
                      token={token}
                      incidentId={incident.id}
                      intakeStatus={incident.intake_status}
                      verifiedOverride={verifiedMap[incident.id] ?? null}
                    />
                  </span>
                  <span className="incident-open" aria-hidden="true">
                    Open →
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
