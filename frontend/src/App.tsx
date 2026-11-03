import { useCallback, useEffect, useState } from 'react'
import './App.css'
import {
  ApiError,
  BACKEND_URL,
  api,
  clearStoredToken,
  getStoredToken,
  setStoredToken,
} from './api'
import type { Incident, IncidentSummary } from './types'
import NewReportForm from './NewReportForm'
import IncidentList from './IncidentList'
import IncidentDetail from './IncidentDetail'
import type { LocalItem } from './EvidenceCapture'

type BackendState =
  | { kind: 'checking' }
  | { kind: 'connected'; service: string; latencyMs: number }
  | { kind: 'unreachable'; detail: string }

const INCIDENT_HASH = /^#incident=([0-9a-f]{32})$/

function incidentIdFromHash(): string | null {
  const match = INCIDENT_HASH.exec(window.location.hash)
  return match ? match[1] : null
}

function App() {
  const [backend, setBackend] = useState<BackendState>({ kind: 'checking' })
  const [token, setToken] = useState(getStoredToken())
  const [tokenDraft, setTokenDraft] = useState(getStoredToken())
  const [incidents, setIncidents] = useState<IncidentSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(incidentIdFromHash())
  const [selected, setSelected] = useState<Incident | null>(null)
  const [carryover, setCarryover] = useState<{ incidentId: string; items: LocalItem[] } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const checkBackend = useCallback(async () => {
    setBackend({ kind: 'checking' })
    const started = performance.now()
    try {
      const response = await fetch(`${BACKEND_URL}/health`)
      if (!response.ok) {
        setBackend({
          kind: 'unreachable',
          detail: `HTTP ${response.status} from ${BACKEND_URL}/health`,
        })
        return
      }
      const body = await response.json()
      if (body?.status !== 'ok') {
        setBackend({
          kind: 'unreachable',
          detail: `Unexpected payload: ${JSON.stringify(body)}`,
        })
        return
      }
      setBackend({
        kind: 'connected',
        service: String(body.service ?? 'unknown'),
        latencyMs: Math.round(performance.now() - started),
      })
    } catch (err) {
      setBackend({
        kind: 'unreachable',
        detail: err instanceof Error ? err.message : String(err),
      })
    }
  }, [])

  useEffect(() => {
    void checkBackend()
  }, [checkBackend])

  const loadList = useCallback(async (activeToken: string) => {
    setLoading(true)
    setError(null)
    try {
      setIncidents(await api.listIncidents(activeToken))
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Could not load incidents (${err.status}): ${err.message}`
          : String(err),
      )
    } finally {
      setLoading(false)
    }
  }, [])

  const loadSelected = useCallback(
    async (activeToken: string, id: string) => {
      setLoading(true)
      setError(null)
      try {
        setSelected(await api.getIncident(activeToken, id))
      } catch (err) {
        setSelected(null)
        setError(
          err instanceof ApiError
            ? `Could not open incident (${err.status}): ${err.message}`
            : String(err),
        )
      } finally {
        setLoading(false)
      }
    },
    [],
  )

  // Load the list once a token is present; reopen the hashed incident after refresh.
  useEffect(() => {
    if (!token) {
      setIncidents([])
      setSelected(null)
      return
    }
    void loadList(token)
    const hashed = incidentIdFromHash()
    if (hashed) {
      setSelectedId(hashed)
      void loadSelected(token, hashed)
    }
  }, [token, loadList, loadSelected])

  // Track back/forward navigation across incidents. Hash changes caused by
  // in-app selection already match state and are skipped, which also keeps
  // draft-save carryover intact until a real navigation clears it.
  useEffect(() => {
    const onHashChange = () => {
      const hashed = incidentIdFromHash()
      if (hashed === selectedId) return
      setCarryover(null)
      setSelectedId(hashed)
      if (hashed && token) {
        void loadSelected(token, hashed)
      } else {
        setSelected(null)
      }
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [token, loadSelected, selectedId])

  function saveToken(event: React.FormEvent): void {
    event.preventDefault()
    setStoredToken(tokenDraft.trim())
    setToken(tokenDraft.trim())
  }

  function clearToken(): void {
    clearStoredToken()
    setTokenDraft('')
    setToken('')
    setSelectedId(null)
    setSelected(null)
    window.location.hash = ''
  }

  function selectIncident(id: string): void {
    window.location.hash = `incident=${id}`
    setCarryover(null)
    setSelectedId(id)
    setSelected(null)
    void loadSelected(token, id)
  }

  function closeIncident(): void {
    window.location.hash = ''
    setCarryover(null)
    setSelectedId(null)
    setSelected(null)
  }

  function handleSaved(incident: Incident, leftoverItems: LocalItem[]): void {
    void loadList(token)
    selectIncident(incident.id)
    setCarryover(leftoverItems.length > 0 ? { incidentId: incident.id, items: leftoverItems } : null)
  }

  function handleChanged(incident: Incident): void {
    setSelected(incident)
    void loadList(token)
  }

  return (
    <main className="page">
      <h1>BugLens AI</h1>
      <p className="subtitle">Reproduce, diagnose, verify, and publish UI bug fixes</p>

      <section className="status-card" aria-live="polite">
        <div className="status-row">
          <span className={`dot dot-${backend.kind}`} aria-hidden="true" />
          <h2>
            {backend.kind === 'checking' && 'Checking backend…'}
            {backend.kind === 'connected' && 'Backend connected'}
            {backend.kind === 'unreachable' && 'Backend unreachable'}
          </h2>
        </div>

        {backend.kind === 'connected' && (
          <dl>
            <dt>Service</dt>
            <dd>{backend.service}</dd>
            <dt>Health endpoint</dt>
            <dd>{`${BACKEND_URL}/health`}</dd>
            <dt>Latency</dt>
            <dd>{backend.latencyMs} ms</dd>
          </dl>
        )}

        {backend.kind === 'unreachable' && (
          <p className="detail">
            Could not reach <code>{`${BACKEND_URL}/health`}</code>. {backend.detail}
          </p>
        )}

        <button type="button" onClick={() => void checkBackend()}>
          Retry connection
        </button>
      </section>

      <section className="card">
        <h2>API token</h2>
        <p className="hint">
          Incident and evidence endpoints require the server&apos;s BUGLENS_API_TOKEN. It stays in
          this tab&apos;s session storage and is sent only to the backend above.
        </p>
        <form className="token-form" onSubmit={saveToken}>
          <input
            type="password"
            value={tokenDraft}
            onChange={(e) => setTokenDraft(e.target.value)}
            placeholder="Paste API token"
            autoComplete="off"
            spellCheck={false}
          />
          <button type="submit">Save token</button>
          {token && (
            <button type="button" onClick={clearToken}>
              Clear
            </button>
          )}
        </form>
        {token && backend.kind === 'connected' && <p className="hint">Token saved for this tab.</p>}
      </section>

      {!token ? (
        <p className="hint">Save the API token to report and open incidents.</p>
      ) : selectedId || selected ? (
        <>
          {loading && !selected && <p className="hint">Loading incident…</p>}
          {selected && (
            <IncidentDetail
              token={token}
              incident={selected}
              onChanged={handleChanged}
              onClose={closeIncident}
              initialItems={
                carryover && carryover.incidentId === selected.id ? carryover.items : undefined
              }
            />
          )}
          {error && (
            <p className="error" role="alert">
              {error}{' '}
              <button type="button" onClick={closeIncident}>
                Back to list
              </button>
            </p>
          )}
        </>
      ) : (
        <>
          <NewReportForm token={token} onSaved={handleSaved} />
          {loading && <p className="hint">Loading incidents…</p>}
          {error && (
            <p className="error" role="alert">
              {error}{' '}
              <button type="button" onClick={() => void loadList(token)}>
                Retry
              </button>
            </p>
          )}
          <IncidentList incidents={incidents} selectedId={selectedId} onSelect={selectIncident} />
        </>
      )}
    </main>
  )
}

export default App
