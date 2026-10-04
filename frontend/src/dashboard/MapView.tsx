// The hero: a dark OpenStreetMap with one pin per located report and the latest sitrep hotspots.
import 'leaflet/dist/leaflet.css'
import L from 'leaflet'
import { memo, useEffect, useMemo, useRef } from 'react'
import { Circle, MapContainer, Marker, TileLayer, Tooltip } from 'react-leaflet'
import type { Hotspot, Report } from '../types'
import { LEVEL_COLOR, LEVEL_RANK } from '../types'
import { hasCoords, isPending, needsReview, placeLabel, reportColor } from './format'

const PIN_SIZE = { CRITICAL: 26, HIGH: 21, MEDIUM: 17, LOW: 15 } as const
const PENDING_SIZE = 19
const METERS_PER_DEGREE_LAT = 111_320

interface PinLook {
  color: string
  size: number
  pulse: boolean
  pending: boolean
  dispatched: boolean
  resolved: boolean
  selected: boolean
  review: boolean
}

// Icons are cached by their look so React re-renders do not rebuild DOM for unchanged pins.
const iconCache = new Map<string, L.DivIcon>()

function pinIcon(look: PinLook): L.DivIcon {
  const key = JSON.stringify(look)
  const cached = iconCache.get(key)
  if (cached) return cached
  const classes = ['fl-pin']
  if (look.pulse) classes.push('is-pulse')
  if (look.pending) classes.push('is-pending')
  if (look.dispatched) classes.push('is-dispatched')
  if (look.resolved) classes.push('is-resolved')
  if (look.selected) classes.push('is-selected')
  if (look.review) classes.push('is-review')
  // The box is larger than the dot so the dispatched ring and pulse have room.
  const box = look.size + 22
  const icon = L.divIcon({
    className: 'fl-pin-icon',
    html: `<span class="${classes.join(' ')}" style="--pin:${look.color};--size:${look.size}px"><span class="fl-pin-dot"></span></span>`,
    iconSize: [box, box],
    iconAnchor: [box / 2, box / 2],
  })
  iconCache.set(key, icon)
  return icon
}

function lookFor(report: Report, selected: boolean): PinLook {
  const pending = isPending(report)
  const level = report.urgency_level
  return {
    color: reportColor(report),
    size: pending || !level ? PENDING_SIZE : PIN_SIZE[level],
    pulse: report.status === 'new' && (pending || level === 'CRITICAL'),
    pending,
    dispatched: report.status === 'dispatched',
    resolved: report.status === 'resolved',
    selected,
    review: !pending && needsReview(report),
  }
}

// Higher urgency draws on top so a CRITICAL pin is never hidden under a LOW one.
function zIndexFor(report: Report, selected: boolean): number {
  if (selected) return 2000
  if (isPending(report)) return 900
  const rank = report.urgency_level ? LEVEL_RANK[report.urgency_level] : 0
  const statusPenalty = report.status === 'new' ? 0 : -600
  return rank * 150 + statusPenalty
}

interface PinProps {
  report: Report & { lat: number; lng: number }
  selected: boolean
  onSelect: (id: number) => void
}

const Pin = memo(function Pin({ report, selected, onSelect }: PinProps) {
  const icon = pinIcon(lookFor(report, selected))
  const handlers = useMemo(() => ({ click: () => onSelect(report.id) }), [onSelect, report.id])
  const title = isPending(report)
    ? `#${report.id} processing`
    : `#${report.id} ${report.urgency_level ?? ''}: ${report.ai_summary ?? placeLabel(report)}`

  // Leaflet applies `title` only when it first builds the marker element, and setIcon reuses a
  // divIcon's element, so a pin created while "processing" would keep that hover text forever.
  const markerRef = useRef<L.Marker | null>(null)
  useEffect(() => {
    const marker = markerRef.current
    if (!marker) return
    marker.options.title = title
    const el = marker.getElement()
    if (el) el.title = title
  }, [title])

  return (
    <Marker
      ref={markerRef}
      position={[report.lat, report.lng]}
      icon={icon}
      zIndexOffset={zIndexFor(report, selected)}
      eventHandlers={handlers}
      title={title}
      alt={title}
      riseOnHover
    />
  )
})

interface HotspotProps {
  hotspot: Hotspot
  onFocus: (hotspot: Hotspot) => void
}

function HotspotCircle({ hotspot, onFocus }: HotspotProps) {
  const color = LEVEL_COLOR[hotspot.level]
  const handlers = useMemo(() => ({ click: () => onFocus(hotspot) }), [hotspot, onFocus])
  const labelAt = useMemo<[number, number]>(
    () => [hotspot.lat + hotspot.radius_m / METERS_PER_DEGREE_LAT, hotspot.lng],
    [hotspot.lat, hotspot.lng, hotspot.radius_m],
  )
  return (
    <Circle
      center={[hotspot.lat, hotspot.lng]}
      radius={hotspot.radius_m}
      pathOptions={{ color, weight: 1.5, opacity: 0.9, dashArray: '6 6', fillColor: color, fillOpacity: 0.1 }}
      eventHandlers={handlers}
    >
      {/* The label is clickable too: it is the most visible part of a hotspot. It sits on the
          circle's top edge rather than its center, so it never covers the urgent pins inside. */}
      <Tooltip
        permanent
        position={labelAt}
        direction="top"
        offset={[0, -2]}
        className="fl-hotspot-label"
        interactive
        eventHandlers={handlers}
      >
        <span className="fl-hotspot-name">{hotspot.label}</span>
        <span className="fl-hotspot-count">
          {hotspot.report_ids.length} {hotspot.report_ids.length === 1 ? 'report' : 'reports'}
        </span>
      </Tooltip>
    </Circle>
  )
}

interface MapViewProps {
  reports: Report[]
  selectedId: number | null
  hotspots: Hotspot[]
  onSelect: (id: number) => void
  onHotspot: (hotspot: Hotspot) => void
  onMap: (map: L.Map | null) => void
  center: [number, number]
  zoom: number
}

export default function MapView({ reports, selectedId, hotspots, onSelect, onHotspot, onMap, center, zoom }: MapViewProps) {
  // Draw resolved first, then dispatched, then open, so the DOM order matches visual priority.
  const located = useMemo(() => reports.filter(hasCoords).slice().reverse(), [reports])

  return (
    <MapContainer
      ref={onMap}
      className="fl-map"
      center={center}
      zoom={zoom}
      zoomControl
      // Quarter-step zoom when framing all reports, so keeping pins clear of the QR panel costs a
      // little zoom instead of a whole level (the +/- buttons still move one level).
      zoomSnap={0.25}
      preferCanvas={false}
      worldCopyJump={false}
      attributionControl
    >
      <TileLayer
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        maxZoom={19}
      />
      {hotspots.map((h) => (
        <HotspotCircle key={`${h.label}-${h.lat}-${h.lng}`} hotspot={h} onFocus={onHotspot} />
      ))}
      {located.map((r) => (
        <Pin key={r.id} report={r} selected={r.id === selectedId} onSelect={onSelect} />
      ))}
    </MapContainer>
  )
}
