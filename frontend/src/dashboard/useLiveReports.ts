// Live report state for the dashboard, fed by subscribeEvents (SSE, or polling as a fallback).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { subscribeEvents, type LiveStatus } from '../api'
import type { AppConfig, Report, ServerEvent, StormState } from '../types'
import { compareReports } from '../types'

export type ChangeKind = 'new' | 'changed'

/** What the header shows. 'offline' = polling, but no poll has reached the server for a while. */
export type ConnectionStatus = LiveStatus | 'offline'

/** A recent change to one report. `stamp` is unique per change so the flash animation can restart. */
export interface Flash {
  kind: ChangeKind
  stamp: number
}

export interface LiveReports {
  reports: Map<number, Report>
  sorted: Report[]
  status: ConnectionStatus
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
// A stream stuck in "connecting" this long is re-opened from scratch. The browser's own EventSource
// retry can hang on a proxy or tunnel that holds the request open without answering, and api.ts
// only arms its "no hello, fall back to polling" timer on a fresh connect.
const STUCK_CONNECTING_MS = 10000
// Polling succeeds every 3 s; this long without a fresh list means the server is unreachable.
const OFFLINE_AFTER_MS = 8000

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
  const [subscription, setSubscription] = useState(0) // bump to re-open the event stream
  const [offline, setOffline] = useState(false)
  const lastSnapshotAt = useRef(0)

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
      // The same id with a different creation time means the server was reset behind our back (for
      // example from another dashboard while this one was polling and missed the 'reset' event):
      // treat it as a fresh load, so reseeded reports do not flash, toast or keep a stale selection.
      if (loadedRef.current) {
        const replaced = list.some((r) => {
          const prev = reportsRef.current.get(r.id)
          return !!prev && prev.created_at !== r.created_at
        })
        if (replaced) {
          loadedRef.current = false
          setFlashes(new Map())
        }
      }
      const current = loadedRef.current ? reportsRef.current : new Map<number, Report>()
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
      lastSnapshotAt.current = Date.now()
      setOffline(false)
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
    return unsubscribe
  }, [applySnapshot, expectReload, upsert, subscription])

  useEffect(() => {
    const timers = flashTimers.current
    return () => {
      for (const t of timers.values()) window.clearTimeout(t)
      timers.clear()
    }
  }, [])

  // Watchdog for a reconnect that never resolves (see STUCK_CONNECTING_MS).
  useEffect(() => {
    if (status !== 'connecting') return
    const t = window.setTimeout(() => setSubscription((n) => n + 1), STUCK_CONNECTING_MS)
    return () => window.clearTimeout(t)
  }, [status, subscription])

  // While polling, notice when the polls stop reaching the server.
  useEffect(() => {
    if (status !== 'polling') return
    // Grace period: the first poll fires right away, so give it a few seconds before judging.
    lastSnapshotAt.current = Math.max(lastSnapshotAt.current, Date.now() - 3000)
    const id = window.setInterval(() => {
      setOffline(Date.now() - lastSnapshotAt.current > OFFLINE_AFTER_MS)
    }, 1000)
    return () => {
      window.clearInterval(id)
      setOffline(false)
    }
  }, [status])

  const sorted = useMemo(() => Array.from(reports.values()).sort(compareReports), [reports])

  return {
    reports,
    sorted,
    status: status === 'polling' && offline ? 'offline' : status,
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
