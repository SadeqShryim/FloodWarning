// POST /api/reports with upload progress, for the phone page.
//
// api.createReport uses fetch, which cannot report upload progress. On a weak signal through the
// tunnel a 60 s voice note (about 2 MB) can take half a minute to go up, and a frozen spinner for
// that long looks broken, so the phone page sends the same multipart form with XMLHttpRequest
// (which every old phone supports) and shows how far along it is.
import { ApiError } from '../api'
import type { NewReport } from '../api'
import type { Report } from '../types'

export type UploadFailure = 'network' | 'timeout' | 'aborted'

/** The request never got an HTTP answer (offline, tunnel down, stalled, or stopped by us). */
export class UploadError extends Error {
  reason: UploadFailure

  constructor(reason: UploadFailure) {
    super(reason)
    this.reason = reason
  }
}

export interface UploadOptions {
  /** 0..1 as the body goes up; 1 once it is all sent (the server may still be working). */
  onProgress?: (fraction: number) => void
  /** Give up when nothing has moved for this long (not a cap on the whole upload). */
  stallTimeoutMs?: number
  signal?: AbortSignal
}

/** Same fields as api.createReport, so the server cannot tell the two apart. */
export function buildReportForm(input: NewReport): FormData {
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
  return form
}

export function uploadReport(input: NewReport, options: UploadOptions = {}): Promise<Report> {
  const { onProgress, stallTimeoutMs = 45000, signal } = options
  return new Promise<Report>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    let settled = false
    let stallTimer = 0

    const finish = (err: Error | null, report?: Report) => {
      if (settled) return
      settled = true
      window.clearTimeout(stallTimer)
      if (signal) signal.removeEventListener('abort', onAbort)
      if (err) reject(err)
      else resolve(report as Report)
    }
    // Any sign of life (bytes going up, the answer starting to arrive) restarts the clock, so a slow
    // but moving upload is never cut off; a dead tunnel is given up on after stallTimeoutMs.
    const alive = () => {
      window.clearTimeout(stallTimer)
      stallTimer = window.setTimeout(() => {
        finish(new UploadError('timeout'))
        xhr.abort()
      }, stallTimeoutMs)
    }
    const onAbort = () => {
      finish(new UploadError('aborted'))
      xhr.abort()
    }

    xhr.open('POST', '/api/reports')
    if (xhr.upload) {
      xhr.upload.onprogress = (event) => {
        alive()
        if (event.lengthComputable && event.total > 0) onProgress?.(Math.min(1, event.loaded / event.total))
      }
      xhr.upload.onload = () => {
        alive()
        onProgress?.(1)
      }
    }
    xhr.onprogress = alive
    xhr.onerror = () => finish(new UploadError('network'))
    xhr.ontimeout = () => finish(new UploadError('timeout'))
    xhr.onabort = () => finish(new UploadError('aborted'))
    xhr.onload = () => {
      let body: unknown = null
      try {
        body = JSON.parse(xhr.responseText)
      } catch {
        // not JSON (a tunnel error page, for example)
      }
      if (xhr.status >= 200 && xhr.status < 300 && body && typeof body === 'object') {
        finish(null, body as Report)
        return
      }
      // status 0 with onload means the connection broke without an HTTP answer.
      if (xhr.status === 0) {
        finish(new UploadError('network'))
        return
      }
      const detail = body && typeof (body as { detail?: unknown }).detail === 'string' ? (body as { detail: string }).detail : ''
      finish(new ApiError(xhr.status, detail || `${xhr.status} ${xhr.statusText}`))
    }

    if (signal) {
      if (signal.aborted) {
        finish(new UploadError('aborted'))
        return
      }
      signal.addEventListener('abort', onAbort)
    }
    alive()
    xhr.send(buildReportForm(input))
  })
}
