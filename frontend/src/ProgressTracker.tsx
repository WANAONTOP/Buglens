import { useCallback, useEffect, useState } from 'react'
import { scrollToPanel } from './Progressive'
import { ApiError, api } from './api'
import type {
  CandidateSummary,
  Incident,
  InvestigationSummary,
  Publication,
  ReproSummary,
  VerificationSummary,
} from './types'

type StageStatus = 'complete' | 'running' | 'blocked' | 'failed' | 'todo'

interface Stage {
  key: string
  label: string
  anchor: string
  status: StageStatus
  /** True while a backend job is actively queued/running (pulses the dot). */
  live: boolean
  detail: string
  prUrl?: string
}

interface Props {
  token: string
  incident: Incident
  /** Where the "next" button goes; defaults to scrolling to the panel anchor. */
  onNavigate?: (anchor: string) => void
}

function shortId(value: string): string {
  return value.length > 8 ? value.slice(0, 8) : value
}

function latest<T extends { created_at: string }>(items: T[]): T | null {
  if (items.length === 0) return null
  return items.reduce((a, b) => (a.created_at >= b.created_at ? a : b))
}

function buildStages(
  incident: Incident,
  repros: ReproSummary[] | null,
  invs: InvestigationSummary[] | null,
  cands: CandidateSummary[] | null,
  verifs: VerificationSummary[] | null,
  pubs: Publication[] | null,
): Stage[] {
  const evidenceCount = incident.evidence.length
  const evidence: Stage =
    evidenceCount > 0
      ? {
          key: 'evidence',
          label: 'Evidence',
          anchor: 'evidence',
          status: 'complete',
          live: false,
          detail: `${evidenceCount} uploaded`,
        }
      : {
          key: 'evidence',
          label: 'Evidence',
          anchor: 'evidence',
          status: 'todo',
          live: false,
          detail: 'nothing uploaded yet',
        }

  let reproduce: Stage = {
    key: 'reproduce',
    label: 'Reproduce',
    anchor: 'reproduce',
    status: 'todo',
    live: false,
    detail: 'no runs yet',
  }
  const activeRepro = (repros ?? []).find((r) => r.state === 'queued' || r.state === 'running')
  const lastRepro = latest(repros ?? [])
  if (activeRepro) {
    reproduce = { ...reproduce, status: 'running', live: true, detail: `${activeRepro.state} · ${shortId(activeRepro.id)}` }
  } else if (lastRepro) {
    if (lastRepro.state === 'done' && lastRepro.verdict === 'REPRODUCED') {
      reproduce = { ...reproduce, status: 'complete', detail: `REPRODUCED · ${shortId(lastRepro.id)}` }
    } else if (lastRepro.state === 'done' && lastRepro.verdict === 'BLOCKED') {
      reproduce = { ...reproduce, status: 'blocked', detail: `BLOCKED · ${shortId(lastRepro.id)}` }
    } else if (lastRepro.state === 'done') {
      reproduce = { ...reproduce, status: 'failed', detail: `NEEDS_REPRODUCTION · ${shortId(lastRepro.id)}` }
    } else if (lastRepro.state === 'error') {
      reproduce = { ...reproduce, status: 'failed', detail: `error · ${shortId(lastRepro.id)}` }
    } else {
      reproduce = { ...reproduce, status: 'blocked', detail: `${lastRepro.state} · ${shortId(lastRepro.id)}` }
    }
  }

  let diagnose: Stage = {
    key: 'diagnose',
    label: 'Diagnose',
    anchor: 'diagnose',
    status: 'todo',
    live: false,
    detail: 'no investigations yet',
  }
  const activeInv = (invs ?? []).find((i) => i.state === 'queued' || i.state === 'running')
  const lastInv = latest(invs ?? [])
  if (activeInv) {
    diagnose = { ...diagnose, status: 'running', live: true, detail: `${activeInv.state} · ${shortId(activeInv.id)}` }
  } else if (lastInv) {
    if (lastInv.state === 'done' && lastInv.hypothesis_count > 0) {
      diagnose = {
        ...diagnose,
        status: 'complete',
        detail: `${lastInv.hypothesis_count} ${lastInv.hypothesis_count === 1 ? 'hypothesis' : 'hypotheses'}`,
      }
    } else if (lastInv.state === 'done') {
      diagnose = { ...diagnose, status: 'failed', detail: `done, no hypotheses · ${shortId(lastInv.id)}` }
    } else if (lastInv.state === 'blocked' || lastInv.state === 'cancelled') {
      diagnose = { ...diagnose, status: 'blocked', detail: `${lastInv.state} · ${shortId(lastInv.id)}` }
    } else {
      diagnose = { ...diagnose, status: 'failed', detail: `${lastInv.state} · ${shortId(lastInv.id)}` }
    }
  }

  let patch: Stage = {
    key: 'patch',
    label: 'Patch',
    anchor: 'patch',
    status: 'todo',
    live: false,
    detail: 'no candidates yet',
  }
  const activeCand = (cands ?? []).find((c) => c.state === 'queued' || c.state === 'generating')
  const approved = latest((cands ?? []).filter((c) => c.state === 'approved'))
  const proposed = latest((cands ?? []).filter((c) => c.state === 'proposed'))
  const lastCand = latest(cands ?? [])
  if (activeCand) {
    patch = { ...patch, status: 'running', live: true, detail: `${activeCand.state} · ${shortId(activeCand.id)}` }
  } else if (approved) {
    patch = { ...patch, status: 'complete', detail: `approved · ${shortId(approved.id)}` }
  } else if (proposed) {
    patch = { ...patch, status: 'running', live: false, detail: `proposed · awaiting approval` }
  } else if (lastCand) {
    if (lastCand.state === 'rejected' || lastCand.state === 'error') {
      patch = { ...patch, status: 'failed', detail: `${lastCand.state} · ${shortId(lastCand.id)}` }
    } else {
      patch = { ...patch, status: 'blocked', detail: `${lastCand.state} · ${shortId(lastCand.id)}` }
    }
  }

  let verify: Stage = {
    key: 'verify',
    label: 'Verify',
    anchor: 'verify',
    status: 'todo',
    live: false,
    detail: 'no verification runs yet',
  }
  const activeVerif = (verifs ?? []).find((v) => v.state === 'queued' || v.state === 'running')
  const lastVerif = latest(verifs ?? [])
  if (activeVerif) {
    verify = { ...verify, status: 'running', live: true, detail: `${activeVerif.state} · ${shortId(activeVerif.id)}` }
  } else if (lastVerif) {
    if (lastVerif.outcome === 'VERIFIED') {
      verify = { ...verify, status: 'complete', detail: `VERIFIED · ${lastVerif.gates_passed}/7 gates` }
    } else if (lastVerif.outcome === 'NOT_VERIFIED') {
      verify = { ...verify, status: 'failed', detail: `NOT_VERIFIED · ${lastVerif.gates_passed}/7 gates` }
    } else if (lastVerif.outcome === 'BLOCKED' || lastVerif.state === 'blocked') {
      verify = { ...verify, status: 'blocked', detail: `BLOCKED · ${shortId(lastVerif.id)}` }
    } else {
      verify = { ...verify, status: 'failed', detail: `${lastVerif.state} · ${shortId(lastVerif.id)}` }
    }
  }

  let pr: Stage = {
    key: 'pr',
    label: 'Pull request',
    anchor: 'publish',
    status: 'todo',
    live: false,
    detail: 'not published',
  }
  const activePub = (pubs ?? []).find((p) => p.state === 'publishing')
  const published = latest((pubs ?? []).filter((p) => p.state === 'published'))
  const lastPub = latest(pubs ?? [])
  if (activePub) {
    pr = { ...pr, status: 'running', live: true, detail: `publishing · ${activePub.branch}` }
  } else if (published && published.pr_url) {
    pr = {
      ...pr,
      status: 'complete',
      detail: published.pr_number > 0 ? `PR #${published.pr_number}` : 'published',
      prUrl: published.pr_url,
    }
  } else if (lastPub) {
    pr = { ...pr, status: 'failed', detail: `${lastPub.state} · ${shortId(lastPub.id)}` }
  }

  return [evidence, reproduce, diagnose, patch, verify, pr]
}

