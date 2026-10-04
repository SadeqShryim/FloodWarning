// /dashboard: the responder command center. Map on the left (the hero), ranked queue on the right,
// details in a drawer over the queue. All data arrives live through useLiveReports.
import L from 'leaflet'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import type { Briefing, Hotspot, Report } from '../types'
import Drawer from './Drawer'
import Header, { type Counts } from './Header'
import MapView from './MapView'
import QrPanel from './QrPanel'
import Queue, { type QueueFilter } from './Queue'
import SitrepPanel from './SitrepPanel'
import Toasts, { type Toast } from './Toasts'
import { DEFAULT_CENTER, DEFAULT_ZOOM, hasCoords, placeLabel } from './format'
import { useLiveReports, useNow } from './useLiveReports'
import './dashboard.css'

const FONT_HREF = 'https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,300..900&display=swap'
const MAX_TOASTS = 4

// Per-viewer conveniences only; the page works the same when storage is blocked.
function readPref(key: string, fallback: string): string {
  try {
    return window.localStorage.getItem(key) ?? fallback
  } catch {
    return fallback
  }
}

function writePref(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // private mode or blocked storage: nothing to remember
  }
}

function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
}

/** Archivo (with its width axis) for the console; system fonts take over if it cannot load. */
function useDashboardFont() {
  useEffect(() => {
    if (document.getElementById('fl-dash-font')) return
    const link = document.createElement('link')
    link.id = 'fl-dash-font'
    link.rel = 'stylesheet'
    link.href = FONT_HREF
    document.head.appendChild(link)
  }, [])
}

/**
 * Padding for fitting a hotspot into the part of the map the floating panels leave clear: right of
 * the sitrep (bottom left) and below the QR code (top right). Falls back to plain padding when the
 * panels leave too little room (small screens).
 */
function clearAreaPadding(map: L.Map): Pick<L.FitBoundsOptions, 'paddingTopLeft' | 'paddingBottomRight'> {
  const box = map.getContainer().getBoundingClientRect()
  const gap = 24
  let left = 64
  let top = 64
  const sitrep = document.querySelector('.fl-sitrep')?.getBoundingClientRect()
  if (sitrep) left = Math.max(left, sitrep.right - box.left + gap)
  const qr = document.querySelector('.fl-qr')?.getBoundingClientRect()
  if (qr) top = Math.max(top, qr.bottom - box.top + gap)
  if (box.width - left - 64 < 200 || box.height - top - 64 < 160) {
    return { paddingTopLeft: [64, 64], paddingBottomRight: [64, 64] }
  }
  return { paddingTopLeft: [left, top], paddingBottomRight: [64, 64] }
}

function errorText(err: unknown, action: string): string {
  return err instanceof Error ? `${action}: ${err.message}` : `${action}.`
}

