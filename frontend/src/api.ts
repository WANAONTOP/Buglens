import type {
  Evidence,
  Incident,
  IncidentCreatePayload,
  IncidentSummary,
  ReproRun,
  ReproSummary,
} from './types'

export const BACKEND_URL = 'http://127.0.0.1:8000'

const TOKEN_KEY = 'buglens-api-token'

export function getStoredToken(): string {
  return sessionStorage.getItem(TOKEN_KEY) ?? ''
}

export function setStoredToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token)
}

export function clearStoredToken(): void {
  sessionStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

interface ValidationIssue {
  loc?: (string | number)[]
  msg?: string
}

async function parseErrorBody(response: Response): Promise<string> {
  try {
    const body = await response.json()
    const detail = body?.detail as unknown
    if (typeof detail === 'string' && detail) return detail
    if (Array.isArray(detail)) {
      const parts = (detail as ValidationIssue[]).map((issue) => {
        const where = Array.isArray(issue.loc) ? issue.loc.join('.') : ''
        return where ? `${where}: ${issue.msg ?? 'invalid'}` : (issue.msg ?? 'invalid')
      })
      if (parts.length > 0) return parts.join('; ')
    }
  } catch {
    // fall through to the generic message
  }
  return `HTTP ${response.status}`
}

async function request<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BACKEND_URL}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(init?.body && typeof init.body === 'string'
          ? { 'Content-Type': 'application/json' }
          : {}),
        ...(init?.headers ?? {}),
      },
    })
  } catch (err) {
    throw new ApiError(0, err instanceof Error ? err.message : 'Network error')
  }
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorBody(response))
  }
  return (await response.json()) as T
}

export const api = {
  listIncidents(token: string): Promise<IncidentSummary[]> {
    return request(token, '/api/incidents')
  },

  getIncident(token: string, id: string): Promise<Incident> {
    return request(token, `/api/incidents/${id}`)
  },

  createIncident(token: string, payload: IncidentCreatePayload): Promise<Incident> {
    return request(token, '/api/incidents', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  pinCommit(token: string, id: string, commitSha: string): Promise<Incident> {
    return request(token, `/api/incidents/${id}/commit`, {
      method: 'PATCH',
      body: JSON.stringify({ commit_sha: commitSha }),
    })
  },

  startReproduction(token: string, incidentId: string): Promise<ReproSummary> {
    return request(token, `/api/incidents/${incidentId}/reproductions`, { method: 'POST' })
  },

  listReproductions(token: string, incidentId: string): Promise<ReproSummary[]> {
    return request(token, `/api/incidents/${incidentId}/reproductions`)
  },

  getReproduction(token: string, runId: string): Promise<ReproRun> {
    return request(token, `/api/reproductions/${runId}`)
  },

  cancelReproduction(token: string, runId: string): Promise<ReproSummary> {
    return request(token, `/api/reproductions/${runId}/cancel`, { method: 'POST' })
  },

  async downloadReproArtifact(token: string, runId: string, name: string): Promise<Blob> {
    let response: Response
    try {
      response = await fetch(
        `${BACKEND_URL}/api/reproductions/${runId}/artifacts/${encodeURIComponent(name)}/download`,
        { headers: { Authorization: `Bearer ${token}` } },
      )
    } catch (err) {
      throw new ApiError(0, err instanceof Error ? err.message : 'Network error')
    }
    if (!response.ok) {
      throw new ApiError(response.status, await parseErrorBody(response))
    }
    return response.blob()
  },

  async downloadBlob(token: string, incidentId: string, evidenceId: string): Promise<Blob> {
    let response: Response
    try {
      response = await fetch(
        `${BACKEND_URL}/api/incidents/${incidentId}/evidence/${evidenceId}/download`,
        { headers: { Authorization: `Bearer ${token}` } },
      )
    } catch (err) {
      throw new ApiError(0, err instanceof Error ? err.message : 'Network error')
    }
    if (!response.ok) {
      throw new ApiError(response.status, await parseErrorBody(response))
    }
    return response.blob()
  },
}

/** Multipart upload with progress. Uses XHR because fetch has no upload progress. */
export function uploadEvidence(
  token: string,
  incidentId: string,
  form: FormData,
  onProgress: (percent: number | null) => void,
): Promise<Evidence> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${BACKEND_URL}/api/incidents/${incidentId}/evidence`)
    xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.upload.onprogress = (event) => {
      onProgress(event.lengthComputable ? Math.round((event.loaded / event.total) * 100) : null)
    }
    xhr.onload = () => {
      if (xhr.status === 201) {
        try {
          resolve(JSON.parse(xhr.responseText) as Evidence)
        } catch {
          reject(new ApiError(xhr.status, 'Upload succeeded but the response was unreadable.'))
        }
        return
      }
      let message = `HTTP ${xhr.status}`
      try {
        const body = JSON.parse(xhr.responseText)
        if (typeof body?.detail === 'string' && body.detail) message = body.detail
      } catch {
        // keep the generic message
      }
      reject(new ApiError(xhr.status, message))
    }
    xhr.onerror = () => reject(new ApiError(0, 'Network error during upload.'))
    xhr.onabort = () => reject(new ApiError(0, 'Upload cancelled.'))
    onProgress(0)
    xhr.send(form)
  })
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function formatTimestampMs(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  const millis = ms % 1000
  return `${minutes}:${String(seconds).padStart(2, '0')}.${String(millis).padStart(3, '0')}`
}
