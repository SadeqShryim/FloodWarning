// One-shot GPS fix for the report. A failure is never fatal: the reporter can type an address,
// or the AI can find the place they mention in the voice note.

export interface Fix {
  lat: number
  lng: number
  accuracy: number // meters
}

export type LocationFailure = 'denied' | 'unavailable' | 'insecure' | 'unsupported'

export function requestLocation(timeoutMs = 10000): Promise<Fix> {
  return new Promise((resolve, reject) => {
    if (window.isSecureContext === false) return reject('insecure' as LocationFailure)
    if (!('geolocation' in navigator)) return reject('unsupported' as LocationFailure)
    let settled = false
    // Some browsers never call back at all when the permission prompt is dismissed; this guard
    // makes sure the status line does not say "locating" forever.
    const guard = window.setTimeout(() => finish(null, 'unavailable'), timeoutMs + 3000)
    function finish(fix: Fix | null, failure?: LocationFailure) {
      if (settled) return
      settled = true
      window.clearTimeout(guard)
      if (fix) resolve(fix)
      else reject(failure ?? 'unavailable')
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => finish({ lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy }),
      (err) => finish(null, err.code === err.PERMISSION_DENIED ? 'denied' : 'unavailable'),
      { enableHighAccuracy: true, timeout: timeoutMs, maximumAge: 60000 },
    )
  })
}
