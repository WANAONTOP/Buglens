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
