// Pure audio helpers for the voice-note recorder: streaming downsampler and WAV writer.
// No DOM or Web Audio here, so the node check in data-report-ui/ can run this file directly.

export const TARGET_RATE = 16000

/**
 * Turns float samples at the input rate into 16 kHz PCM16, chunk by chunk.
 *
 * Each output sample is the average of the input samples that fall in its time slot. Averaging is
 * a crude low-pass filter, but it is cheap enough for an old phone and keeps speech intelligible,
 * which is all the AI needs. The running sum carries across chunk boundaries, so the result does
 * not depend on how the audio callback happened to slice the stream.
 */
export class Downsampler {
  readonly inputRate: number
  readonly outputRate: number
  private ratio: number
  private sum = 0
  private count = 0
  private consumed = 0 // input samples seen so far
  private nextBoundary: number // input position where the current output slot ends

  constructor(inputRate: number, outputRate = TARGET_RATE) {
    this.inputRate = inputRate
    // Never upsample: a context slower than 16 kHz (rare) is recorded at its own rate.
    this.outputRate = inputRate < outputRate ? inputRate : outputRate
    this.ratio = this.inputRate / this.outputRate
    this.nextBoundary = this.ratio
  }

  /** Feed one chunk of mono float samples (-1..1); returns the PCM16 samples it completed. */
  push(input: Float32Array): Int16Array {
    const out = new Int16Array(Math.ceil(input.length / this.ratio) + 1)
    let n = 0
    for (let i = 0; i < input.length; i++) {
      this.sum += input[i]
      this.count += 1
      this.consumed += 1
      if (this.consumed >= this.nextBoundary) {
        out[n++] = floatToPcm16(this.sum / this.count)
        this.sum = 0
        this.count = 0
        this.nextBoundary += this.ratio
      }
    }
    return out.subarray(0, n)
  }
}

export function floatToPcm16(value: number): number {
  const s = value > 1 ? 1 : value < -1 ? -1 : value
  // Asymmetric scale so -1 maps to -32768 and +1 to 32767 without overflow.
  return s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff)
}

/** Joins PCM16 chunks into one array. */
export function concatPcm(chunks: Int16Array[]): Int16Array {
  let total = 0
  for (const c of chunks) total += c.length
  const all = new Int16Array(total)
  let offset = 0
  for (const c of chunks) {
    all.set(c, offset)
    offset += c.length
  }
  return all
}

/** A canonical 44-byte RIFF/WAVE header followed by mono 16-bit little-endian PCM. */
export function encodeWavBuffer(samples: Int16Array, sampleRate: number): ArrayBuffer {
  const channels = 1
  const bytesPerSample = 2
  const dataBytes = samples.length * bytesPerSample
  const buffer = new ArrayBuffer(44 + dataBytes)
  const view = new DataView(buffer)

  writeAscii(view, 0, 'RIFF')
  view.setUint32(4, 36 + dataBytes, true) // file size minus the 8 bytes of "RIFF" + this field
  writeAscii(view, 8, 'WAVE')
  writeAscii(view, 12, 'fmt ')
  view.setUint32(16, 16, true) // fmt chunk size for PCM
  view.setUint16(20, 1, true) // audio format 1 = integer PCM
  view.setUint16(22, channels, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * channels * bytesPerSample, true) // byte rate
  view.setUint16(32, channels * bytesPerSample, true) // block align
  view.setUint16(34, 16, true) // bits per sample
  writeAscii(view, 36, 'data')
  view.setUint32(40, dataBytes, true)

  // DataView keeps the bytes little-endian whatever the platform's own byte order is.
  let offset = 44
  for (let i = 0; i < samples.length; i++, offset += 2) view.setInt16(offset, samples[i], true)
  return buffer
}

export function encodeWav(samples: Int16Array, sampleRate: number): Blob {
  return new Blob([encodeWavBuffer(samples, sampleRate)], { type: 'audio/wav' })
}

function writeAscii(view: DataView, offset: number, text: string) {
  for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i))
}
