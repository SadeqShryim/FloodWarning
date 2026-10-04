// Thin client for the FloodLine API. All paths are same-origin (/api/...): FastAPI serves the
// built app, and the Vite dev server proxies /api to the backend.
import type { AppConfig, Briefing, Report, ReportStatus, ServerEvent, StormState, UiLanguage } from './types'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body && typeof body.detail === 'string') message = body.detail
    } catch {
      // body was not JSON; keep the status text
    }
    throw new ApiError(res.status, message)
  }
  return (await res.json()) as T
}

function sendJson<T>(method: 'POST' | 'PATCH', path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export interface NewReport {
  audio?: Blob | null
  audioFilename?: string // e.g. "voice-note.wav"; the extension tells the server the format
  photo?: Blob | null
  photoFilename?: string
  text?: string // typed report (instead of, or in addition to, a voice note)
  lat?: number | null
  lng?: number | null
  accuracyM?: number | null
  addressText?: string // typed address when GPS is unavailable
  uiLanguage: UiLanguage
}

/** POST /api/reports. Resolves once the report is saved and the AI has finished (or the server stopped waiting). */
export function createReport(input: NewReport, signal?: AbortSignal): Promise<Report> {
  const form = new FormData()
  if (input.audio) form.append('audio', input.audio, input.audioFilename ?? 'voice-note.wav')
  if (input.photo) form.append('photo', input.photo, input.photoFilename ?? 'photo.jpg')
  const text = input.text?.trim()
  if (text) form.append('text', text)
  if (input.lat != null && input.lng != null) {
    form.append('lat', String(input.lat))
    form.append('lng', String(input.lng))
    if (input.accuracyM != null) form.append('accuracy_m', String(Math.round(input.accuracyM)))
  }
  const address = input.addressText?.trim()
  if (address) form.append('address_text', address)
  form.append('ui_language', input.uiLanguage)
  return request<Report>('/api/reports', { method: 'POST', body: form, signal })
}

export const api = {
  listReports: () => request<Report[]>('/api/reports'),
  getReport: (id: number) => request<Report>(`/api/reports/${id}`),
  createReport,
  setStatus: (id: number, status: ReportStatus) => sendJson<Report>('PATCH', `/api/reports/${id}`, { status }),
  reprocess: (id: number) => sendJson<Report>('POST', `/api/reports/${id}/reprocess`),
  getConfig: () => request<AppConfig>('/api/config'),
  setPublicUrl: (publicUrl: string | null) =>
    sendJson<AppConfig>('POST', '/api/config/public-url', { public_url: publicUrl }),
  startStorm: () => sendJson<StormState>('POST', '/api/storm/start'),
  stopStorm: () => sendJson<StormState>('POST', '/api/storm/stop'),
  briefing: () => sendJson<Briefing>('POST', '/api/briefing'),
  resetDemo: () => sendJson<{ ok: boolean; count: number }>('POST', '/api/admin/reset'),
}

export type LiveStatus = 'connecting' | 'live' | 'polling'

/**
 * Streams dashboard events from GET /api/events (Server-Sent Events).
 *
 * On every (re)connect it also emits a 'snapshot' (the full report list) plus the current config
 * and storm state, so events missed while disconnected never leave the UI stale. If the stream
 * cannot be established (some tunnels buffer text/event-stream), it polls every 3 s instead and
 * keeps retrying the stream in the background. Returns an unsubscribe function.
 */
export function subscribeEvents(
  onEvent: (event: ServerEvent) => void,
  onStatus?: (status: LiveStatus) => void,
): () => void {
  let closed = false
  let source: EventSource | null = null
  let failures = 0
  let polling = false
  let pollTimer = 0
  let helloTimer = 0
  let retryTimer = 0

  const setStatus = (status: LiveStatus) => {
    if (!closed) onStatus?.(status)
  }

  // Refreshes can overlap (the first load and the reload on 'hello', a poll and a 'reset'). An
  // answer that lands after a newer one is dropped, so the list never steps back in time.
  let refreshSeq = 0
  let appliedSeq = 0

  const refresh = async () => {
    const seq = ++refreshSeq
    try {
      const [reports, config] = await Promise.all([api.listReports(), api.getConfig()])
      if (closed || seq < appliedSeq) return
      appliedSeq = seq
      onEvent({ type: 'snapshot', reports })
      onEvent({ type: 'config.updated', config })
      onEvent({ type: 'storm.state', running: config.storm_running, injected: config.storm_injected })
    } catch {
      // server unreachable right now; the next poll or reconnect will try again
    }
  }

  const scheduleRetry = () => {
    window.clearTimeout(retryTimer)
    retryTimer = window.setTimeout(connect, 20000)
  }

  const stopPolling = () => {
    polling = false
    window.clearTimeout(pollTimer)
    window.clearTimeout(retryTimer)
  }

  const startPolling = () => {
    if (polling || closed) return
    polling = true
    setStatus('polling')
    const tick = async () => {
      if (!polling || closed) return
      await refresh()
      if (polling && !closed) pollTimer = window.setTimeout(tick, 3000)
    }
    void tick()
    scheduleRetry()
  }

  const dropSource = () => {
    window.clearTimeout(helloTimer)
    source?.close()
    source = null
  }

  function connect() {
    if (closed) return
    dropSource()
    if (!polling) setStatus('connecting')
    const es = new EventSource('/api/events')
    source = es

    // The server sends 'hello' immediately. If it never arrives, the stream is being buffered.
    helloTimer = window.setTimeout(() => {
      if (source !== es || closed) return
      dropSource()
      if (polling) scheduleRetry()
      else startPolling()
    }, 6000)

    es.onmessage = (message) => {
      let event: ServerEvent
      try {
        event = JSON.parse(message.data) as ServerEvent
      } catch {
        return
      }
      if (event.type === 'hello') {
        window.clearTimeout(helloTimer)
        failures = 0
        stopPolling()
        setStatus('live')
        void refresh()
        return
      }
      if (event.type === 'reset') void refresh()
      onEvent(event)
    }

    es.onerror = () => {
      if (source !== es || closed) return
      failures += 1
      if (es.readyState === EventSource.CLOSED || failures >= 3) {
        dropSource()
        if (polling) scheduleRetry()
        else startPolling()
      } else if (!polling) {
        setStatus('connecting') // EventSource retries on its own
      }
    }
  }

  connect()
  // Load the data now instead of waiting for the stream's 'hello': through a tunnel that buffers the
  // stream, 'hello' never comes, and the page would sit empty until polling starts (~6 s).
  void refresh()

  return () => {
    closed = true
    dropSource()
    window.clearTimeout(pollTimer)
    window.clearTimeout(retryTimer)
  }
}