export default function DashboardPage() {
  useDashboardFont()
  const now = useNow(30000)

  const [toasts, setToasts] = useState<Toast[]>([])
  const toastSeq = useRef(0)
  const onCritical = useCallback((report: Report) => {
    toastSeq.current += 1
    const toast: Toast = {
      key: toastSeq.current,
      reportId: report.id,
      summary: report.ai_summary || 'Critical report',
      place: placeLabel(report),
    }
    setToasts((prev) => [toast, ...prev.filter((t) => t.reportId !== report.id)].slice(0, MAX_TOASTS))
  }, [])

  const live = useLiveReports({ onCritical })
  const { reports, sorted, status, storm, config, epoch, flashes, upsert, expectReload, replaceAll, setConfig, setStorm } =
    live

  const [map, setMap] = useState<L.Map | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [filter, setFilter] = useState<QueueFilter>(() => (readPref('fl.queueFilter', 'all') === 'open' ? 'open' : 'all'))
  const [qrOpen, setQrOpen] = useState(() => readPref('fl.qrOpen', '1') === '1')
  const [briefing, setBriefing] = useState<Briefing | null>(null)
  const [sitrepOpen, setSitrepOpen] = useState(false)
  const [sitrepBusy, setSitrepBusy] = useState(false)
  const [sitrepError, setSitrepError] = useState<string | null>(null)
  const [stormBusy, setStormBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  // A fresh data set (first load, demo reset): the old selection, sitrep and toasts describe
  // reports that no longer exist. Reset them during render (React's pattern for state that follows
  // a changed value), so the stale drawer is never painted.
  const [shownEpoch, setShownEpoch] = useState(epoch)
  if (shownEpoch !== epoch) {
    setShownEpoch(epoch)
    setSelectedId(null)
    setBriefing(null)
    setSitrepOpen(false)
    setToasts([])
  }

  // Map callbacks need the latest reports without re-subscribing on every update.
  const reportsRef = useRef(reports)
  useEffect(() => {
    reportsRef.current = reports
  }, [reports])

  const selected = selectedId != null ? reports.get(selectedId) : undefined

  const counts: Counts = useMemo(() => {
    let criticalOpen = 0
    let dispatched = 0
    let processing = 0
    for (const r of sorted) {
      if (r.urgency_level === 'CRITICAL' && r.status === 'new') criticalOpen += 1
      if (r.status === 'dispatched') dispatched += 1
      if (r.ai_status === 'pending') processing += 1
    }
    return { total: sorted.length, criticalOpen, dispatched, processing }
  }, [sorted])
  const openCount = useMemo(() => sorted.filter((r) => r.status === 'new').length, [sorted])

  useEffect(() => {
    document.title = counts.criticalOpen > 0 ? `(${counts.criticalOpen}) FloodLine dashboard` : 'FloodLine dashboard'
  }, [counts.criticalOpen])

  useEffect(() => writePref('fl.queueFilter', filter), [filter])
  useEffect(() => writePref('fl.qrOpen', qrOpen ? '1' : '0'), [qrOpen])

  // Errors from header actions show briefly over the map instead of blocking anything.
  useEffect(() => {
    if (!notice) return
    const t = window.setTimeout(() => setNotice(null), 6000)
    return () => window.clearTimeout(t)
  }, [notice])

  // A fresh data set (first load or demo reset): frame every located report once.
  useEffect(() => {
    if (!map || epoch === 0) return
    const points = Array.from(reportsRef.current.values())
      .filter(hasCoords)
      .map((r) => L.latLng(r.lat, r.lng))
    if (points.length === 0) return
    map.fitBounds(L.latLngBounds(points), { padding: [48, 48], maxZoom: 15, animate: false })
  }, [map, epoch])

  // Before the first snapshot, center on whatever the server says the city is.
  const centeredFromConfig = useRef(false)
  useEffect(() => {
    if (!map || !config || centeredFromConfig.current || epoch > 0) return
    centeredFromConfig.current = true
    map.setView(config.map_center, config.map_zoom, { animate: false })
  }, [map, config, epoch])

  const flyTo = useCallback(
    (id: number) => {
      const report = reportsRef.current.get(id)
      if (!map || !report || !hasCoords(report)) return
      const zoom = Math.max(map.getZoom(), 15)
      if (prefersReducedMotion()) map.setView([report.lat, report.lng], zoom)
      else map.flyTo([report.lat, report.lng], zoom, { duration: 0.7 })
    },
    [map],
  )

  // From the queue or a toast: select and bring the pin into view.
  const selectAndFly = useCallback(
    (id: number) => {
      setSelectedId(id)
      flyTo(id)
    },
    [flyTo],
  )

  // From the map: the pin is already in view, so just select it.
  const selectFromMap = useCallback((id: number) => setSelectedId(id), [])

  const closeDrawer = useCallback(() => setSelectedId(null), [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelectedId(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const focusHotspot = useCallback(
    (hotspot: Hotspot) => {
      if (!map) return
      const points = hotspot.report_ids
        .map((id) => reportsRef.current.get(id))
        .filter((r): r is Report & { lat: number; lng: number } => !!r && hasCoords(r))
        .map((r) => L.latLng(r.lat, r.lng))
      const bounds =
        points.length >= 2
          ? L.latLngBounds(points)
          : L.latLng(hotspot.lat, hotspot.lng).toBounds(Math.max(hotspot.radius_m, 250) * 2)
      map.flyToBounds(bounds, { ...clearAreaPadding(map), maxZoom: 16, duration: prefersReducedMotion() ? 0 : 0.7 })
    },
    [map],
  )

  const dismissToast = useCallback((key: number) => setToasts((prev) => prev.filter((t) => t.key !== key)), [])

  const toggleStorm = async () => {
    setStormBusy(true)
    try {
      setStorm(storm.running ? await api.stopStorm() : await api.startStorm())
    } catch (err) {
      setNotice(errorText(err, storm.running ? 'Could not stop the storm' : 'Could not start the storm'))
    } finally {
      setStormBusy(false)
    }
  }

  const loadBriefing = async () => {
    setSitrepBusy(true)
    setSitrepError(null)
    try {
      setBriefing(await api.briefing())
    } catch (err) {
      setSitrepError(errorText(err, 'Could not write the sitrep'))
    } finally {
      setSitrepBusy(false)
    }
  }

  const toggleSitrep = () => {
    if (sitrepOpen) {
      setSitrepOpen(false)
      return
    }
    setSitrepOpen(true)
    void loadBriefing()
  }

  const resetDemo = async () => {
    // Forget the current set first, so the reseeded reports load quietly instead of as 25 "new"
    // ones (and four "new CRITICAL" toasts). Then load the fresh set right away instead of
    // waiting for the SSE refresh or the next poll; whichever lands last wins, both are correct.
    expectReload()
    try {
      await api.resetDemo()
    } catch (err) {
      replaceAll(await api.listReports().catch(() => []))
      throw err
    }
    expectReload()
    replaceAll(await api.listReports())
  }

  const center = config?.map_center ?? DEFAULT_CENTER
  const zoom = config?.map_zoom ?? DEFAULT_ZOOM
  const hotspots = sitrepOpen && briefing ? briefing.hotspots : []

  return (
    <div className="fl-dash">
      <Header
        counts={counts}
        status={status}
        storm={storm}
        stormBusy={stormBusy}
        onStorm={toggleStorm}
        sitrepBusy={sitrepBusy}
        sitrepOpen={sitrepOpen}
        onSitrep={toggleSitrep}
        qrOpen={qrOpen}
        onQr={() => setQrOpen((v) => !v)}
        onReset={resetDemo}
      />
      <main className="fl-main">
        <div className="fl-stage">
          <MapView
            reports={sorted}
            selectedId={selectedId}
            hotspots={hotspots}
            onSelect={selectFromMap}
            onHotspot={focusHotspot}
            onMap={setMap}
            center={center}
            zoom={zoom}
          />
          <div className="fl-overlay">
            <Toasts toasts={toasts} onOpen={selectAndFly} onDismiss={dismissToast} />
            {notice && (
              <p className="fl-notice" role="alert">
                {notice}
              </p>
            )}
            {sitrepOpen && (
              <SitrepPanel
                briefing={briefing}
                busy={sitrepBusy}
                error={sitrepError}
                onRefresh={() => void loadBriefing()}
                onClose={() => setSitrepOpen(false)}
                onHotspot={focusHotspot}
              />
            )}
            {qrOpen && <QrPanel config={config} onConfig={setConfig} onClose={() => setQrOpen(false)} />}
          </div>
          <Legend />
        </div>
        <div className="fl-side">
          <Queue
            reports={sorted}
            selectedId={selectedId}
            flashes={flashes}
            now={now}
            filter={filter}
            onFilter={setFilter}
            openCount={openCount}
            onSelect={selectAndFly}
          />
          {/* Keyed by report, so a newly selected report starts with fresh busy/error state. */}
          {selected && <Drawer key={selected.id} report={selected} now={now} onClose={closeDrawer} onUpdated={upsert} />}
        </div>
      </main>
    </div>
  )
}

/** Pin key, so a projector audience can read the map without the queue. */
function Legend() {
  return (
    <ul className="fl-legend" aria-label="Map key">
      <li className="is-critical">Critical</li>
      <li className="is-high">High</li>
      <li className="is-medium">Medium</li>
      <li className="is-low">Low</li>
      <li className="is-pending">Processing</li>
      <li className="is-ring">Dispatched</li>
    </ul>
  )
}
