// The ranked queue: one card per report, most urgent first, reordering smoothly as reports change.
import { memo, useLayoutEffect, useRef, type KeyboardEvent } from 'react'
import type { Report } from '../types'
import type { Flash } from './useLiveReports'
import {
  formatDepth,
  isPending,
  languageLabel,
  levelClass,
  needsReview,
  placeLabel,
  relativeTime,
  reportLanguage,
  statusLabel,
} from './format'

export type QueueFilter = 'open' | 'all'

// The gauge on each card's edge fills like a staff gauge: full at 1.5 m of water.
const GAUGE_FULL_CM = 150

function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
}

interface CardProps {
  report: Report
  selected: boolean
  flash: Flash | undefined
  now: number
  onSelect: (id: number) => void
}

const Card = memo(function Card({ report, selected, flash, now, onSelect }: CardProps) {
  const pending = isPending(report)
  const review = !pending && needsReview(report)
  const lang = reportLanguage(report)
  const depth = report.water_depth_cm
  const gauge = depth != null ? Math.min(1, depth / GAUGE_FULL_CM) : 0
  const chips = report.urgency_reasons.filter((r) => r !== 'needs review')

  return (
    <li
      data-card-id={report.id}
      className={`fl-card ${levelClass(pending ? null : report.urgency_level)} is-${report.status}${selected ? ' is-selected' : ''}`}
    >
      {flash && <span key={flash.stamp} className={`fl-card-flash is-${flash.kind}`} aria-hidden="true" />}
      <button
        type="button"
        className="fl-card-button"
        aria-pressed={selected}
        data-report-id={report.id}
        onClick={() => onSelect(report.id)}
      >
        <span className="fl-gauge" aria-hidden="true">
          <span className="fl-gauge-fill" style={{ height: `${Math.round(gauge * 100)}%` }} />
        </span>
        <span className="fl-card-body">
          <span className="fl-card-top">
            {pending ? (
              <span className="fl-level is-pending">Processing</span>
            ) : (
              <span className={`fl-level ${levelClass(report.urgency_level)}`}>
                {report.urgency_level ?? 'Unrated'}
                {report.urgency_score != null && <span className="fl-level-score">{report.urgency_score}</span>}
              </span>
            )}
            <span className="fl-lang" title={`Reported in ${languageLabel(lang)}`}>
              {languageLabel(lang)}
              {lang !== 'en' && <span className="fl-lang-tr">translated</span>}
            </span>
            <span className="fl-card-time">{relativeTime(report.created_at, now)}</span>
          </span>

          {pending ? (
            <span className="fl-card-summary is-skeleton">
              <span className="fl-skel-text">Processing...</span>
              <span className="fl-skel-bar" />
              <span className="fl-skel-bar is-short" />
            </span>
          ) : (
            <span className="fl-card-summary" dir="auto">
              {report.ai_summary || report.transcript_english || report.transcript_original || 'No summary yet'}
            </span>
          )}

          <span className="fl-card-place">
            <span className="fl-card-place-text">{placeLabel(report)}</span>
            {depth != null && <span className="fl-card-depth">{formatDepth(depth)}</span>}
          </span>

          {(chips.length > 0 || review || report.status !== 'new') && (
            <span className="fl-card-foot">
              {review && <span className="fl-chip is-review">Needs review</span>}
              {chips.slice(0, 4).map((c) => (
                <span key={c} className="fl-chip">
                  {c}
                </span>
              ))}
              {report.status !== 'new' && (
                <span className={`fl-status is-${report.status}`}>{statusLabel(report.status)}</span>
              )}
            </span>
          )}
        </span>
      </button>
    </li>
  )
})

interface QueueProps {
  reports: Report[]
  selectedId: number | null
  flashes: Map<number, Flash>
  now: number
  filter: QueueFilter
  onFilter: (filter: QueueFilter) => void
  openCount: number
  onSelect: (id: number) => void
}

export default function Queue({ reports, selectedId, flashes, now, filter, onFilter, openCount, onSelect }: QueueProps) {
  const listRef = useRef<HTMLOListElement>(null)
  const lastTops = useRef<Map<number, number>>(new Map())

  const visible = filter === 'open' ? reports.filter((r) => r.status === 'new') : reports
  const order = visible.map((r) => r.id).join(',')

  // FLIP: after the order changes, start each card where it used to be and let it glide home.
  // offsetTop is relative to the list, so scrolling does not distort the deltas.
  useLayoutEffect(() => {
    const reduce = prefersReducedMotion()
    const moved: HTMLElement[] = []
    const tops = new Map<number, number>()
    const cards = listRef.current ? Array.from(listRef.current.children) : []
    for (const card of cards) {
      const el = card as HTMLElement
      const id = Number(el.dataset.cardId)
      if (!Number.isFinite(id)) continue
      const top = el.offsetTop
      tops.set(id, top)
      const before = lastTops.current.get(id)
      if (reduce || before === undefined || before === top) continue
      el.style.transition = 'none'
      el.style.transform = `translateY(${before - top}px)`
      moved.push(el)
    }
    lastTops.current = tops
    if (moved.length === 0) return
    // Force a layout so the inverted position sticks before transitioning back to zero.
    void listRef.current?.offsetHeight
    const frame = requestAnimationFrame(() => {
      for (const el of moved) {
        el.style.transition = 'transform 420ms cubic-bezier(0.2, 0.7, 0.2, 1)'
        el.style.transform = ''
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [order])

  // Up/Down arrows walk the queue like a list box; the focused card is selected as you go.
  const onKeyDown = (event: KeyboardEvent<HTMLOListElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    const buttons = Array.from(listRef.current?.querySelectorAll<HTMLButtonElement>('.fl-card-button') ?? [])
    if (buttons.length === 0) return
    event.preventDefault()
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
    const nextIndex =
      index < 0 ? 0 : Math.min(buttons.length - 1, Math.max(0, index + (event.key === 'ArrowDown' ? 1 : -1)))
    const target = buttons[nextIndex]
    target.focus()
    const id = Number(target.dataset.reportId)
    if (Number.isFinite(id)) onSelect(id)
  }

  return (
    <section className="fl-queue" aria-label="Ranked report queue">
      <div className="fl-queue-head">
        <h2 className="fl-queue-title">Queue</h2>
        <div className="fl-seg" role="group" aria-label="Which reports to show">
          <button type="button" aria-pressed={filter === 'all'} onClick={() => onFilter('all')}>
            All <span className="fl-seg-n">{reports.length}</span>
          </button>
          <button type="button" aria-pressed={filter === 'open'} onClick={() => onFilter('open')}>
            Open <span className="fl-seg-n">{openCount}</span>
          </button>
        </div>
      </div>
      {visible.length === 0 ? (
        <p className="fl-queue-empty">
          {reports.length === 0
            ? 'Waiting for reports. Scan the QR code with a phone to send the first one.'
            : 'No open reports. Switch to All to see dispatched and resolved ones.'}
        </p>
      ) : (
        <ol className="fl-queue-list" ref={listRef} onKeyDown={onKeyDown}>
          {visible.map((r) => (
            <Card
              key={r.id}
              report={r}
              selected={r.id === selectedId}
              flash={flashes.get(r.id)}
              now={now}
              onSelect={onSelect}
            />
          ))}
        </ol>
      )}
    </section>
  )
}
