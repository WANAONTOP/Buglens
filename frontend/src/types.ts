export type EvidenceKind = 'screenshot' | 'video' | 'video_frame'

export interface ClickedPoint {
  x: number
  y: number
}

export interface Evidence {
  id: string
  incident_id: string
  kind: EvidenceKind
  original_filename: string
  stored_filename: string
  content_type: string
  size_bytes: number
  sha256: string
  width: number | null
  height: number | null
  duration_ms: number | null
  timestamp_ms: number | null
  source_video_evidence_id: string | null
  clicked_point: ClickedPoint | null
  created_at: string
}

export interface Incident {
  id: string
  title: string
  observed_behavior: string
  expected_behavior: string
  reproduction_steps: string[]
  repo: string | null
  ref: string | null
  commit_sha: string | null
  browser_viewport: string | null
  permitted_source_paths: string[]
  created_at: string
  updated_at: string
  evidence: Evidence[]
  intake_status: 'blocked' | 'intake_complete'
  blockers: string[]
  investigation_blockers: string[]
}

export interface IncidentSummary {
  id: string
  title: string
  repo: string | null
  ref: string | null
  commit_sha: string | null
  intake_status: 'blocked' | 'intake_complete'
  blockers: string[]
  evidence_count: number
  created_at: string
  updated_at: string
}

export type ReproState = 'queued' | 'running' | 'done' | 'cancelled' | 'error'

export type ReproVerdict = 'REPRODUCED' | 'NEEDS_REPRODUCTION' | 'BLOCKED'

export interface ReproEvent {
  at: string
  phase: string
  message: string
}

export interface ReproArtifact {
  name: string
  stored_filename: string
  content_type: string
  size_bytes: number
  sha256: string
}

export interface ReproRun {
  id: string
  incident_id: string
  repo: string
  commit_sha: string
  contract_hash: string
  state: ReproState
  verdict: ReproVerdict | null
  detail: string
  events: ReproEvent[]
  artifacts: ReproArtifact[]
  created_at: string
  updated_at: string
}

export interface ReproSummary {
  id: string
  incident_id: string
  repo: string
  commit_sha: string
  contract_hash: string
  state: ReproState
  verdict: ReproVerdict | null
  detail: string
  artifact_count: number
  created_at: string
  updated_at: string
}

export type InvestigationState = 'queued' | 'running' | 'done' | 'blocked' | 'cancelled' | 'error'

export interface EvidenceItem {
  ref: string
  origin: 'uploaded' | 'baseline' | 'broken' | 'control'
  kind: string
  route: string | null
  artifact: string | null
  summary: string
  data: Record<string, unknown>
}

export interface ModelCall {
  model: string
  status: string
  latency_ms: number
  attempts: number
  usage: Record<string, unknown> | null
}

export interface SparkResult {
  call: ModelCall
  frames: string[]
  observations: string[]
  hypotheses: string[]
  raw_text: string
  parsed: boolean
  parse_note: string
}

export interface SamBox {
  object_id: number
  x1: number
  y1: number
  x2: number
  y2: number
  frame_w: number
  frame_h: number
  mask_h: number
  mask_w: number
}

export interface SamResult {
  call: ModelCall
  concept: string
  frame_ref: string
  frame_artifact: string
  frame_w: number
  frame_h: number
  matches: SamBox[]
  note: string
}

export interface SourceMapping {
  target: string
  file: string | null
  start_line: number | null
  end_line: number | null
  certainty: 'exact' | 'inferred' | 'unknown'
  evidence_refs: string[]
  note: string
}

export interface WonderHypothesis {
  rank: number
  cause: string
  mechanism: string
  supporting_evidence: string[]
  conflicting_or_missing: string[]
  locations: SourceMapping[]
  repair_direction: string
  unvalidated_citations: string[]
}

export interface WonderResult {
  call: ModelCall
  hypotheses: WonderHypothesis[]
  raw_text: string
  parsed: boolean
  parse_note: string
}

export interface InvestigationEvent {
  at: string
  phase: string
  message: string
}

export interface Investigation {
  id: string
  incident_id: string
  run_id: string
  repo: string
  commit_sha: string
  contract_hash: string
  state: InvestigationState
  detail: string
  evidence: EvidenceItem[]
  absent_evidence: string[]
  spark: SparkResult | null
  sam: SamResult | null
  mappings: SourceMapping[]
  wonder: WonderResult | null
  events: InvestigationEvent[]
  created_at: string
  updated_at: string
}

