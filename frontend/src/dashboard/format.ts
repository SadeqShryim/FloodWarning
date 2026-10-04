// Small display helpers for the responder dashboard. Pure functions, no React.
import type { LocationSource, LocationType, Report, UrgencyLevel } from '../types'
import { LEVEL_COLOR, PENDING_COLOR } from '../types'

// Map view used until /api/config answers (same values as backend config.MAP_CENTER / MAP_ZOOM).
// Kept here rather than in MapView.tsx so that file exports only its component (fast refresh).
export const DEFAULT_CENTER: [number, number] = [42.315, -83.199]
export const DEFAULT_ZOOM = 13

/** "45 cm" under a meter, "1.2 m" from 100 cm (same wording as the urgency reason chips). */
export function formatDepth(cm: number | null | undefined): string {
  if (cm == null) return 'Unknown'
  if (cm < 100) return `${Math.round(cm)} cm`
  return `${(cm / 100).toFixed(1)} m`
}

const LANGUAGE_NAMES: Record<string, string> = { ar: 'Arabic', es: 'Spanish', en: 'English' }

/** Full name for the three demo languages, the uppercased ISO code for anything else. */
export function languageLabel(code: string | null | undefined): string {
  if (!code) return 'Unknown'
  const key = code.toLowerCase()
  return LANGUAGE_NAMES[key] ?? key.toUpperCase()
}

/** What the reporter spoke. While the AI is still working, the phone's UI language is the best guess. */
export function reportLanguage(report: Report): string {
  return (report.language || report.ui_language || 'en').toLowerCase()
}

/** "just now", "4 min ago", "2 h ago", or a date for anything older than a day. */
export function relativeTime(iso: string, now: number): string {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return ''
  const seconds = Math.max(0, Math.round((now - then) / 1000))
  if (seconds < 45) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  return new Date(then).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function clockTime(iso: string): string {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return ''
  return new Date(then).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
}

export function placeLabel(report: Report): string {
  return report.address_text?.trim() || report.location_hint?.trim() || 'Location unknown'
}

export function hasCoords(report: Report): report is Report & { lat: number; lng: number } {
  return typeof report.lat === 'number' && typeof report.lng === 'number'
}

export function isPending(report: Report): boolean {
  return report.ai_status === 'pending'
}

export function needsReview(report: Report): boolean {
  return report.ai_status === 'failed' || report.urgency_reasons.includes('needs review')
}

/** Pin and badge color: the level color, or neutral gray while the AI is still working. */
export function reportColor(report: Report): string {
  if (isPending(report) || !report.urgency_level) return PENDING_COLOR
  return LEVEL_COLOR[report.urgency_level]
}

export function levelClass(level: UrgencyLevel | null): string {
  return level ? `is-${level.toLowerCase()}` : 'is-pending'
}

const LOCATION_TYPE_LABEL: Record<LocationType, string> = {
  basement: 'Basement',
  home: 'Home',
  street: 'Street',
  car: 'In a car',
  other: 'Other',
}

export function locationTypeLabel(type: LocationType | null): string {
  return type ? LOCATION_TYPE_LABEL[type] : 'Unknown'
}

const LOCATION_SOURCE_LABEL: Record<LocationSource, string> = {
  gps: 'Phone GPS',
  typed: 'Typed address',
  spoken: 'Place named in the voice note',
  seed: 'Demo data',
  storm: 'Storm simulation',
}

export function locationSourceLabel(source: LocationSource | null): string {
  return source ? LOCATION_SOURCE_LABEL[source] : 'Not located'
}

const STATUS_LABEL = { new: 'Open', dispatched: 'Dispatched', resolved: 'Resolved' } as const

export function statusLabel(status: Report['status']): string {
  return STATUS_LABEL[status]
}

/** Truthy keys of a flags object, as readable words. */
export function flagList(flags: object, labels: Record<string, string>): string[] {
  const out: string[] = []
  for (const [key, value] of Object.entries(flags)) {
    if (value === true) out.push(labels[key] ?? key)
  }
  return out
}

/** "ai engine" values: model ids read better without the vendor prefix noise. */
export function engineLabel(engine: string | null): string {
  if (!engine) return 'None yet'
  if (engine === 'rules') return 'Keyword rules (AI unavailable)'
  if (engine === 'seed') return 'Demo data'
  if (engine === 'storm-sim') return 'Storm simulation'
  return engine
}
