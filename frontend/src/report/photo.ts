// Shrinks a phone photo (often 4-12 MB) to a JPEG of at most 1280 px before upload, so it goes
// through on a weak signal. If anything about that fails, the original file is sent instead.

export interface PreparedPhoto {
  blob: Blob
  filename: string
}

const MAX_SIDE = 1280

export async function preparePhoto(file: File): Promise<PreparedPhoto> {
  try {
    const blob = await downscale(file)
    if (blob && blob.size > 0 && blob.size < file.size) return { blob, filename: 'photo.jpg' }
  } catch {
    // fall through to the original
  }
  return { blob: file, filename: file.name || 'photo.jpg' }
}

function downscale(file: File): Promise<Blob | null> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    const timer = window.setTimeout(() => done(new Error('image load timeout')), 15000)
    function done(err: Error | null, blob?: Blob | null) {
      window.clearTimeout(timer)
      URL.revokeObjectURL(url)
      if (err) reject(err)
      else resolve(blob ?? null)
    }
    img.onerror = () => done(new Error('image could not be decoded'))
    img.onload = () => {
      try {
        const w = img.naturalWidth
        const h = img.naturalHeight
        if (!w || !h) return done(new Error('empty image'))
        const scale = Math.min(1, MAX_SIDE / Math.max(w, h))
        const canvas = document.createElement('canvas')
        canvas.width = Math.round(w * scale)
        canvas.height = Math.round(h * scale)
        const ctx = canvas.getContext('2d')
        if (!ctx) return done(new Error('no 2d context'))
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
        canvas.toBlob((blob) => done(null, blob), 'image/jpeg', 0.8)
      } catch (err) {
        done(err instanceof Error ? err : new Error(String(err)))
      }
    }
    img.src = url
  })
}
