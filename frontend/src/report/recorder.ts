// Voice-note recorder: microphone -> Web Audio -> 16 kHz mono PCM16 -> WAV Blob.
//
// We record WAV ourselves instead of using MediaRecorder because MediaRecorder's formats differ by
// browser (webm on Chrome, mp4 on Safari, missing on Safari 13), while WAV plays back everywhere
// and Gemini accepts it directly. 60 s of 16 kHz PCM16 is under 2 MB.
import { concatPcm, Downsampler, encodeWav } from './wav'

export type MicErrorCode = 'insecure' | 'unsupported' | 'denied' | 'notfound' | 'busy' | 'failed'

export class MicError extends Error {
  code: MicErrorCode

  constructor(code: MicErrorCode, message?: string) {
    super(message ?? code)
    this.code = code
  }
}

export interface RecorderOptions {
  maxSeconds?: number
  /** Loudness 0..1, called a few dozen times per second while recording. */
  onLevel?: (level: number) => void
  /** Called once when the recording hits maxSeconds and stopped itself. */
  onAutoStop?: (wav: Blob, seconds: number) => void
}

/** Why voice notes cannot work in this page at all (checked before showing the RECORD button). */
export function micUnavailableReason(): MicErrorCode | null {
  if (typeof window === 'undefined') return 'unsupported'
  // Browsers hide getUserMedia on plain http:// pages (except localhost).
  if (window.isSecureContext === false) return 'insecure'
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') return 'unsupported'
  if (!audioContextClass()) return 'unsupported'
  return null
}

function audioContextClass(): typeof AudioContext | undefined {
  const w = window as unknown as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext }
  return w.AudioContext ?? w.webkitAudioContext // old Safari only has the prefixed one
}

/** Maps a getUserMedia rejection to something we can explain to the reporter. */
export function classifyMicError(err: unknown): MicErrorCode {
  if (err instanceof MicError) return err.code
  const name = err && typeof err === 'object' && 'name' in err ? String((err as { name: unknown }).name) : ''
  switch (name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError': // old Chrome
    case 'SecurityError':
      return 'denied'
    case 'NotFoundError':
    case 'DevicesNotFoundError': // old Chrome
    case 'OverconstrainedError':
      return 'notfound'
    case 'NotReadableError':
    case 'TrackStartError': // old Chrome
    case 'AbortError':
      return 'busy'
    case 'NotSupportedError':
    case 'TypeError':
      return 'unsupported'
    default:
      return 'failed'
  }
}

// Runs on the audio thread. Batches the 128-frame render quanta into ~1024-frame messages so the
// main thread is not woken up 375 times a second.
const WORKLET_SOURCE = `
class FloodLineCapture extends AudioWorkletProcessor {
  constructor() {
    super()
    this.buffer = new Float32Array(1024)
    this.filled = 0
  }
  process(inputs) {
    const input = inputs[0]
    const channel = input && input[0]
    if (channel) {
      for (let i = 0; i < channel.length; i++) {
        this.buffer[this.filled++] = channel[i]
        if (this.filled === this.buffer.length) {
          this.port.postMessage(this.buffer)
          this.buffer = new Float32Array(1024)
          this.filled = 0
        }
      }
    }
    return true
  }
}
registerProcessor('floodline-capture', FloodLineCapture)
`

/**
 * One recorder per voice note: start(), then stop() for the WAV, or cancel() to throw it away.
 * Every path out (stop, cancel, auto-stop, error) releases the microphone so the phone's
 * "recording" indicator goes away.
 */
export class WavRecorder {
  private maxSeconds: number
  private onLevel?: (level: number) => void
  private onAutoStop?: (wav: Blob, seconds: number) => void

  private ctx: AudioContext | null = null
  private stream: MediaStream | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private node: AudioNode | null = null
  private sink: GainNode | null = null
  private moduleUrl: string | null = null
  private downsampler: Downsampler | null = null
  private chunks: Int16Array[] = []
  private samples = 0
  private active = false
  private finished = false

  constructor(options: RecorderOptions = {}) {
    this.maxSeconds = options.maxSeconds ?? 60
    this.onLevel = options.onLevel
    this.onAutoStop = options.onAutoStop
  }

  get recording(): boolean {
    return this.active
  }

  /** Seconds of audio captured so far (counted in samples, so it matches the file). */
  get elapsed(): number {
    return this.downsampler ? this.samples / this.downsampler.outputRate : 0
  }

  /**
   * Call this directly from the tap handler: iOS only lets an AudioContext start inside a user
   * gesture, so the context is created and resumed before the first await.
   */
  async start(): Promise<void> {
    if (this.active || this.finished) throw new Error('recorder already used')
    const reason = micUnavailableReason()
    if (reason) throw new MicError(reason)

    const Ctx = audioContextClass()!
    const ctx = new Ctx()
    this.ctx = ctx
    this.active = true
    void ctx.resume?.().catch(() => undefined)

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
    } catch (err) {
      this.teardown()
      throw new MicError(classifyMicError(err))
    }
    // cancel() may have been called while the permission prompt was open.
    if (!this.active) {
      this.teardown()
      throw new MicError('failed', 'cancelled')
    }