function nextAction(stages: Stage[]): { text: string; anchor: string } | null {
  const byKey = Object.fromEntries(stages.map((s) => [s.key, s]))
  if (byKey.evidence.status !== 'complete') {
    return { text: 'Attach a screenshot, video, or screen recording', anchor: 'evidence' }
  }
  if (byKey.reproduce.status !== 'complete') {
    return byKey.reproduce.status === 'running'
      ? { text: 'Reproduction is running — follow its progress', anchor: 'reproduce' }
      : { text: 'Reproduce the bug in Modal', anchor: 'reproduce' }
  }
  if (byKey.diagnose.status !== 'complete') {
    return byKey.diagnose.status === 'running'
      ? { text: 'Investigation is running — follow its progress', anchor: 'diagnose' }
      : { text: 'Investigate the reproduced run', anchor: 'diagnose' }
  }
  if (byKey.patch.status !== 'complete') {
    if (byKey.patch.detail.includes('awaiting approval')) {
      return { text: 'Review the candidate diff and approve or reject it', anchor: 'patch' }
    }
    return byKey.patch.status === 'running'
      ? { text: 'Candidate generation is running — follow its progress', anchor: 'patch' }
      : { text: 'Generate a candidate patch', anchor: 'patch' }
  }
  if (byKey.verify.status !== 'complete') {
    return byKey.verify.status === 'running'
      ? { text: 'Verification is running — follow the gates', anchor: 'verify' }
      : { text: 'Verify the approved candidate', anchor: 'verify' }
  }
  if (byKey.pr.status !== 'complete') {
    return { text: 'Preview and publish the verified repair', anchor: 'publish' }
  }
  return null
}

