import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'
import {
  ApiError,
  BACKEND_URL,
  api,
  clearStoredToken,
  getStoredToken,
  onUnauthorized,
  setStoredToken,
} from './api'
import type { Incident, IncidentSummary } from './types'
import ConnectDialog from './ConnectDialog'
import SettingsDialog from './SettingsDialog'
import NewReportWizard from './NewReportWizard'
import IncidentList from './IncidentList'
import IncidentDetail from './IncidentDetail'
import type { LocalItem } from './EvidenceCapture'

type BackendState =
  | { kind: 'checking' }
  | { kind: 'connected'; service: string; latencyMs: number }
  | { kind: 'unreachable'; detail: string }

type View = 'reports' | 'investigation'

const INCIDENT_HASH = /^#incident=([0-9a-f]{32})$/

function incidentIdFromHash(): string | null {
  const match = INCIDENT_HASH.exec(window.location.hash)
  return match ? match[1] : null
}

function App() {
  const [backend, setBackend] = useState<BackendState>({ kind: 'checking' })
  const [token, setToken] = useState(getStoredToken())
  const [connectOpen, setConnectOpen] = useState(() => getStoredToken().length === 0)
  const [connectNotice, setConnectNotice] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [wizardOpen, setWizardOpen] = useState(false)
  const [view, setView] = useState<View>(() => (incidentIdFromHash() ? 'investigation' : 'reports'))
  const [navNotice, setNavNotice] = useState<string | null>(null)
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
      setView('investigation')
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
        setView('investigation')
        void loadSelected(token, hashed)
      } else {
        setSelected(null)
        setView('reports')
      }
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [token, loadSelected, selectedId])

  // Mirror for the global 401 handler, which outlives render closures.
  const tokenRef = useRef(token)
  tokenRef.current = token

  function clearProtectedState(): void {
    setIncidents([])
    setSelectedId(null)
    setSelected(null)
    setCarryover(null)
    setError(null)
  }

  // Invalid/expired credentials: drop the session, clear protected data, and
  // reopen the connect dialog. Network failures (status 0) never land here.
  useEffect(
    () =>
      onUnauthorized(() => {
        if (!tokenRef.current) return
        clearStoredToken()
        tokenRef.current = ''
        setToken('')
        clearProtectedState()
        setSettingsOpen(false)
        setWizardOpen(false)
        setConnectNotice('The session was rejected by the backend (401). Sign in again.')
        setConnectOpen(true)
      }),
    [],
  )

  function handleConnected(nextToken: string): void {
    setStoredToken(nextToken)
    setToken(nextToken)
    setConnectNotice(null)
    setConnectOpen(false)
  }

  function clearToken(): void {
    clearStoredToken()
    setToken('')
    clearProtectedState()
    setWizardOpen(false)
    setView('reports')
    window.location.hash = ''
  }

  function selectIncident(id: string): void {
    window.location.hash = `incident=${id}`
    setCarryover(null)
    setSelectedId(id)
    setSelected(null)
    setView('investigation')
    setNavNotice(null)
    void loadSelected(token, id)
  }

  function closeIncident(): void {
    window.location.hash = ''
    setCarryover(null)
    setSelectedId(null)
    setSelected(null)
    setView('reports')
  }

  function goToReports(): void {
    setNavNotice(null)
    closeIncident()
  }

  function goToInvestigation(): void {
    if (selectedId) {
      setNavNotice(null)
      setView('investigation')
      if (!selected) void loadSelected(token, selectedId)
      return
    }
    setNavNotice('Select a report first — the Investigation page shows one open report.')
  }

  function handleSaved(incident: Incident, leftoverItems: LocalItem[]): void {
    setWizardOpen(false)
    void loadList(token)
    selectIncident(incident.id)
    setCarryover(leftoverItems.length > 0 ? { incidentId: incident.id, items: leftoverItems } : null)
  }

  function handleChanged(incident: Incident): void {
    setSelected(incident)
    void loadList(token)
  }

  function openConnect(): void {
    setConnectNotice(null)
    setConnectOpen(true)
  }

  const investigating = view === 'investigation' && (selectedId !== null || selected !== null)

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <button type="button" className="brand" onClick={goToReports} aria-label="BugLens home">
            <span className="brand-icon" aria-hidden="true">
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <circle cx="7" cy="7" r="4" />
                <path d="M10.2 10.2L14 14" />
              </svg>
            </span>
            <span className="wordmark">BugLens</span>
            <span className="ai-badge">AI</span>
          </button>
          {token && (
            <nav className="topnav" aria-label="Pages">
              <button
                type="button"
                className={view === 'reports' ? 'nav-btn nav-active' : 'nav-btn'}
                aria-current={view === 'reports' ? 'page' : undefined}
                onClick={goToReports}
              >
                Reports
              </button>
              <button
                type="button"
                className={view === 'investigation' ? 'nav-btn nav-active' : 'nav-btn'}
                aria-current={view === 'investigation' ? 'page' : undefined}
                onClick={goToInvestigation}
              >
                Investigation
              </button>
            </nav>
          )}
          <div className="topbar-spacer" />
          <div className="status-pill" aria-live="polite">
            <span className={`dot dot-${backend.kind}`} aria-hidden="true" />
            <span>
              {backend.kind === 'checking' && 'Checking…'}
              {backend.kind === 'connected' && token && 'Connected'}
              {backend.kind === 'connected' && !token && 'Backend OK'}
              {backend.kind === 'unreachable' && 'Offline'}
            </span>
          </div>
          <div className="topbar-actions">
            {token ? (
              <button type="button" onClick={() => setSettingsOpen(true)}>
                Settings
              </button>
            ) : (
              <button type="button" className="btn-primary" onClick={openConnect}>
                Connect
              </button>
            )}
          </div>
        </div>
      </header>

      <main className="page">
        {connectOpen && (
          <ConnectDialog
            notice={connectNotice}
            onConnected={handleConnected}
            onClose={() => setConnectOpen(false)}
          />
        )}
        {settingsOpen && token && (
          <SettingsDialog
            backend={backend}
            onRetryBackend={() => void checkBackend()}
            onSaveToken={handleConnected}
            onSignOut={clearToken}
            onClose={() => setSettingsOpen(false)}
          />
        )}
        {wizardOpen && token && (
          <NewReportWizard
            token={token}
            onSaved={handleSaved}
            onClose={() => setWizardOpen(false)}
          />
        )}

        {!token ? (
          <section className="card">
            <h2>Find and fix UI bugs</h2>
            <p className="hint">Capture the issue. Review the diagnosis. Verify the fix.</p>
            <button type="button" className="btn btn-primary" onClick={openConnect}>
              Connect to workspace
            </button>
          </section>
        ) : investigating ? (
          <>
            {loading && !selected && <p className="hint">Loading report…</p>}
            {selected && (
              <IncidentDetail
                key={selected.id}
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
                  Back to reports
                </button>
              </p>
            )}
          </>
        ) : (
          <>
            <div className="hero">
              <div>
                <h2>Find and fix UI bugs</h2>
                <p>Capture the issue. Review the diagnosis. Verify the fix.</p>
              </div>
              <button type="button" className="btn btn-primary" onClick={() => setWizardOpen(true)}>
                + New report
              </button>
            </div>
            {navNotice && (
              <p className="hint" role="status">
                {navNotice}
              </p>
            )}
            {loading && <p className="hint">Loading reports…</p>}
            {error && (
              <p className="error" role="alert">
                {error}{' '}
                <button type="button" onClick={() => void loadList(token)}>
                  Retry
                </button>
              </p>
            )}
            <IncidentList
              token={token}
              incidents={incidents}
              selectedId={selectedId}
              onSelect={selectIncident}
            />
          </>
        )}
      </main>
    </>
  )
}

export default App