    try {
      if (ctx.state === 'suspended') await ctx.resume()
      this.downsampler = new Downsampler(ctx.sampleRate)
      this.source = ctx.createMediaStreamSource(this.stream)
      // Processing nodes only run when something pulls on them, so route them into the speakers
      // through a muted gain (the reporter must not hear themselves).
      this.sink = ctx.createGain()
      this.sink.gain.value = 0
      this.sink.connect(ctx.destination)
      this.node = (await this.tryWorklet(ctx)) ?? this.scriptProcessor(ctx)
      this.source.connect(this.node)
      this.node.connect(this.sink)
    } catch (err) {
      this.teardown()
      throw new MicError('failed', err instanceof Error ? err.message : String(err))
    }
    if (!this.active) {
      this.teardown()
      throw new MicError('failed', 'cancelled')
    }
  }

  /** Stops recording and returns the WAV. Safe to call more than once. */
  async stop(): Promise<Blob> {
    const wav = this.buildWav()
    this.teardown()
    return wav
  }

  /** Throws the recording away and releases the microphone. */
  cancel(): void {
    this.chunks = []
    this.samples = 0
    this.teardown()
  }

  private buildWav(): Blob {
    const rate = this.downsampler ? this.downsampler.outputRate : 16000
    return encodeWav(concatPcm(this.chunks), rate)
  }

  private async tryWorklet(ctx: AudioContext): Promise<AudioNode | null> {
    if (!ctx.audioWorklet || typeof AudioWorkletNode === 'undefined') return null
    try {
      this.moduleUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: 'application/javascript' }))
      await ctx.audioWorklet.addModule(this.moduleUrl)
      const node = new AudioWorkletNode(ctx, 'floodline-capture', {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        channelCount: 1,
        channelCountMode: 'explicit', // the browser mixes stereo mics down to mono for us
      })
      node.port.onmessage = (event: MessageEvent<Float32Array>) => this.handleSamples(event.data)
      return node
    } catch {
      // Blob-URL modules can be blocked or unsupported; the ScriptProcessor path still works.
      return null
    }
  }

  private scriptProcessor(ctx: AudioContext): AudioNode {
    // Deprecated, but it is the only option on Safari 13 and older Android browsers.
    const node = ctx.createScriptProcessor(4096, 1, 1)
    node.onaudioprocess = (event: AudioProcessingEvent) => {
      // Copy: the browser reuses this buffer for the next callback.
      this.handleSamples(new Float32Array(event.inputBuffer.getChannelData(0)))
    }
    return node
  }

  private handleSamples(input: Float32Array) {
    if (!this.active || !this.downsampler) return
    const pcm = this.downsampler.push(input)
    const limit = Math.round(this.maxSeconds * this.downsampler.outputRate)
    const room = limit - this.samples
    const kept = pcm.length > room ? pcm.subarray(0, Math.max(0, room)) : pcm
    if (kept.length) {
      this.chunks.push(kept)
      this.samples += kept.length
    }

    if (this.onLevel) {
      let sumSquares = 0
      for (let i = 0; i < input.length; i++) sumSquares += input[i] * input[i]
      const rms = Math.sqrt(sumSquares / (input.length || 1))
      // sqrt again: speech RMS sits around 0.02-0.2, this spreads it over the meter.
      this.onLevel(Math.min(1, Math.sqrt(rms) * 1.8))
    }

    if (this.samples >= limit) {
      const seconds = this.elapsed
      const wav = this.buildWav()
      this.teardown()
      this.onAutoStop?.(wav, seconds)
    }
  }

  private teardown() {
    this.active = false
    this.finished = true
    if (this.node) {
      const port = (this.node as Partial<AudioWorkletNode>).port
      if (port) port.onmessage = null
      if ('onaudioprocess' in this.node) (this.node as ScriptProcessorNode).onaudioprocess = null
    }
    for (const n of [this.source, this.node, this.sink]) {
      try {
        n?.disconnect()
      } catch {
        // already disconnected
      }
    }
    this.source = this.node = this.sink = null
    this.stream?.getTracks().forEach((track) => track.stop())
    this.stream = null
    if (this.ctx) {
      // Phones allow only a few live AudioContexts; close each one we made.
      const ctx = this.ctx
      this.ctx = null
      try {
        void ctx.close?.().catch(() => undefined)
      } catch {
        // very old webkitAudioContext had no close()
      }
    }
    if (this.moduleUrl) {
      URL.revokeObjectURL(this.moduleUrl)
      this.moduleUrl = null
    }
  }
}
