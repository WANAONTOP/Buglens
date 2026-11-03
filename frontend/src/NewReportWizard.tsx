import { useCallback, useRef, useState } from 'react'
import Dialog from './Dialog'
import { Disclosure } from './Progressive'
import { ApiError, api } from './api'
import type { Incident } from './types'
import EvidenceCapture, { uploadLocalItem, type LocalItem } from './EvidenceCapture'

interface Props {
  token: string
  onSaved: (incident: Incident, leftoverItems: LocalItem[]) => void
  onClose: () => void
}

const STEPS = ['Evidence', 'Describe', 'Repository'] as const

function splitLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
}

const REPO_PATTERN = /^[^/\s]+\/[^/\s]+$/

export default function NewReportWizard({ token, onSaved, onClose }: Props) {
  const [step, setStep] = useState(0)
  const [title, setTitle] = useState('')
  const [observed, setObserved] = useState('')
  const [expected, setExpected] = useState('')
  const [stepsText, setStepsText] = useState('')
  const [repo, setRepo] = useState('')
  const [ref, setRef] = useState('')
  const [commitSha, setCommitSha] = useState('')
  const [viewport, setViewport] = useState('')
  const [pathsText, setPathsText] = useState('')
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pendingCount, setPendingCount] = useState(0)

  const pendingRef = useRef<LocalItem[]>([])
  const savingRef = useRef(false)
  const handlePendingChange = useCallback((items: LocalItem[]) => {
    pendingRef.current = items
    setPendingCount(items.length)
  }, [])

  const dirty =
    title.trim() !== '' ||
    observed.trim() !== '' ||
    expected.trim() !== '' ||
    stepsText.trim() !== '' ||
    repo.trim() !== '' ||
    ref.trim() !== '' ||
    commitSha.trim() !== '' ||
    viewport.trim() !== '' ||
    pathsText.trim() !== '' ||
    pendingRef.current.length > 0

  function tryClose(): void {
    if (saving) return
    if (dirty && !window.confirm('Discard this unsaved report? Evidence and text will be lost.')) {
      return
    }
    onClose()
  }

  function nextFromDescribe(): void {
    setError(null)
    if (!title.trim() || !observed.trim() || !expected.trim()) {
      setError('Title, observed behavior, and expected behavior are required.')
      return
    }
    if (splitLines(stepsText).length === 0) {
      setError('Add at least one reproduction step. The backend requires one or more steps.')
      return
    }
    setStep(2)
  }

  function nextFromRepository(): void {
    setError(null)
    if (repo.trim() !== '' && !REPO_PATTERN.test(repo.trim())) {
      setError("Repository must look like 'owner/name' with no spaces.")
      return
    }
    void handleSave()
  }

  async function handleSave(): Promise<void> {
    // Ref guard: the disabled-button state update is async, so a fast
    // double-click could otherwise create two incidents.
    if (savingRef.current) return
    savingRef.current = true
    setSaving(true)
    setError(null)
    setStatus('Creating report…')
    let incident: Incident
    try {
      const trimmedRepo = repo.trim()
      const trimmedRef = ref.trim()
      const trimmedSha = commitSha.trim()
      const trimmedViewport = viewport.trim()
      incident = await api.createIncident(token, {
        title: title.trim(),
        observed_behavior: observed.trim(),
        expected_behavior: expected.trim(),
        reproduction_steps: splitLines(stepsText),
        ...(trimmedRepo ? { repo: trimmedRepo } : {}),
        ...(trimmedRef ? { ref: trimmedRef } : {}),
        ...(trimmedSha ? { commit_sha: trimmedSha } : {}),
        ...(trimmedViewport ? { browser_viewport: trimmedViewport } : {}),
        permitted_source_paths: splitLines(pathsText),
      })
    } catch (err) {
      setError(
        err instanceof ApiError ? `Save failed (${err.status}): ${err.message}` : String(err),
      )
      setSaving(false)
      setStatus(null)
      savingRef.current = false
      return
    }

    const pending = pendingRef.current
    const leftovers: LocalItem[] = []
    for (let index = 0; index < pending.length; index++) {
      const item = pending[index]
      setStatus(`Uploading evidence ${index + 1} of ${pending.length}…`)
      try {
        await uploadLocalItem(token, incident.id, item, () => {})
      } catch (err) {
        leftovers.push({
          ...item,
          uploading: false,
          progress: null,
          error:
            err instanceof ApiError
              ? `Upload failed (${err.status}): ${err.message}`
              : String(err),
        })
      }
    }
    setSaving(false)
    setStatus(null)
    onSaved(incident, leftovers)
  }

  return (
    <Dialog title="New report" onClose={tryClose} wide>
      <ol className="wizard-steps" aria-label="Report steps">
        {STEPS.map((label, index) => (
          <li
            key={label}
            className={`wizard-step${index < step ? ' wizard-done' : ''}`}
            aria-current={index === step ? 'step' : undefined}
          >
            <span className="step-num" aria-hidden="true">
              {index + 1}
            </span>
            {label}
          </li>
        ))}
      </ol>

      <div className="wizard-body">
        {/* All steps stay mounted (hidden) so attachments and recordings survive Back/Next. */}
        <div hidden={step !== 0}>
          <>
            <p className="hint">Files you attach here upload when you save.</p>
            <EvidenceCapture
              token={token}
              incidentId={null}
              uploaded={[]}
              onChanged={() => {}}
              onPendingChange={handlePendingChange}
              compact
            />
          </>
        </div>

        <div hidden={step !== 1}>
          <div className="form">
            <label>
              <span className="label-title">Title</span>
              <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
            </label>
            <label>
              <span className="label-title">Observed behavior</span>
              <textarea value={observed} onChange={(e) => setObserved(e.target.value)} rows={3} />
            </label>
            <label>
              <span className="label-title">Expected behavior</span>
              <textarea value={expected} onChange={(e) => setExpected(e.target.value)} rows={3} />
            </label>
            <label>
              <span className="label-title">Reproduction steps</span>
              <span className="label-hint">One step per line. At least one step is required.</span>
              <textarea
                value={stepsText}
                onChange={(e) => setStepsText(e.target.value)}
                rows={4}
                placeholder={'Open the page\nClick the button'}
              />
            </label>
          </div>
        </div>

        <div hidden={step !== 2}>
          <div className="form">
            <p className="hint">
              Repository details are optional for a draft. Investigation later requires a
              repository, ref, pinned commit, and permitted source paths.
            </p>
            <div className="form-row">
              <label>
                <span className="label-title">GitHub repository</span>
                <input
                  value={repo}
                  onChange={(e) => setRepo(e.target.value)}
                  placeholder="owner/name"
                />
              </label>
              <label>
                <span className="label-title">Branch / ref</span>
                <input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="main" />
              </label>
            </div>
            <Disclosure title="Advanced settings">
              <div className="form">
                <label>
                  <span className="label-title">Pinned commit SHA</span>
                  <span className="label-hint">Full 40-character hex SHA. Never invented.</span>
                  <input
                    value={commitSha}
                    onChange={(e) => setCommitSha(e.target.value)}
                    placeholder="40-character hex SHA"
                    spellCheck={false}
                  />
                </label>
                <label>
                  <span className="label-title">Browser viewport</span>
                  <span className="input-with-button">
                    <input
                      value={viewport}
                      onChange={(e) => setViewport(e.target.value)}
                      placeholder="1440x900"
                    />
                    <button
                      type="button"
                      onClick={() => setViewport(`${window.innerWidth}x${window.innerHeight}`)}
                    >
                      Use current window
                    </button>
                  </span>
                </label>
                <label>
                  <span className="label-title">Permitted source paths</span>
                  <span className="label-hint">
                    One per line, relative to the repo root. Only these files may ever change.
                  </span>
                  <textarea
                    value={pathsText}
                    onChange={(e) => setPathsText(e.target.value)}
                    rows={2}
                    placeholder={'src/nav.tsx'}
                  />
                </label>
              </div>
            </Disclosure>
          </div>
        </div>
      </div>

      {status && <p className="hint">{status}</p>}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <div className="wizard-nav">
        <span>
          {step > 0 && (
            <button type="button" onClick={() => setStep(step - 1)} disabled={saving}>
              ← Back
            </button>
          )}
        </span>
        <span>
          {step === 0 && (
            <button type="button" className="btn-primary" onClick={() => setStep(1)} disabled={saving}>
              {pendingCount === 0 ? 'Continue without evidence' : `Next →`}
            </button>
          )}
          {step === 1 && (
            <button type="button" className="btn-primary" onClick={nextFromDescribe} disabled={saving}>
              Next →
            </button>
          )}
          {step === 2 && (
            <button type="button" className="btn-primary" onClick={nextFromRepository} disabled={saving}>
              {saving ? 'Saving…' : 'Save report'}
            </button>
          )}
        </span>
      </div>
      <p className="hint">Saving stores the report for review. It does not fix the bug.</p>
    </Dialog>
  )
}
