import { useEffect, useState } from 'react'
import { api } from './api'

interface Props {
  token: string
  incidentId: string
  intakeStatus: 'blocked' | 'intake_complete'
  /** When provided, skips the verification lookup (list rows share one fetch). */
  verifiedOverride?: boolean | null
}

/**
 * Combined status from real state: a verified result stays visible even when
 * report setup is incomplete. Fails soft to the intake badge alone.
 */
export default function StatusBadges({ token, incidentId, intakeStatus, verifiedOverride }: Props) {
  const [verified, setVerified] = useState<boolean | null>(null)

  useEffect(() => {
    if (verifiedOverride !== undefined) return
    let live = true
    void (async () => {
      try {
        const cands = await api.listCandidates(token, incidentId)
        let found = false
        for (const cand of cands) {
          const verifs = await api.listVerifications(token, cand.id)
          if (verifs.some((v) => v.outcome === 'VERIFIED')) {
            found = true
            break
          }
        }
        if (live) setVerified(found)
      } catch {
        if (live) setVerified(false)
      }
    })()
    return () => {
      live = false
    }
  }, [token, incidentId, verifiedOverride])

  const isVerified = verifiedOverride ?? verified
  return (
    <span className="status-badges">
      {isVerified === true && <span className="badge badge-success">Verification passed</span>}
      {intakeStatus === 'blocked' ? (
        <span className="badge badge-warning">Setup incomplete</span>
      ) : (
        isVerified !== true && <span className="badge badge-info">Intake complete</span>
      )}
    </span>
  )
}
