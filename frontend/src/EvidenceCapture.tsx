import { useEffect, useRef, useState } from 'react'
import { Disclosure } from './Progressive'
import { ApiError, BACKEND_URL, api, formatBytes, formatTimestampMs, uploadEvidence } from './api'
import type { Evidence, EvidenceKind } from './types'

export interface LocalItem {
  uid: string
  kind: EvidenceKind
  blob: Blob
  name: string
  url: string
  width: number | null
  height: number | null
  durationMs: number | null
  timestampMs: number | null
  sourceVideoEvidenceId: string | null
  sourceLabel: string | null
  click: { x: number; y: number } | null
  uploading: boolean
  progress: number | null
  error: string | null
}

/** Upload one local attachment. Shared by single uploads and draft-save flows. */
export async function uploadLocalItem(
  token: string,
  incidentId: string,
  item: LocalItem,
  onProgress: (percent: number | null) => void,
  opts?: { timeoutMs?: number; signal?: AbortSignal },
): Promise<Evidence> {
  const form = new FormData()
  const fallbackType = item.kind === 'video' ? 'video/webm' : 'image/png'
  form.append('file', new File([item.blob], item.name, { type: item.blob.type || fallbackType }))
  form.append('kind', item.kind)
  if (item.timestampMs !== null) form.append('timestamp_ms', String(item.timestampMs))
  if (item.sourceVideoEvidenceId) form.append('source_video_evidence_id', item.sourceVideoEvidenceId)
  if (item.click) {
    form.append('clicked_x', String(item.click.x))
    form.append('clicked_y', String(item.click.y))
  }
  if (item.width !== null && item.height !== null) {
    form.append('client_width', String(item.width))
    form.append('client_height', String(item.height))
  }
  if (item.durationMs !== null) form.append('client_duration_ms', String(item.durationMs))
  return uploadEvidence(token, incidentId, form, onProgress, opts)
}

interface Props {
  token: string
  /** Null on the new-report screen: items stay local until the draft is saved. */
  incidentId: string | null
  uploaded: Evidence[]
  onChanged: () => void
  /** Local items re-mounted after a draft save (unmounted child's URLs are reminted). */
  initialItems?: LocalItem[]
  /** Reports local items upward so the draft screen can upload them after create. */
  onPendingChange?: (items: LocalItem[]) => void
  /** Compact single-attachment-area layout for the new-report wizard. */
  compact?: boolean
}

type Preview = { type: 'local'; uid: string } | { type: 'uploaded'; evidenceId: string } | null

interface ActiveRecording {
  stream: MediaStream
  recorder: MediaRecorder
  startedAt: number
}

let uidCounter = 0

function nextUid(): string {
  uidCounter += 1
  return `${Date.now()}-${uidCounter}`
}

function stopTracks(stream: MediaStream): void {
  stream.getTracks().forEach((track) => {
    try {
      track.stop()
    } catch {
      // already stopped
    }
  })
}

function pickRecorderMime(): string | undefined {
  if (typeof window.MediaRecorder === 'undefined') return undefined
  const candidates = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm']
  for (const candidate of candidates) {
    try {
      if (window.MediaRecorder.isTypeSupported(candidate)) return candidate
    } catch {
      // ignore and try the next candidate
    }
  }
  return undefined
}

function loadImageSize(url: string): Promise<{ width: number; height: number }> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve({ width: img.naturalWidth, height: img.naturalHeight })
    img.onerror = () => reject(new Error('Could not decode the image.'))
    img.src = url
  })
}

function loadVideoMeta(url: string): Promise<{ width: number; height: number; durationMs: number | null }> {
  return new Promise((resolve, reject) => {
    const video = document.createElement('video')
    video.preload = 'metadata'
    video.onloadedmetadata = () =>
      resolve({
        width: video.videoWidth,
        height: video.videoHeight,
        durationMs: Number.isFinite(video.duration) ? Math.round(video.duration * 1000) : null,
      })
    video.onerror = () => reject(new Error('Could not decode the video.'))
    video.src = url
  })
}

