// Detail drawer for one report: what was said, what the AI understood, and the responder actions.
import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { api } from '../api'
import type { Report, ReportStatus } from '../types'
import {
  clockTime,
  engineLabel,
  flagList,
  formatDepth,
  hasCoords,
  isPending,
  languageLabel,
  levelClass,
  locationSourceLabel,
  locationTypeLabel,
  needsReview,
  placeLabel,
  relativeTime,
  reportLanguage,
  statusLabel,
} from './format'

const HAZARD_LABELS = { electrical: 'Electrical', sewage: 'Sewage', gas: 'Gas', structural: 'Structural' }
const PEOPLE_LABELS = { elderly: 'Elderly', children: 'Children', disabled: 'Disabled', medical: 'Medical', trapped: 'Trapped' }
const NEEDS_LABELS = { evacuation: 'Evacuation', pumping: 'Pumping', medical: 'Medical', supplies: 'Supplies' }

function yesNo(value: boolean): string {
  return value ? 'Yes' : 'No'
}

function listOrNone(items: string[]): string {
  return items.length ? items.join(', ') : 'None reported'
}

interface DrawerProps {
  report: Report
  now: number
  onClose: () => void
  onUpdated: (report: Report) => void
  /** Move the selection to the next (+1) or previous (-1) report in queue order. */
  onStep: (delta: 1 | -1) => void
}

type Busy = ReportStatus | 'reprocess' | null

