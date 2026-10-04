// "New CRITICAL" toasts. Clicking one selects the report; each fades out on its own.
import { useEffect } from 'react'

export interface Toast {
  key: number
  reportId: number
  summary: string
  place: string
}

// Short enough that a storm's stream of CRITICALs never buries the map; the queue keeps them all.
const TOAST_MS = 7000

interface ToastsProps {
  toasts: Toast[]
  onOpen: (reportId: number) => void
  onDismiss: (key: number) => void
}

function ToastItem({ toast, onOpen, onDismiss }: { toast: Toast; onOpen: (id: number) => void; onDismiss: (key: number) => void }) {
  useEffect(() => {
    const t = window.setTimeout(() => onDismiss(toast.key), TOAST_MS)
    return () => window.clearTimeout(t)
  }, [toast.key, onDismiss])

  return (
    <li className="fl-toast">
      <button
        type="button"
        className="fl-toast-body"
        onClick={() => {
          onOpen(toast.reportId)
          onDismiss(toast.key)
        }}
      >
        <span className="fl-toast-kicker">New CRITICAL</span>
        <span className="fl-toast-text" dir="auto">
          {toast.summary}, {toast.place}
        </span>
      </button>
      <button type="button" className="fl-icon-btn" aria-label="Dismiss" onClick={() => onDismiss(toast.key)}>
        <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true">
          <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      </button>
    </li>
  )
}

export default function Toasts({ toasts, onOpen, onDismiss }: ToastsProps) {
  return (
    <ol className="fl-toasts" aria-live="assertive" aria-label="Alerts">
      {toasts.map((t) => (
        <ToastItem key={t.key} toast={t} onOpen={onOpen} onDismiss={onDismiss} />
      ))}
    </ol>
  )
}