export default function ProgressTracker({ token, incident, onNavigate }: Props) {
  const go = onNavigate ?? scrollToPanel
  const [repros, setRepros] = useState<ReproSummary[] | null>(null)
  const [invs, setInvs] = useState<InvestigationSummary[] | null>(null)
  const [cands, setCands] = useState<CandidateSummary[] | null>(null)
  const [verifs, setVerifs] = useState<VerificationSummary[] | null>(null)
  const [pubs, setPubs] = useState<Publication[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [fetchedRepros, fetchedInvs, fetchedCands] = await Promise.all([
        api.listReproductions(token, incident.id),
        api.listInvestigations(token, incident.id),
        api.listCandidates(token, incident.id),
      ])
      setRepros(fetchedRepros)
      setInvs(fetchedInvs)
      setCands(fetchedCands)
      const newest = latest(fetchedCands)
      if (newest) {
        const [fetchedVerifs, fetchedPubs] = await Promise.all([
          api.listVerifications(token, newest.id),
          api.listPublications(token, newest.id),
        ])
        setVerifs(fetchedVerifs)
        setPubs(fetchedPubs)
      } else {
        setVerifs([])
        setPubs([])
      }
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? `Tracker refresh failed (${err.status}): ${err.message}` : String(err))
    }
  }, [token, incident.id])

  useEffect(() => {
    setRepros(null)
    setInvs(null)
    setCands(null)
    setVerifs(null)
    setPubs(null)
    setError(null)
    void refresh()
  }, [refresh, incident.updated_at])

  const stages = buildStages(incident, repros, invs, cands, verifs, pubs)
  const action = nextAction(stages)
  const currentKey = action ? stages.find((s) => s.anchor === action.anchor)?.key : stages[stages.length - 1].key
  const anyLive = stages.some((s) => s.live)

  // Re-poll while a job is active so the tracker follows real state changes.
  useEffect(() => {
    if (!anyLive) return
    const timer = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(timer)
  }, [anyLive, refresh])

  const loading = repros === null || invs === null || cands === null

  return (
    <div>
      <ol className="tracker" aria-label="Incident progress">
        {stages.map((stage) => (
          <li
            key={stage.key}
            className="tracker-step"
            aria-current={stage.key === currentKey ? 'step' : undefined}
          >
            <span className="tracker-head">
              <span
                className={`status-dot status-${stage.status}${stage.live ? ' pulse' : ''}`}
                aria-hidden="true"
              />
              {stage.label}
            </span>
            <span className="tracker-detail">
              {loading ? 'loading…' : stage.detail}
              {stage.prUrl && (
                <>
                  {' · '}
                  <a href={stage.prUrl} target="_blank" rel="noreferrer">
                    open PR
                  </a>
                </>
              )}
            </span>
          </li>
        ))}
      </ol>
      {action && !loading && (
        <p className="next-action">
          <strong>Next:</strong> {action.text}{' '}
          <button type="button" onClick={() => go(action.anchor)}>
            Go to {action.anchor}
          </button>
        </p>
      )}
      {!action && !loading && (
        <p className="next-action">
          <strong>Complete:</strong> evidence, reproduction, diagnosis, patch, verification, and PR
          are all recorded above.
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}{' '}
          <button type="button" onClick={() => void refresh()}>
            Retry
          </button>
        </p>
      )}
    </div>
  )
}