export interface InvestigationSummary {
  id: string
  incident_id: string
  run_id: string
  commit_sha: string
  state: InvestigationState
  detail: string
  hypothesis_count: number
  created_at: string
  updated_at: string
}

export type CandidateState =
  | 'queued'
  | 'generating'
  | 'proposed'
  | 'approved'
  | 'rejected'
  | 'blocked'
  | 'error'

export interface ValidationCheck {
  name: string
  ok: boolean
  detail: string
}

export interface ValidationResult {
  ok: boolean
  checks: ValidationCheck[]
  reasons: string[]
}

export interface ChangeExplanation {
  path: string
  explanation: string
}

export interface Approval {
  candidate_id: string
  diff_sha256: string
  approved_at: string
  note: string
}

export interface Candidate {
  id: string
  incident_id: string
  run_id: string
  investigation_id: string
  hypothesis_rank: number
  repo: string
  baseline_sha: string
  contract_hash: string
  state: CandidateState
  detail: string
  diff: string
  diff_sha256: string
  changed_paths: string[]
  explanations: ChangeExplanation[]
  evidence_refs: string[]
  expected_effect: string
  regression_risks: string[]
  provider: ModelCall | null
  raw_text: string
  parsed: boolean
  parse_note: string
  validation: ValidationResult | null
  approval: Approval | null
  events: InvestigationEvent[]
  created_at: string
  updated_at: string
}

export interface CandidateSummary {
  id: string
  incident_id: string
  investigation_id: string
  baseline_sha: string
  state: CandidateState
  detail: string
  changed_paths: string[]
  created_at: string
  updated_at: string
}

export type VerificationState = 'queued' | 'running' | 'done' | 'blocked' | 'error'

export type GateVerdict = 'PASS' | 'FAIL' | 'BLOCKED'

export type VerificationOutcome = 'VERIFIED' | 'NOT_VERIFIED' | 'BLOCKED'

export interface GateResult {
  gate: number
  name: string
  verdict: GateVerdict
  detail: string
  measurements: Record<string, unknown>
  artifacts: string[]
}

export interface VerifyArtifact {
  name: string
  stored_filename: string
  content_type: string
  size_bytes: number
  sha256: string
}

export interface Verification {
  id: string
  incident_id: string
  run_id: string
  investigation_id: string
  candidate_id: string
  repo: string
  contract_hash: string
  policy_hash: string
  approval: {
    candidate_id: string
    diff_sha256: string
    approved_at: string
  } | null
  state: VerificationState
  outcome: VerificationOutcome | null
  detail: string
  gates: GateResult[]
  tested: {
    diff_sha256: string
    file_hashes: Record<string, string>
    baseline_sha: string
  } | null
  artifacts: VerifyArtifact[]
  events: InvestigationEvent[]
  created_at: string
  updated_at: string
}

export interface VerificationSummary {
  id: string
  incident_id: string
  candidate_id: string
  state: VerificationState
  outcome: VerificationOutcome | null
  detail: string
  gates_passed: number
  created_at: string
  updated_at: string
}

export interface PreflightCheck {
  name: string
  ok: boolean
  detail: string
}

export interface Publication {
  id: string
  incident_id: string
  candidate_id: string
  verification_id: string
  repo: string
  base_branch: string
  base_sha: string
  branch: string
  pr_title: string
  state: 'publishing' | 'published' | 'failed'
  detail: string
  commit_sha: string
  pr_number: number
  pr_url: string
  preflight: PreflightCheck[]
  events: InvestigationEvent[]
  created_at: string
  updated_at: string
}

export interface PublicationPreview {
  repo: string
  base_branch: string
  base_sha: string
  branch: string
  changed_files: string[]
  diff: string
  diff_sha256: string
  pr_title: string
  pr_body: string
  verification_id: string
  candidate_id: string
  preflight: PreflightCheck[]
  preflight_ok: boolean
}

export interface IncidentCreatePayload {
  title: string
  observed_behavior: string
  expected_behavior: string
  reproduction_steps: string[]
  repo?: string
  ref?: string
  commit_sha?: string
  browser_viewport?: string
  permitted_source_paths: string[]
}
