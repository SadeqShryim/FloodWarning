// AI sitrep: the incident commander's briefing and the hotspots it found.
import type { Briefing, Hotspot } from '../types'
import { clockTime, levelClass } from './format'

interface SitrepPanelProps {
  briefing: Briefing | null
  busy: boolean
  error: string | null
  onRefresh: () => void
  onClose: () => void
  onHotspot: (hotspot: Hotspot) => void
}

function engineBadge(engine: string): string {
  if (engine === 'rules') return 'Template (AI unavailable)'
  return engine
}

export default function SitrepPanel({ briefing, busy, error, onRefresh, onClose, onHotspot }: SitrepPanelProps) {
  const paragraphs = briefing ? briefing.text.split(/\n{2,}/).filter((p) => p.trim()) : []
  return (
    <section className="fl-sitrep" aria-label="AI situation report" aria-busy={busy}>
      <div className="fl-sitrep-head">
        <h2>Situation report</h2>
        {briefing && (
          <span className={`fl-engine${briefing.engine === 'rules' ? ' is-rules' : ''}`}>{engineBadge(briefing.engine)}</span>
        )}
        <span className="fl-sitrep-spacer" />
        <button type="button" className="fl-link-btn" onClick={onRefresh} disabled={busy}>
          {busy ? 'Writing...' : 'Refresh'}
        </button>
        <button type="button" className="fl-icon-btn" onClick={onClose} aria-label="Close situation report">
          <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
            <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      {error && (
        <p className="fl-error" role="alert">
          {error}
        </p>
      )}
      {!briefing && busy && <p className="fl-muted">Reading every open report and looking for clusters...</p>}
      {!briefing && !busy && !error && <p className="fl-muted">No sitrep yet. Press Refresh to write one.</p>}

      {briefing && (
        <>
          <div className={`fl-sitrep-text${busy ? ' is-stale' : ''}`}>
            {paragraphs.map((p, i) => (
              <p key={i}>{p}</p>
            ))}
          </div>
          {briefing.hotspots.length > 0 && (
            <ul className="fl-hotspots" aria-label="Hotspots">
              {briefing.hotspots.map((h) => (
                <li key={`${h.label}-${h.lat}-${h.lng}`}>
                  <button type="button" onClick={() => onHotspot(h)}>
                    <span className={`fl-hot-dot ${levelClass(h.level)}`} aria-hidden="true" />
                    <span className="fl-hot-name">{h.label}</span>
                    <span className="fl-hot-n">
                      {h.report_ids.length} {h.report_ids.length === 1 ? 'report' : 'reports'}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <p className="fl-sitrep-time">Written at {clockTime(briefing.generated_at)}</p>
        </>
      )}
    </section>
  )
}
