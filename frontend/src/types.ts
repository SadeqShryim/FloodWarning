// Shapes shared with the FloodLine API. Mirrors backend/app/models.py: change both together.

export type UrgencyLevel = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'
export type LocationType = 'basement' | 'home' | 'street' | 'car' | 'other'
export type ReportStatus = 'new' | 'dispatched' | 'resolved'
export type AIStatus = 'pending' | 'done' | 'failed'
export type LocationSource = 'gps' | 'typed' | 'spoken' | 'seed' | 'storm'
export type InputType = 'voice' | 'text'
export type UiLanguage = 'en' | 'ar' | 'es'

export interface Hazards {
  electrical: boolean
  sewage: boolean
  gas: boolean
  structural: boolean
}

export interface PeopleAtRisk {
  elderly: boolean
  children: boolean
  disabled: boolean
  medical: boolean
  trapped: boolean
  count: number | null
}

export interface Needs {
  evacuation: boolean
  pumping: boolean
  medical: boolean
  supplies: boolean
}

export interface Report {
  id: number
  created_at: string // ISO 8601, UTC
  updated_at: string // ISO 8601, UTC; milliseconds after an update, strictly increasing (orders versions)
  lat: number | null
  lng: number | null
  accuracy_m: number | null
  location_source: LocationSource | null
  address_text: string | null
  ui_language: UiLanguage
  input_type: InputType
  language: string | null // ISO 639-1 code of what the reporter said, e.g. "ar"
  transcript_original: string | null
  transcript_english: string | null
  ai_summary: string | null
  confirmation_message: string | null // in the reporter's own language
  location_hint: string | null
  water_depth_cm: number | null
  location_type: LocationType | null
  water_in_living_space: boolean
  water_rising: boolean
  hazards: Hazards
  people_at_risk: PeopleAtRisk
  needs: Needs
  urgency_score: number | null // null while ai_status is 'pending'
  urgency_level: UrgencyLevel | null
  urgency_reasons: string[]
  status: ReportStatus
  ai_status: AIStatus
  ai_engine: string | null
  ai_error: string | null
  ai_latency_ms: number | null
  audio_url: string | null
  photo_url: string | null
  is_simulated: boolean
}

export interface AppConfig {
  public_url: string | null
  report_url: string | null
  ai_enabled: boolean
  ai_model: string | null
  storm_running: boolean
  storm_injected: number
  map_center: [number, number]
  map_zoom: number
}

export interface StormState {
  running: boolean
  injected: number
}

export interface Hotspot {
  label: string
  lat: number
  lng: number
  radius_m: number
  level: UrgencyLevel
  report_ids: number[]
}

export interface Briefing {
  text: string
  hotspots: Hotspot[]
  generated_at: string
  engine: string
}

/** Events from GET /api/events (SSE). 'snapshot' is synthesized by subscribeEvents in api.ts. */
export type ServerEvent =
  | { type: 'hello'; server_time: string }
  | { type: 'report.created'; report: Report }
  | { type: 'report.updated'; report: Report }
  | { type: 'storm.state'; running: boolean; injected: number }
  | { type: 'config.updated'; config: AppConfig }
  | { type: 'reset' }
  | { type: 'snapshot'; reports: Report[] }

export const LEVELS: UrgencyLevel[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']

export const LEVEL_RANK: Record<UrgencyLevel, number> = { CRITICAL: 4, HIGH: 3, MEDIUM: 2, LOW: 1 }

// Same values as the --fl-* tokens in index.css; Leaflet paths need plain colors.
export const LEVEL_COLOR: Record<UrgencyLevel, string> = {
  CRITICAL: '#e5484d',
  HIGH: '#f76b15',
  MEDIUM: '#f5b70a',
  LOW: '#3e8ef7',
}
export const PENDING_COLOR = '#8b95a7'

const STATUS_RANK: Record<ReportStatus, number> = { new: 0, dispatched: 1, resolved: 2 }

/**
 * Queue order: open reports before dispatched before resolved. Among open ones, reports still
 * being processed sit on top (they are seconds old), then CRITICAL to LOW, higher score, newest.
 */
export function compareReports(a: Report, b: Report): number {
  const byStatus = STATUS_RANK[a.status] - STATUS_RANK[b.status]
  if (byStatus !== 0) return byStatus
  const aPending = a.ai_status === 'pending' ? 1 : 0
  const bPending = b.ai_status === 'pending' ? 1 : 0
  if (aPending !== bPending) return bPending - aPending
  const aLevel = a.urgency_level ? LEVEL_RANK[a.urgency_level] : 0
  const bLevel = b.urgency_level ? LEVEL_RANK[b.urgency_level] : 0
  if (aLevel !== bLevel) return bLevel - aLevel
  const byScore = (b.urgency_score ?? 0) - (a.urgency_score ?? 0)
  if (byScore !== 0) return byScore
  const byTime = Date.parse(b.created_at) - Date.parse(a.created_at)
  if (byTime !== 0) return byTime
  return b.id - a.id // same instant (seeds have whole seconds): newer report first, like the server
}
