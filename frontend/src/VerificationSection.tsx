import { useCallback, useEffect, useState } from 'react'
import PublishPanel from './PublishPanel'
import VerifyPanel from './VerifyPanel'
import { ApiError, api } from './api'
import type { Candidate, CandidateSummary, Incident } from './types'

interface Props {
  token: string
  incident: Incident
}

function shortHash(value: string): string {
  return value.length > 12 ? value.slice(0, 12) : value
}

/** Verification tab: pick a candidate, verify it, preview and publish the PR. */
export default function VerificationSection({ token, incident }: Props) {
  const [cands, setCands] = useState<CandidateSummary[] | null>(null)
  const [selected, setSelected] = useState<Candidate | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const list = await api.listCandidates(token, incident.id)
      setCands(list)
      if (list.length > 0 && selected === null) {
        const newest = list.reduce((a, b) => (a.created_at >= b.created_at ? a : b))
        setSelected(await api.getCandidate(token, newest.id))
      }
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? `Could not load candidates (${err.status}): ${err.message}` : String(err))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, incident.id])

  useEffect(() => {
    setCands(null)
    setSelected(null)
    setError(null)
    void load()
  }, [load])

  async function handleSelect(candidateId: string): Promise<void> {
    setError(null)
    try {
      setSelected(await api.getCandidate(token, candidateId))
    } catch (err) {
      setError(err instanceof ApiError ? `Open failed (${err.status}): ${err.message}` : String(err))
    }
  }

  return (
    <div>
      {cands === null ? (
        <p className="hint">Loading candidates…</p>
      ) : cands.length === 0 ? (
        <p className="hint">No candidates yet. Generate one in the Patch tab first.</p>
      ) : (
        <div className="evidence-actions">
          <label>
            Candidate{' '}
            <select
              value={selected?.id ?? ''}
              onChange={(event) => void handleSelect(event.target.value)}
            >
              {cands.map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {shortHash(candidate.id)} · {candidate.state}
                  {candidate.changed_paths.length > 0 && ` · ${candidate.changed_paths.join(', ')}`}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {selected !== null && (
        <>
          <VerifyPanel token={token} candidate={selected} />
          <PublishPanel token={token} candidate={selected} />
        </>
      )}
    </div>
  )
}
