import { useEffect, useRef, type ReactNode } from 'react'

interface Props {
  title: string
  onClose: () => void
  children: ReactNode
  wide?: boolean
}

const FOCUSABLE = 'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])'

/** Accessible modal: focus trap, Escape to close, focus restored on unmount. */
export default function Dialog({ title, onClose, children, wide = false }: Props) {
  const boxRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null
    const box = boxRef.current
    const preferred = box?.querySelector<HTMLElement>('[data-autofocus]')
    const first =
      preferred ?? box?.querySelector<HTMLElement>(FOCUSABLE) ?? (box as HTMLElement | null)
    first?.focus()

    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
        return
      }
      if (event.key !== 'Tab' || !box) return
      const items = Array.from(box.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => !el.hasAttribute('disabled') && el.offsetParent !== null,
      )
      if (items.length === 0) return
      const firstItem = items[0]
      const lastItem = items[items.length - 1]
      if (event.shiftKey && document.activeElement === firstItem) {
        event.preventDefault()
        lastItem.focus()
      } else if (!event.shiftKey && document.activeElement === lastItem) {
        event.preventDefault()
        firstItem.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      previouslyFocused?.focus?.()
    }
  }, [onClose])

  return (
    <div
      className="dialog-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={boxRef}
        className={wide ? 'dialog dialog-wide' : 'dialog'}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
      >
        <div className="dialog-head">
          <h2>{title}</h2>
          <button type="button" className="dialog-close" onClick={onClose} aria-label="Close dialog">
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}
