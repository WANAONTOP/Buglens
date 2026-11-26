import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from './api'

interface Props {
  token: string
  incidentId: string
}

interface Line {
  key: string
  at: string
  source: string
  phase: string
  message: string
}

function latestId<T extends { id: string; created_at: string }>(items: T[]): string | null {
  if (items.length === 0) return null
  return items.reduce((a, b) => (a.created_at >= b.created_at ? a : b)).id
}

/**
 * Real job-event stream over the existing REST transport (no WebSocket).
 * Sticks to the latest entry only while the reader is already at the bottom.
 */
export default function ActivityPanel({ token, incidentId }: Props) {
  const [lines, setLines] = useState<Line[] | null>(null)
  const [active, setActive] = useState(false)
  const [pinned, setPinned] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const boxRef = useRef<HTMLDivElement>(null)

  const refresh = useCallback(async () => {
    try {
      const [repros, invs, cands] = await Promise.all([
        api.listReproductions(token, incidentId),
        api.listInvestigations(token, incidentId),
        api.listCandidates(token, incidentId),
      ])
      const reproId = latestId(repros)
      const invId = latestId(invs)
      const candId = latestId(cands)
      const verifs = candId ? await api.listVerifications(token, candId) : []
      const verifId = latestId(verifs)

      const collected: Line[] = []
      let anyActive = false
      if (reproId) {
        const run = await api.getReproduction(token, reproId)
        if (run.state === 'queued' || run.state === 'running') anyActive = true
        for (const [i, e] of run.events.entries()) {
          collected.push({ key: `repro-${i}`, at: e.at, source: 'Reproduce', phase: e.phase, message: e.message })
        }
      }
      if (invId) {
        const inv = await api.getInvestigation(token, invId)
        if (inv.state === 'queued' || inv.state === 'running') anyActive = true
        for (const [i, e] of inv.events.entries()) {
          collected.push({ key: `inv-${i}`, at: e.at, source: 'Diagnose', phase: e.phase, message: e.message })
        }
      }
      if (candId) {
        const cand = await api.getCandidate(token, candId)
        if (cand.state === 'queued' || cand.state === 'generating') anyActive = true
        for (const [i, e] of cand.events.entries()) {
          collected.push({ key: `cand-${i}`, at: e.at, source: 'Patch', phase: e.phase, message: e.message })
        }
      }
      if (verifId) {
        const verif = await api.getVerification(token, verifId)
        if (verif.state === 'queued' || verif.state === 'running') anyActive = true
        for (const [i, e] of verif.events.entries()) {
          collected.push({ key: `verif-${i}`, at: e.at, source: 'Verify', phase: e.phase, message: e.message })
        }
      }
      collected.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0))
      setLines(collected.slice(-200))
      setActive(anyActive)
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? `Activity failed (${err.status}): ${err.message}` : String(err))
    }
  }, [token, incidentId])

  useEffect(() => {
    setLines(null)
    setError(null)
    void refresh()
  }, [refresh])

  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(timer)
  }, [active, refresh])

  useEffect(() => {
    const box = boxRef.current
    if (box && pinned) box.scrollTop = box.scrollHeight
  }, [lines, pinned])

  function handleScroll(): void {
    const box = boxRef.current
    if (!box) return
    setPinned(box.scrollHeight - box.scrollTop - box.clientHeight < 48)
  }

  return (
    <div>
      <p className="hint">{active ? 'Live — a job is running.' : 'Latest recorded events.'}</p>
      {error && (
        <p className="error" role="alert">
          {error}{' '}
          <button type="button" onClick={() => void refresh()}>
            Retry
          </button>
        </p>
      )}
      {lines === null ? (
        <p className="hint">Loading activity…</p>
      ) : lines.length === 0 ? (
        <p className="hint">No job activity yet — start a reproduction to see live events here.</p>
      ) : (
        <div className="activity-wrap">
          <div ref={boxRef} className="activity-log" onScroll={handleScroll} tabIndex={0} aria-label="Job activity log">
            {lines.map((line) => (
              <div key={line.key} className="activity-line">
                <span className="activity-source">{line.source}</span>
                <span className="activity-phase">[{line.phase}]</span>{' '}
                <span>{line.message}</span>
              </div>
            ))}
          </div>
          {!pinned && (
            <button
              type="button"
              className="activity-jump"
              onClick={() => {
                setPinned(true)
                const box = boxRef.current
                if (box) box.scrollTop = box.scrollHeight
              }}
            >
              Jump to latest ↓
            </button>
          )}
        </div>
      )}
    </div>
  )
}
