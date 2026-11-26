import { useId, useState, type ReactNode } from 'react'

// Buttons, not hash links: the app router owns `location.hash`.
export function scrollToPanel(id: string): void {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  document.getElementById(id)?.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth' })
}

/** Expandable details block; keeps clutter one click away. */
export function Disclosure({
  title,
  count,
  defaultOpen = false,
  children,
}: {
  title: string
  count?: number
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  const id = useId()
  return (
    <div className="disclosure">
      <button
        type="button"
        className="disclosure-head"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((prev) => !prev)}
      >
        <span>
          {title}
          {count !== undefined && ` (${count})`}
        </span>
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div id={id} className="disclosure-body">
          {children}
        </div>
      )}
    </div>
  )
}

/** Short hash with a one-click full reveal. Short values render as-is. */
export function HashValue({ value, label }: { value: string; label: string }) {
  const [open, setOpen] = useState(false)
  if (value.length <= 12) return <span className="mono">{value}</span>
  return open ? (
    <span className="hash-full">
      <span className="mono">{value}</span>{' '}
      <button type="button" className="link-btn" onClick={() => setOpen(false)}>
        hide
      </button>
    </span>
  ) : (
    <span>
      <span className="mono">{value.slice(0, 12)}…</span>{' '}
      <button
        type="button"
        className="link-btn"
        onClick={() => setOpen(true)}
        aria-label={`Show full ${label}`}
      >
        show full
      </button>
    </span>
  )
}
