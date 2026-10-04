// Live report state for the dashboard, fed by subscribeEvents (SSE, or polling as a fallback).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { subscribeEvents, type LiveStatus } from '../api'
import type { AppConfig, Report, ServerEvent, StormState } from '../types'
import { compareReports } from '../types'

export type ChangeKind = 'new' | 'changed'

/** A recent change to one report. `stamp` is unique per change so the flash animation can restart. */
export interface Flash {
  kind: ChangeKind
  stamp: number
}

export interface LiveReports {
  reports: Map<number, Report>
  sorted: Report[]
  status: LiveStatus
  storm: StormState
  config: AppConfig | null
  /** Bumps when the whole data set is replaced from scratch (first load, demo reset). */
  epoch: number
  flashes: Map<number, Flash>
  /** Apply a report the API just returned, without waiting for the SSE echo. */
  upsert: (report: Report) => void
  /** Treat the next snapshot as a fresh load (after a demo reset), so reseeding does not flash or toast. */
  expectReload: () => void
  /** Replace every report with a freshly fetched list (diffed like an SSE snapshot). */
  replaceAll: (reports: Report[]) => void
  setConfig: (config: AppConfig) => void
  setStorm: (storm: StormState) => void
}

interface Options {
  /** Called once when a report becomes CRITICAL after the initial load (new or upgraded). */
  onCritical?: (report: Report) => void
}

const FLASH_MS = 2400

// What the queue shows changing. updated_at is part of it, but a fresh insert carries
// updated_at == created_at (whole seconds), so the visible fields are compared too.
function signature(r: Report): string {
  return [r.updated_at, r.status, r.ai_status, r.urgency_level, r.urgency_score, r.address_text].join('|')
}

// Never let an older copy (an action's HTTP response or a poll that raced an SSE event) overwrite a
// newer one. The server gives every update its own millisecond updated_at, strictly increasing, so
// this orders even a "pending" write and the "AI failed" write that follows it a moment later.
function isStale(incoming: Report, current: Report | undefined): boolean {
  return !!current && Date.parse(incoming.updated_at) < Date.parse(current.updated_at)
}

export function useLiveReports({ onCritical }: Options = {}): LiveReports {
  const [reports, setReports] = useState<Map<number, Report>>(() => new Map())
  const [status, setStatus] = useState<LiveStatus>('connecting')
  const [storm, setStorm] = useState<StormState>({ running: false, injected: 0 })
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [epoch, setEpoch] = useState(0)
  const [flashes, setFlashes] = useState<Map<number, Flash>>(() => new Map())

  // The ref is the source of truth for diffing; state mirrors it for rendering. Doing the diff
  // outside setState keeps side effects (toasts) from running twice under StrictMode.
  const reportsRef = useRef<Map<number, Report>>(new Map())
  const loadedRef = useRef(false)
  const flashTimers = useRef<Map<number, number>>(new Map())
  const stampRef = useRef(0)
  const onCriticalRef = useRef(onCritical)
  useEffect(() => {
    onCriticalRef.current = onCritical
  }, [onCritical])

  const flash = useCallback((id: number, kind: ChangeKind) => {
    stampRef.current += 1
    const stamp = stampRef.current
    setFlashes((prev) => new Map(prev).set(id, { kind, stamp }))
    const timers = flashTimers.current
    window.clearTimeout(timers.get(id))
    timers.set(
      id,
      window.setTimeout(() => {
        timers.delete(id)
        setFlashes((prev) => {
          if (prev.get(id)?.stamp !== stamp) return prev
          const next = new Map(prev)
          next.delete(id)
          return next
        })
      }, FLASH_MS),
    )
  }, [])

  // Compare one incoming report against what we had, and fire flash/toast side effects.
  const noticeChange = useCallback(
    (prev: Report | undefined, next: Report) => {
      if (!loadedRef.current) return
      if (!prev) flash(next.id, 'new')
      else if (signature(prev) !== signature(next)) flash(next.id, 'changed')
      const becameCritical =
        next.urgency_level === 'CRITICAL' && next.status === 'new' && prev?.urgency_level !== 'CRITICAL'
      if (becameCritical) onCriticalRef.current?.(next)
    },
    [flash],
  )

  const commit = useCallback((next: Map<number, Report>) => {
    reportsRef.current = next
    setReports(next)
  }, [])

  const upsert = useCallback(
    (report: Report) => {
      const current = reportsRef.current
      const prev = current.get(report.id)
      if (isStale(report, prev)) return
      noticeChange(prev, report)
      commit(new Map(current).set(report.id, report))
    },
    [commit, noticeChange],
  )

  const applySnapshot = useCallback(
    (list: Report[]) => {
      const current = reportsRef.current
      const next = new Map<number, Report>()
      for (const report of list) {
        const prev = current.get(report.id)
        if (isStale(report, prev)) {
          next.set(report.id, prev!)
          continue
        }
        noticeChange(prev, report)
        next.set(report.id, report)
      }
      commit(next)
      if (!loadedRef.current) {
        loadedRef.current = true
        setEpoch((e) => e + 1)
      }
    },
    [commit, noticeChange],
  )

  const expectReload = useCallback(() => {
    loadedRef.current = false
    commit(new Map())
    setFlashes(new Map())
  }, [commit])

  useEffect(() => {
    const handle = (event: ServerEvent) => {
      switch (event.type) {
        case 'snapshot':
          applySnapshot(event.reports)
          break
        case 'report.created':
        case 'report.updated':
          upsert(event.report)
          break
        case 'storm.state':
          setStorm({ running: event.running, injected: event.injected })
          break
        case 'config.updated':
          setConfig(event.config)
          break
        case 'reset':
          // api.ts fetches a fresh snapshot next; treat it like a first load (no flashes, no toasts).
          expectReload()
          break
        case 'hello':
          break
      }
    }
    const unsubscribe = subscribeEvents(handle, setStatus)
    const timers = flashTimers.current
    return () => {
      unsubscribe()
      for (const t of timers.values()) window.clearTimeout(t)
      timers.clear()
    }
  }, [applySnapshot, expectReload, upsert])

  const sorted = useMemo(() => Array.from(reports.values()).sort(compareReports), [reports])

  return {
    reports,
    sorted,
    status,
    storm,
    config,
    epoch,
    flashes,
    upsert,
    expectReload,
    replaceAll: applySnapshot,
    setConfig,
    setStorm,
  }
}

/** A clock that ticks every `intervalMs`, for relative times ("4 min ago"). */
export function useNow(intervalMs = 30000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(id)
  }, [intervalMs])
  return now
}