export default function EvidenceCapture({
  token,
  incidentId,
  uploaded,
  onChanged,
  initialItems,
  onPendingChange,
  compact = false,
}: Props) {
  // Carryover items arrive with URLs revoked by the unmounted draft panel,
  // so each mount mints fresh object URLs from the surviving blobs.
  const [items, setItems] = useState<LocalItem[]>(() =>
    (initialItems ?? []).map((item) => ({ ...item, url: URL.createObjectURL(item.blob) })),
  )
  const [preview, setPreview] = useState<Preview>(null)
  const [uploadedUrls, setUploadedUrls] = useState<Record<string, string>>({})
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [attachError, setAttachError] = useState<string | null>(null)
  const [recording, setRecording] = useState<ActiveRecording | null>(null)
  const [elapsedMs, setElapsedMs] = useState(0)
  const [recorderError, setRecorderError] = useState<string | null>(null)
  const [finalizing, setFinalizing] = useState(false)
  const [lastUploaded, setLastUploaded] = useState<string | null>(null)
  const [videoClock, setVideoClock] = useState('0:00.000')

  const videoRef = useRef<HTMLVideoElement | null>(null)
  const liveUrls = useRef<Set<string>>(new Set())
  const recordingRef = useRef<ActiveRecording | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const discardRef = useRef(false)
  const uploadControllers = useRef(new Map<string, AbortController>())

  recordingRef.current = recording

  // Release every object URL and stop any live capture when unmounting.
  useEffect(() => {
    const urls = liveUrls.current
    return () => {
      if (recordingRef.current) {
        try {
          if (recordingRef.current.recorder.state !== 'inactive') {
            recordingRef.current.recorder.stop()
          }
        } catch {
          // ignore — we are tearing down
        }
        stopTracks(recordingRef.current.stream)
      }
      urls.forEach((url) => URL.revokeObjectURL(url))
      urls.clear()
    }
  }, [])

  // Recording timer.
  useEffect(() => {
    if (!recording) return
    setElapsedMs(0)
    const timer = window.setInterval(() => setElapsedMs(Date.now() - recording.startedAt), 250)
    return () => window.clearInterval(timer)
  }, [recording])

  // Track carryover URLs (minted fresh above) so they are released on unmount.
  useEffect(() => {
    items.forEach((item) => liveUrls.current.add(item.url))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Report local items upward for the draft-save flow.
  useEffect(() => {
    onPendingChange?.(items)
  }, [items, onPendingChange])

  function trackUrl(url: string): void {
    liveUrls.current.add(url)
  }

  function untrackUrl(url: string): void {
    liveUrls.current.delete(url)
    URL.revokeObjectURL(url)
  }

  function patchItem(uid: string, patch: Partial<LocalItem>): void {
    setItems((prev) => prev.map((item) => (item.uid === uid ? { ...item, ...patch } : item)))
  }

  async function addLocalImage(file: File): Promise<void> {
    if (!file.type.startsWith('image/')) {
      setAttachError(`"${file.name}" is not an image file.`)
      return
    }
    const url = URL.createObjectURL(file)
    trackUrl(url)
    const item: LocalItem = {
      uid: nextUid(),
      kind: 'screenshot',
      blob: file,
      name: file.name,
      url,
      width: null,
      height: null,
      durationMs: null,
      timestampMs: null,
      sourceVideoEvidenceId: null,
      sourceLabel: null,
      click: null,
      uploading: false,
      progress: null,
      error: null,
    }
    setItems((prev) => [...prev, item])
    setPreview({ type: 'local', uid: item.uid })
    try {
      const size = await loadImageSize(url)
      patchItem(item.uid, { width: size.width, height: size.height })
    } catch (err) {
      patchItem(item.uid, { error: err instanceof Error ? err.message : String(err) })
    }
  }

  async function addLocalVideo(blob: Blob, name: string, sourceLabel: string | null): Promise<void> {
    const url = URL.createObjectURL(blob)
    trackUrl(url)
    const item: LocalItem = {
      uid: nextUid(),
      kind: 'video',
      blob,
      name,
      url,
      width: null,
      height: null,
      durationMs: null,
      timestampMs: null,
      sourceVideoEvidenceId: null,
      sourceLabel,
      click: null,
      uploading: false,
      progress: null,
      error: null,
    }
    setItems((prev) => [...prev, item])
    setPreview({ type: 'local', uid: item.uid })
    try {
      const meta = await loadVideoMeta(url)
      patchItem(item.uid, { width: meta.width, height: meta.height, durationMs: meta.durationMs })
    } catch (err) {
      patchItem(item.uid, { error: err instanceof Error ? err.message : String(err) })
    }
  }

  function handleFiles(kind: EvidenceKind, fileList: FileList | null): void {
    setAttachError(null)
    if (!fileList) return
    for (const file of Array.from(fileList)) {
      if (kind === 'screenshot') {
        void addLocalImage(file)
      } else if (file.type.startsWith('video/')) {
        void addLocalVideo(file, file.name, null)
      } else {
        setAttachError(`"${file.name}" is not a video file.`)
      }
    }
  }

  async function startRecording(): Promise<void> {
    setRecorderError(null)
    if (!navigator.mediaDevices?.getDisplayMedia) {
      setRecorderError('Screen capture is not supported in this browser.')
      return
    }
    if (typeof window.MediaRecorder === 'undefined') {
      setRecorderError('MediaRecorder is not supported in this browser.')
      return
    }
    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: { ideal: 30 } },
        audio: false,
      })
    } catch (err) {
      if (err instanceof DOMException && err.name === 'NotAllowedError') {
        setRecorderError('Screen sharing was cancelled or permission was denied.')
      } else {
        setRecorderError(
          `Could not start screen capture: ${err instanceof Error ? err.message : String(err)}`,
        )
      }
      return
    }
    const mimeType = pickRecorderMime()
    let recorder: MediaRecorder
    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
    } catch (err) {
      stopTracks(stream)
      setRecorderError(
        `Could not start the recorder: ${err instanceof Error ? err.message : String(err)}`,
      )
      return
    }
    chunksRef.current = []
    recorder.ondataavailable = (event: BlobEvent) => {
      if (event.data.size > 0) chunksRef.current.push(event.data)
    }
    recorder.onerror = () => setRecorderError('Recording failed with a recorder error.')
    recorder.onstop = () => {
      stopTracks(stream)
      const discard = discardRef.current
      discardRef.current = false
      setRecording(null)
      setFinalizing(false)
      if (discard) return
      const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'video/webm' })
      if (blob.size === 0) {
        setRecorderError('Recording produced no data.')
        return
      }
      const stamp = new Date().toISOString().replace(/[:.]/g, '-')
      void addLocalVideo(blob, `screen-recording-${stamp}.webm`, 'Screen recording')
    }
    const [track] = stream.getVideoTracks()
    // The browser's own "Stop sharing" button ends the track directly.
    track?.addEventListener('ended', () => {
      if (recorder.state !== 'inactive') {
        setFinalizing(true)
        recorder.stop()
      }
    })
    recorder.start()
    setRecording({ stream, recorder, startedAt: Date.now() })
  }

  function stopRecording(): void {
    const active = recordingRef.current
    if (active && active.recorder.state !== 'inactive') {
      setFinalizing(true)
      active.recorder.stop()
    }
  }

  function cancelRecording(): void {
    const active = recordingRef.current
    if (!active) return
    discardRef.current = true
    if (active.recorder.state !== 'inactive') {
      active.recorder.stop()
    } else {
      stopTracks(active.stream)
      discardRef.current = false
      setRecording(null)
      setFinalizing(false)
    }
  }

  function removeItem(item: LocalItem): void {
    untrackUrl(item.url)
    setItems((prev) => prev.filter((candidate) => candidate.uid !== item.uid))
    setPreview((current) => (current?.type === 'local' && current.uid === item.uid ? null : current))
  }

  async function uploadItem(item: LocalItem): Promise<void> {
    if (incidentId === null) return
    const controller = new AbortController()
    uploadControllers.current.set(item.uid, controller)
    patchItem(item.uid, { uploading: true, progress: 0, error: null })
    try {
      await uploadLocalItem(token, incidentId, item, (percent) =>
        patchItem(item.uid, { progress: percent }),
        { timeoutMs: 120_000, signal: controller.signal },
      )
      untrackUrl(item.url)
      setItems((prev) => prev.filter((candidate) => candidate.uid !== item.uid))
      setPreview((current) => (current?.type === 'local' && current.uid === item.uid ? null : current))
      // Shown only after the server confirms with 201: the item moves to the
      // uploaded list on the refresh below.
      setLastUploaded(`✓ “${item.name}” uploaded to the backend.`)
      onChanged()
    } catch (err) {
      if (controller.signal.aborted) {
        patchItem(item.uid, {
          uploading: false,
          progress: null,
          error: 'Upload cancelled. The file is kept below — Upload to retry.',
        })
      } else {
        const message =
          err instanceof ApiError
            ? `Upload failed (${err.status}): ${err.message}${err.status === 0 ? ` Could not reach ${BACKEND_URL}.` : ''} The file is kept below — Upload to retry.`
            : String(err)
        patchItem(item.uid, { uploading: false, progress: null, error: message })
      }
    } finally {
      uploadControllers.current.delete(item.uid)
    }
  }

  function cancelUpload(uid: string): void {
    uploadControllers.current.get(uid)?.abort()
  }

  function itemStatus(item: LocalItem): string {
    if (item.uploading) return 'Uploading…'
    if (item.error) return 'Needs attention — see the error below.'
    if (item.width === null || item.height === null) return 'Preparing…'
    return incidentId === null ? 'Ready — uploads when you save.' : 'Ready to upload.'
  }

  function handleScreenshotClick(event: React.MouseEvent<HTMLImageElement>, item: LocalItem): void {
    if (item.width === null || item.height === null) return
    const rect = event.currentTarget.getBoundingClientRect()
    if (rect.width === 0 || rect.height === 0) return
    const x = Math.round(((event.clientX - rect.left) / rect.width) * item.width)
    const y = Math.round(((event.clientY - rect.top) / rect.height) * item.height)
    patchItem(item.uid, {
      click: {
        x: Math.max(0, Math.min(item.width - 1, x)),
        y: Math.max(0, Math.min(item.height - 1, y)),
      },
    })
  }

  function captureFrame(): void {
    const video = videoRef.current
    setPreviewError(null)
    if (!video || video.videoWidth === 0 || video.readyState < 2) {
      setPreviewError('No video frame is ready to capture yet.')
      return
    }
    const canvas = document.createElement('canvas')
    canvas.width = video.videoWidth
    canvas.height = video.videoHeight
    const context = canvas.getContext('2d')
    if (!context) {
      setPreviewError('Frame capture is not available in this browser.')
      return
    }
    context.drawImage(video, 0, 0)
    const timestampMs = Math.round(video.currentTime * 1000)
    let sourceVideoEvidenceId: string | null = null
    let sourceLabel: string | null = null
    if (preview?.type === 'uploaded') {
      const source = uploaded.find((candidate) => candidate.id === preview.evidenceId)
      if (!source || source.kind !== 'video') {
        setPreviewError('Frames can only be captured from video evidence.')
        return
      }
      sourceVideoEvidenceId = source.id
      sourceLabel = source.original_filename
    } else if (preview?.type === 'local') {
      const source = items.find((candidate) => candidate.uid === preview.uid)
      if (!source || source.kind !== 'video') {
        setPreviewError('Frames can only be captured from video evidence.')
        return
      }
      sourceLabel = source.name
    } else {
      setPreviewError('Open a video preview before capturing a frame.')
      return
    }
    canvas.toBlob((blob) => {
      if (!blob) {
        setPreviewError('Frame capture failed.')
        return
      }
      const url = URL.createObjectURL(blob)
      trackUrl(url)
      const stamp = new Date().toISOString().replace(/[:.]/g, '-')
      const item: LocalItem = {
        uid: nextUid(),
        kind: 'video_frame',
        blob,
        name: `frame-${formatTimestampMs(timestampMs).replace(/[:.]/g, '-')}-${stamp}.png`,
        url,
        width: canvas.width,
        height: canvas.height,
        durationMs: null,
        timestampMs,
        sourceVideoEvidenceId,
        sourceLabel,
        click: null,
        uploading: false,
        progress: null,
        error: null,
      }
      setItems((prev) => [...prev, item])
      setPreview({ type: 'local', uid: item.uid })
    }, 'image/png')
  }

  async function ensureUploadedUrl(evidence: Evidence): Promise<string | null> {
    if (incidentId === null) return null
    const cached = uploadedUrls[evidence.id]
    if (cached) return cached
    setPreviewLoading(true)
    setPreviewError(null)
    try {
      const blob = await api.downloadBlob(token, incidentId, evidence.id)
      const url = URL.createObjectURL(blob)
      trackUrl(url)
      setUploadedUrls((prev) => ({ ...prev, [evidence.id]: url }))
      return url
    } catch (err) {
      setPreviewError(
        err instanceof ApiError ? `Preview failed (${err.status}): ${err.message}` : String(err),
      )
      return null
    } finally {
      setPreviewLoading(false)
    }
  }

  function openUploadedPreview(evidence: Evidence): void {
    setPreview({ type: 'uploaded', evidenceId: evidence.id })
    setVideoClock('0:00.000')
    void ensureUploadedUrl(evidence)
  }

  async function downloadUploaded(evidence: Evidence): Promise<void> {
    const url = await ensureUploadedUrl(evidence)
    if (!url) return
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = evidence.original_filename
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  }

  function openLocalPreview(item: LocalItem): void {
    setPreview({ type: 'local', uid: item.uid })
    setVideoClock('0:00.000')
  }

  const previewLocal = preview?.type === 'local' ? items.find((item) => item.uid === preview.uid) ?? null : null
  const previewEvidence =
    preview?.type === 'uploaded' ? uploaded.find((item) => item.id === preview.evidenceId) ?? null : null
  const previewEvidenceUrl = previewEvidence ? (uploadedUrls[previewEvidence.id] ?? null) : null

  function describeDims(width: number | null, height: number | null): string {
    return width !== null && height !== null ? `${width}×${height}` : 'dimensions unknown'
  }

  const filePickers = (
    <>
      <label className="file-pick" tabIndex={0} onKeyDown={openPicker}>
        Attach screenshot
        <input
          type="file"
          accept="image/png,image/jpeg,image/webp"
          multiple
          onChange={(event) => {
            handleFiles('screenshot', event.target.files)
            event.target.value = ''
          }}
        />
      </label>
      <label className="file-pick" tabIndex={0} onKeyDown={openPicker}>
        Attach video
        <input
          type="file"
          accept="video/mp4,video/webm"
          multiple
          onChange={(event) => {
            handleFiles('video', event.target.files)
            event.target.value = ''
          }}
        />
      </label>
    </>
  )

  const recordControls = !recording ? (
    <button type="button" onClick={() => void startRecording()}>
      Record screen
    </button>
  ) : (
    <>
      <button type="button" className="recording-active" onClick={stopRecording}>
        <span className="rec-dot live" aria-hidden="true" /> Stop ({formatTimestampMs(elapsedMs)})
      </button>
      <button type="button" onClick={cancelRecording}>
        Cancel
      </button>
    </>
  )

  function openPicker(event: React.KeyboardEvent): void {
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    event.currentTarget.querySelector('input')?.click()
  }

  return (
    <div className="evidence">
      {!compact && <h3>Evidence</h3>}

      {compact ? (
        <>
          <h4>Add a screenshot or video</h4>
          <div className="capture-row">{filePickers}</div>
          <p className="attach-divider">Or record your screen</p>
          <div className="capture-row">{recordControls}</div>
          <p className="capture-limits">
            PNG, JPEG, WebP up to 10 MB each · MP4, WebM up to 100 MB each.
          </p>
          <Disclosure title="Detailed limits">
            <p className="hint">
              At most 20 evidence items per report
              {incidentId !== null && ` (${uploaded.length + items.length} of 20 used)`}. Files are
              validated by content on the server; oversized or unsupported uploads are rejected
              with an error and can be retried.
            </p>
          </Disclosure>
        </>
      ) : (
        <>
          <div className="capture-row">
            {filePickers}
            {recordControls}
          </div>
          <p className="capture-limits">
            Screenshots: PNG, JPEG, or WebP, up to 10 MiB each. Videos: MP4 or WebM, up to 100 MiB
            each. At most 20 evidence items per incident
            {incidentId !== null && ` (${uploaded.length + items.length} of 20 used)`}.
          </p>
        </>
      )}
      {attachError && (
        <p className="error" role="alert">
          {attachError}
        </p>
      )}
      {recorderError && (
        <p className="error" role="alert">
          {recorderError}
        </p>
      )}
      {recording && (
        <p className="hint">Recording… use the browser&apos;s sharing picker, then Stop here or in the browser chrome.</p>
      )}
      {finalizing && (
        <p className="hint" role="status">
          Finalizing recording…
        </p>
      )}
      {lastUploaded && (
        <p className="hint" role="status">
          {lastUploaded}
        </p>
      )}

      {compact ? (
        <h4>Attachments{items.length > 0 && ` (${items.length})`}</h4>
      ) : (
        <h4>
          On this device — not uploaded yet <span className="badge badge-local">Local</span>
        </h4>
      )}
      {items.length === 0 ? (
        compact ? (
          <p className="hint">No attachments yet. Anything you add uploads when you save.</p>
        ) : (
          <p className="hint">Nothing attached. Screenshots, videos, recordings, and captured frames land here first.</p>
        )
      ) : (
        <ul className="evidence-list">
          {items.map((item) => (
            <li key={item.uid} className="evidence-item">
              <button type="button" className="evidence-name" onClick={() => openLocalPreview(item)}>
                {item.name}
              </button>
              <span className="evidence-meta">
                {item.kind.replace('_', ' ')} · {describeDims(item.width, item.height)} ·{' '}
                {formatBytes(item.blob.size)}
                {item.timestampMs !== null && ` · @ ${formatTimestampMs(item.timestampMs)}`}
                {item.click && ` · click @ ${item.click.x},${item.click.y}`}
              </span>
              <span className="item-status" role="status">
                {itemStatus(item)}
              </span>
              {item.uploading ? (
                <>
                  <span className="progress">
                    <span
                      className="progress-bar"
                      style={{ width: `${item.progress ?? 0}%` }}
                      aria-hidden="true"
                    />
                    <span className="progress-label">
                      {item.progress === null ? 'Uploading…' : `${item.progress}%`}
                    </span>
                  </span>
                  <span className="evidence-actions">
                    <button type="button" onClick={() => cancelUpload(item.uid)}>
                      Cancel upload
                    </button>
                  </span>
                </>
              ) : incidentId === null ? (
                <span className="evidence-actions">
                  <button type="button" onClick={() => removeItem(item)}>
                    Remove
                  </button>
                  <span className="hint">Uploads when you save.</span>
                </span>
              ) : (
                <span className="evidence-actions">
                  <button type="button" onClick={() => void uploadItem(item)}>
                    Upload
                  </button>
                  <button type="button" onClick={() => removeItem(item)}>
                    Discard
                  </button>
                </span>
              )}
              {item.error && (
                <p className="error" role="alert">
                  {item.error}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}

      {(previewLocal || previewEvidence) && (
        <div className="preview-pane">
          <h4>Preview</h4>
          {previewLocal && previewLocal.kind !== 'video' && (
            <div className="image-frame">
              <img
                src={previewLocal.url}
                alt={previewLocal.name}
                className={previewLocal.kind === 'screenshot' ? 'clickable' : undefined}
                onClick={
                  previewLocal.kind === 'screenshot'
                    ? (event) => handleScreenshotClick(event, previewLocal)
                    : undefined
                }
              />
              {previewLocal.click && previewLocal.width !== null && previewLocal.height !== null && (
                <span
                  className="click-marker"
                  style={{
                    left: `${(previewLocal.click.x / previewLocal.width) * 100}%`,
                    top: `${(previewLocal.click.y / previewLocal.height) * 100}%`,
                  }}
                  aria-hidden="true"
                />
              )}
            </div>
          )}
          {previewLocal && previewLocal.kind === 'video' && (
            <video
              key={previewLocal.uid}
              ref={videoRef}
              src={previewLocal.url}
              controls
              onTimeUpdate={(event) =>
                setVideoClock(formatTimestampMs(Math.round(event.currentTarget.currentTime * 1000)))
              }
            />
          )}
          {previewEvidence && !previewEvidenceUrl && (
            <p className="hint">{previewLoading ? 'Loading preview…' : 'Preview unavailable.'}</p>
          )}
          {previewEvidence && previewEvidenceUrl && previewEvidence.kind !== 'video' && (
            <div className="image-frame">
              <img src={previewEvidenceUrl} alt={previewEvidence.original_filename} />
              {previewEvidence.clicked_point &&
                previewEvidence.width !== null &&
                previewEvidence.height !== null && (
                  <span
                    className="click-marker readonly"
                    style={{
                      left: `${(previewEvidence.clicked_point.x / previewEvidence.width) * 100}%`,
                      top: `${(previewEvidence.clicked_point.y / previewEvidence.height) * 100}%`,
                    }}
                    aria-hidden="true"
                  />
                )}
            </div>
          )}
          {previewEvidence && previewEvidenceUrl && previewEvidence.kind === 'video' && (
            <video
              key={previewEvidence.id}
              ref={videoRef}
              src={previewEvidenceUrl}
              controls
              onTimeUpdate={(event) =>
                setVideoClock(formatTimestampMs(Math.round(event.currentTarget.currentTime * 1000)))
              }
            />
          )}
          <div className="preview-meta">
            {previewLocal && (
              <span>
                {previewLocal.name} · {describeDims(previewLocal.width, previewLocal.height)} ·{' '}
                {formatBytes(previewLocal.blob.size)}
                {previewLocal.kind === 'screenshot' && ' · click the image to mark a point'}
                {previewLocal.timestampMs !== null &&
                  ` · frame @ ${formatTimestampMs(previewLocal.timestampMs)} from ${previewLocal.sourceLabel ?? 'video'}`}
              </span>
            )}
            {previewEvidence && (
              <span>
                {previewEvidence.original_filename} ·{' '}
                {describeDims(previewEvidence.width, previewEvidence.height)} ·{' '}
                {formatBytes(previewEvidence.size_bytes)}
                {previewEvidence.timestamp_ms !== null &&
                  ` · frame @ ${formatTimestampMs(previewEvidence.timestamp_ms)}`}
                {previewEvidence.clicked_point &&
                  ` · click @ ${previewEvidence.clicked_point.x},${previewEvidence.clicked_point.y}`}
              </span>
            )}
          </div>
          {((previewLocal && previewLocal.kind === 'video') ||
            (previewEvidence && previewEvidence.kind === 'video' && previewEvidenceUrl)) && (
            <div className="frame-row">
              <span className="video-clock" aria-live="off">
                {videoClock}
              </span>
              <button type="button" onClick={captureFrame}>
                Capture frame at current position
              </button>
            </div>
          )}
          {previewLocal?.click && (
            <button type="button" onClick={() => patchItem(previewLocal.uid, { click: null })}>
              Clear marked point
            </button>
          )}
          {previewError && (
            <p className="error" role="alert">
              {previewError}
            </p>
          )}
        </div>
      )}

      {incidentId !== null && (
        <>
          <h4>
            Uploaded to backend <span className="badge badge-uploaded">Uploaded</span>
          </h4>
          {uploaded.length === 0 ? (
            <p className="hint">No uploaded evidence yet.</p>
          ) : (
        <ul className="evidence-list">
          {uploaded.map((evidence) => (
            <li key={evidence.id} className="evidence-item">
              <span className="evidence-name">{evidence.original_filename}</span>
              <span className="evidence-meta">
                {evidence.kind.replace('_', ' ')} · {describeDims(evidence.width, evidence.height)} ·{' '}
                {formatBytes(evidence.size_bytes)}
                {evidence.duration_ms !== null && ` · ${formatTimestampMs(evidence.duration_ms)}`}
                {evidence.timestamp_ms !== null && ` · frame @ ${formatTimestampMs(evidence.timestamp_ms)}`}
                {evidence.clicked_point && ` · click @ ${evidence.clicked_point.x},${evidence.clicked_point.y}`}
              </span>
              <span className="evidence-actions">
                <button type="button" onClick={() => openUploadedPreview(evidence)}>
                  Preview
                </button>
                <button type="button" onClick={() => void downloadUploaded(evidence)}>
                  Download
                </button>
              </span>
            </li>
          ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}
