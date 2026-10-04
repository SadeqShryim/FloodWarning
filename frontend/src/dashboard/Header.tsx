// Top bar: wordmark, connection state, live counters and the demo controls.
import { useEffect, useState } from 'react'
import type { LiveStatus } from '../api'
import type { StormState } from '../types'

export interface Counts {
  total: number
  criticalOpen: number
  dispatched: number
  processing: number
}

const STATUS_TEXT: Record<LiveStatus, string> = {
  live: 'Live',
  polling: 'Polling every 3 s',
  connecting: 'Reconnecting...',
}

interface HeaderProps {
  counts: Counts
  status: LiveStatus
  storm: StormState
  stormBusy: boolean
  onStorm: () => void
  sitrepBusy: boolean
  sitrepOpen: boolean
  onSitrep: () => void
  qrOpen: boolean
  onQr: () => void
  onReset: () => Promise<void>
}

export default function Header(props: HeaderProps) {
  const { counts, status, storm, stormBusy, onStorm, sitrepBusy, sitrepOpen, onSitrep, qrOpen, onQr, onReset } = props
  return (
    <header className={`fl-head${storm.running ? ' is-storm' : ''}`}>
      <div className="fl-brand">
        <span className="fl-wordmark" aria-label="FloodLine">
          Flood<span className="fl-wordmark-line">Line</span>
        </span>
        <span className={`fl-live is-${status}`} role="status" aria-live="polite">
          <span className="fl-live-dot" aria-hidden="true" />
          {STATUS_TEXT[status]}
        </span>
      </div>

      <dl className="fl-counters">
        <div>
          <dt>Total</dt>
          <dd>{counts.total}</dd>
        </div>
        <div className={counts.criticalOpen > 0 ? 'is-critical' : undefined}>
          <dt>Critical open</dt>
          <dd>{counts.criticalOpen}</dd>
        </div>
        <div>
          <dt>Dispatched</dt>
          <dd>{counts.dispatched}</dd>
        </div>
        <div className={counts.processing > 0 ? 'is-busy' : undefined}>
          <dt>Processing</dt>
          <dd>{counts.processing}</dd>
        </div>
      </dl>

      <div className="fl-tools">
        <button
          type="button"
          className={`fl-btn fl-storm-btn${storm.running ? ' is-on' : ''}`}
          aria-pressed={storm.running}
          disabled={stormBusy}
          onClick={onStorm}
        >
          {storm.running ? (
            <>
              <span className="fl-storm-label">Storm mode</span>
              <span className="fl-storm-n">{storm.injected} injected</span>
              <span className="fl-storm-stop">Stop</span>
            </>
          ) : (
            'Start storm'
          )}
        </button>
        <button type="button" className="fl-btn" aria-pressed={sitrepOpen} disabled={sitrepBusy} onClick={onSitrep}>
          {sitrepBusy ? 'Writing sitrep...' : 'AI sitrep'}
        </button>
        <button type="button" className="fl-btn" aria-pressed={qrOpen} onClick={onQr}>
          Phone QR
        </button>
        <ResetButton onReset={onReset} />
      </div>
    </header>
  )
}

/** Reset with an inline "are you sure" that cancels itself after a few seconds. */
function ResetButton({ onReset }: { onReset: () => Promise<void> }) {
  const [state, setState] = useState<'idle' | 'confirm' | 'busy' | 'error'>('idle')

  useEffect(() => {
    if (state !== 'confirm' && state !== 'error') return
    const t = window.setTimeout(() => setState('idle'), state === 'confirm' ? 6000 : 4000)
    return () => window.clearTimeout(t)
  }, [state])

  const confirm = async () => {
    setState('busy')
    try {
      await onReset()
      setState('idle')
    } catch {
      setState('error')
    }
  }

  if (state === 'confirm' || state === 'busy') {
    return (
      <span className="fl-confirm" role="group" aria-label="Confirm demo reset">
        <span className="fl-confirm-q">Clear all reports and reseed?</span>
        <button type="button" className="fl-btn is-danger" disabled={state === 'busy'} onClick={confirm} autoFocus>
          {state === 'busy' ? 'Resetting...' : 'Reset'}
        </button>
        <button type="button" className="fl-btn is-ghost" disabled={state === 'busy'} onClick={() => setState('idle')}>
          Cancel
        </button>
      </span>
    )
  }
  return (
    <button type="button" className="fl-btn is-ghost" onClick={() => setState('confirm')}>
      {state === 'error' ? 'Reset failed, try again' : 'Reset demo'}
    </button>
  )
}