export default function Drawer({ report, now, onClose, onUpdated, onStep }: DrawerProps) {
  // The parent keys this component by report id, so these start fresh for each report.
  const [busy, setBusy] = useState<Busy>(null)
  const [error, setError] = useState<string | null>(null)
  const rootRef = useRef<HTMLElement>(null)

  // The drawer covers the queue, so keyboard focus moves into it (one mount per selected report).
  useEffect(() => {
    rootRef.current?.focus({ preventScroll: true })
  }, [])

  // Dispatch / Resolve swap the buttons out from under the focus; keep focus in the drawer.
  const keepFocus = () => {
    window.requestAnimationFrame(() => {
      const root = rootRef.current
      if (root && (document.activeElement === document.body || !document.activeElement)) root.focus({ preventScroll: true })
    })
  }

  // Up/Down walk the queue without leaving the drawer, like they do in the queue itself.
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
    const target = event.target as HTMLElement
    if (target.closest('audio, input, textarea, select')) return
    event.preventDefault()
    onStep(event.key === 'ArrowDown' ? 1 : -1)
  }

  const run = async (kind: Exclude<Busy, null>, call: () => Promise<Report>) => {
    setBusy(kind)
    setError(null)
    try {
      onUpdated(await call())
    } catch (err) {
      setError(err instanceof Error ? `Could not update report: ${err.message}` : 'Could not update report.')
    } finally {
      setBusy(null)
      keepFocus()
    }
  }

  const setStatus = (status: ReportStatus) => run(status, () => api.setStatus(report.id, status))
  const retry = () => run('reprocess', () => api.reprocess(report.id))

  const pending = isPending(report)
  const review = !pending && needsReview(report)
  const lang = reportLanguage(report)
  const people = report.people_at_risk
  const peopleList = flagList(people, PEOPLE_LABELS)
  const peopleText =
    people.count != null && people.count > 0
      ? `${listOrNone(peopleList)} (${people.count} ${people.count === 1 ? 'person' : 'people'})`
      : listOrNone(peopleList)
  const chips = report.urgency_reasons.filter((r) => r !== 'needs review')

  return (
    <aside
      ref={rootRef}
      className={`fl-drawer ${levelClass(pending ? null : report.urgency_level)}`}
      aria-label={`Report ${report.id} details`}
      aria-keyshortcuts="ArrowUp ArrowDown Escape"
      tabIndex={-1}
      onKeyDown={onKeyDown}
    >
      <header className="fl-drawer-head">
        <div className="fl-drawer-id">
          {pending ? (
            <span className="fl-level is-pending">Processing</span>
          ) : (
            <span className={`fl-level ${levelClass(report.urgency_level)}`}>
              {report.urgency_level ?? 'Unrated'}
              {report.urgency_score != null && <span className="fl-level-score">{report.urgency_score}</span>}
            </span>
          )}
          <span className="fl-drawer-num">Report {report.id}</span>
          <span className={`fl-status is-${report.status}`}>{statusLabel(report.status)}</span>
        </div>
        <button type="button" className="fl-icon-btn" onClick={onClose} aria-label="Close details">
          <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
            <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      <div className="fl-drawer-scroll">
        <h2 className="fl-drawer-summary" dir="auto">
          {pending ? 'Understanding this report...' : report.ai_summary || 'No summary available'}
        </h2>
        <p className="fl-drawer-place">
          {placeLabel(report)}
          <span className="fl-drawer-when">
            {relativeTime(report.created_at, now)}, at {clockTime(report.created_at)}
          </span>
        </p>

        {review && (
          <div className="fl-review" role="note">
            <strong>Needs review.</strong>{' '}
            {report.ai_error
              ? `The AI could not read this report (${report.ai_error}).`
              : 'The AI could not fully read this report.'}{' '}
            Listen to the audio before ranking it lower.
          </div>
        )}

        <div className="fl-actions">
          {report.status === 'new' && (
            <button type="button" className="fl-btn is-primary" disabled={busy !== null} onClick={() => setStatus('dispatched')}>
              {busy === 'dispatched' ? 'Dispatching...' : 'Dispatch'}
            </button>
          )}
          {report.status !== 'resolved' && (
            <button type="button" className="fl-btn" disabled={busy !== null} onClick={() => setStatus('resolved')}>
              {busy === 'resolved' ? 'Resolving...' : 'Resolve'}
            </button>
          )}
          {report.status !== 'new' && (
            <button type="button" className="fl-btn" disabled={busy !== null} onClick={() => setStatus('new')}>
              {busy === 'new' ? 'Reopening...' : 'Reopen'}
            </button>
          )}
          {report.ai_status === 'failed' && (
            <button type="button" className="fl-btn is-ghost" disabled={busy !== null} onClick={retry}>
              {busy === 'reprocess' ? 'Retrying...' : 'Retry AI'}
            </button>
          )}
        </div>
        {error && (
          <p className="fl-error" role="alert">
            {error}
          </p>
        )}

        {report.audio_url && (
          <section className="fl-sec">
            <h3>Voice note</h3>
            <audio key={report.id} className="fl-audio" controls preload="none" src={report.audio_url} />
          </section>
        )}

        {report.photo_url && (
          <section className="fl-sec">
            <h3>Photo</h3>
            <a href={report.photo_url} target="_blank" rel="noreferrer">
              <img className="fl-photo" src={report.photo_url} alt={`Photo sent with report ${report.id}`} loading="lazy" />
            </a>
          </section>
        )}

        <section className="fl-sec">
          <h3>
            What they said {lang && <span className="fl-sec-note">{languageLabel(lang)}</span>}
          </h3>
          {report.transcript_original ? (
            <p className={`fl-transcript is-original${lang === 'ar' ? ' is-arabic' : ''}`} dir="auto" lang={lang ?? undefined}>
              {report.transcript_original}
            </p>
          ) : (
            <p className="fl-muted">{pending ? 'Transcribing...' : 'No transcript.'}</p>
          )}
        </section>

        {report.transcript_english && lang !== 'en' && (
          <section className="fl-sec">
            <h3>English translation</h3>
            <p className="fl-transcript" lang="en">
              {report.transcript_english}
            </p>
          </section>
        )}

        {chips.length > 0 && (
          <section className="fl-sec">
            <h3>Why it ranks here</h3>
            <div className="fl-chips">
              {chips.map((c) => (
                <span key={c} className="fl-chip">
                  {c}
                </span>
              ))}
            </div>
          </section>
        )}

        <section className="fl-sec">
          <h3>Situation</h3>
          <dl className="fl-fields">
            <div>
              <dt>Water depth</dt>
              <dd>{formatDepth(report.water_depth_cm)}</dd>
            </div>
            <div>
              <dt>Where</dt>
              <dd>{locationTypeLabel(report.location_type)}</dd>
            </div>
            <div>
              <dt>Water rising</dt>
              <dd className={report.water_rising ? 'is-alert' : undefined}>{yesNo(report.water_rising)}</dd>
            </div>
            <div>
              <dt>In living space</dt>
              <dd>{yesNo(report.water_in_living_space)}</dd>
            </div>
            <div className="is-wide">
              <dt>Hazards</dt>
              <dd>{listOrNone(flagList(report.hazards, HAZARD_LABELS))}</dd>
            </div>
            <div className="is-wide">
              <dt>People at risk</dt>
              <dd>{peopleText}</dd>
            </div>
            <div className="is-wide">
              <dt>Needs</dt>
              <dd>{listOrNone(flagList(report.needs, NEEDS_LABELS))}</dd>
            </div>
          </dl>
        </section>

        <section className="fl-sec">
          <h3>Source</h3>
          <dl className="fl-fields is-meta">
            <div>
              <dt>AI engine</dt>
              <dd>{engineLabel(report.ai_engine)}</dd>
            </div>
            <div>
              <dt>AI time</dt>
              <dd>{report.ai_latency_ms != null ? `${(report.ai_latency_ms / 1000).toFixed(1)} s` : 'Not measured'}</dd>
            </div>
            <div>
              <dt>Located by</dt>
              <dd>{locationSourceLabel(report.location_source)}</dd>
            </div>
            <div>
              <dt>Sent as</dt>
              <dd>
                {report.input_type === 'voice' ? 'Voice note' : 'Typed text'}
                {report.is_simulated ? ' (simulated)' : ''}
              </dd>
            </div>
            {report.location_hint && (
              <div className="is-wide">
                <dt>Place mentioned</dt>
                <dd dir="auto">{report.location_hint}</dd>
              </div>
            )}
            {hasCoords(report) && (
              <div className="is-wide">
                <dt>Coordinates</dt>
                <dd className="fl-num">
                  {report.lat.toFixed(5)}, {report.lng.toFixed(5)}
                  {report.accuracy_m != null ? ` (within ${Math.round(report.accuracy_m)} m)` : ''}
                </dd>
              </div>
            )}
          </dl>
        </section>
      </div>
    </aside>
  )
}
