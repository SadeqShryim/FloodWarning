// Top bar: wordmark, connection state, live counters and the demo controls.
import { useEffect, useState } from 'react'
import type { AppConfig, StormState } from '../types'
import { modelLabel } from './format'
import type { ConnectionStatus } from './useLiveReports'

export interface Counts {
  total: number
  criticalOpen: number
  dispatched: number
  processing: number
}

const STATUS_TEXT: Record<ConnectionStatus, string> = {
  live: 'Live',
  polling: 'Polling every 3 s',
  connecting: 'Connecting...',
  reconnecting: 'Reconnecting...',
  offline: 'Server unreachable, retrying',
}

/** Which brain is ranking reports right now, so judges can tell real AI from the keyword fallback. */
function AiBadge({ config }: { config: AppConfig | null }) {
  if (!config) return null
  if (config.ai_enabled) {
    const model = config.ai_model ? modelLabel(config.ai_model) : ''
    return (
      <span className="fl-ai is-on" title={config.ai_model ? `AI model: ${config.ai_model}` : 'Gemini AI is on'}>
        <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
          <path d="M8 0c.6 4.2 3.8 7.4 8 8-4.2.6-7.4 3.8-8 8-.6-4.2-3.8-7.4-8-8 4.2-.6 7.4-3.8 8-8z" fill="currentColor" />
        </svg>
        Gemini{model ? ` ${model}` : ''}
      </span>
    )
  }
  return (
    <span className="fl-ai is-off" title="No Gemini key: typed reports are ranked by keyword rules, voice notes are flagged for review">
      AI offline: keyword rules
    </span>
  )
}

interface HeaderProps {
  counts: Counts
  status: ConnectionStatus
  config: AppConfig | null
  sound: boolean
  onSound: () => void
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
  const { counts, status, config, sound, onSound, storm, stormBusy, onStorm, sitrepBusy, sitrepOpen, onSitrep, qrOpen, onQr, onReset } =
    props
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
        <AiBadge config={config} />
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
        <button
          type="button"
          className={`fl-icon-btn fl-sound-btn${sound ? ' is-on' : ''}`}
          aria-pressed={sound}
          aria-label="Sound for new critical reports"
          title={sound ? 'Sound on for new CRITICAL reports' : 'Sound off'}
          onClick={onSound}
        >
          <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
            <path d="M3 8h3l4-3.5v11L6 12H3z" fill="currentColor" />
            {sound ? (
              <path d="M13 7.5a3.5 3.5 0 010 5M15 5a7 7 0 010 10" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" />
            ) : (
              <path d="M13 8l4 4M17 8l-4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            )}
          </svg>
        </button>
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
