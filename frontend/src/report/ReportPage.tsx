// /report: the phone page a resident opens from the QR code. One big action per screen:
// record -> stop -> send -> "report received". Typing is always available as a fallback.
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ChangeEvent, ReactNode } from 'react'
import { ApiError, api, createReport } from '../api'
import type { Report, UiLanguage } from '../types'
import { initialLanguage, isRtl, LANGUAGES, saveLanguage, STRINGS } from './i18n'
import type { Strings } from './i18n'
import {
  CameraIcon,
  CheckIcon,
  MarkIcon,
  MicIcon,
  PauseIcon,
  PencilIcon,
  PhoneIcon,
  PinIcon,
  PlayIcon,
  StopIcon,
} from './icons'
import { requestLocation } from './location'
import type { Fix, LocationFailure } from './location'
import { preparePhoto } from './photo'
import { classifyMicError, micUnavailableReason, WavRecorder } from './recorder'
import type { MicErrorCode } from './recorder'
import './report.css'

type Screen = 'idle' | 'recording' | 'review' | 'typing' | 'sending' | 'done'
type MicProblem = MicErrorCode | 'empty'
type SendProblem = 'network' | 'timeout' | 'server' | 'tooLarge' | 'missing' | 'textEmpty'
type LocState = { kind: 'locating' } | { kind: 'found'; fix: Fix } | { kind: 'failed'; reason: LocationFailure }

interface Recording {
  blob: Blob
  url: string
  seconds: number
}

interface Photo {
  blob: Blob
  filename: string
  previewUrl: string
}

const MAX_SECONDS = 60
const SEND_TIMEOUT_MS = 45000
const POLL_EVERY_MS = 1500
const POLL_FOR_MS = 20000

function formatClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const rest = s % 60
  return `${Math.floor(s / 60)}:${rest < 10 ? '0' : ''}${rest}`
}

function micMessage(t: Strings, problem: MicProblem): string {
  switch (problem) {
    case 'insecure':
      return t.micInsecure
    case 'unsupported':
      return t.micUnsupported
    case 'denied':
      return t.micDenied
    case 'notfound':
      return t.micNotFound
    case 'busy':
      return t.micBusy
    case 'empty':
      return t.micEmpty
    default:
      return t.micFailed
  }
}

function sendMessage(t: Strings, problem: SendProblem): string {
  switch (problem) {
    case 'timeout':
      return t.errTimeout
    case 'server':
      return t.errServer
    case 'tooLarge':
      return t.errTooLarge
    case 'missing':
      return t.errMissing
    case 'textEmpty':
      return t.textEmpty
    default:
      return t.errNetwork
  }
}

function classifySendError(err: unknown, timedOut: boolean): SendProblem {
  if (timedOut) return 'timeout'
  if (err instanceof ApiError) {
    if (err.status === 413) return 'tooLarge'
    if (err.status === 422) return 'missing'
    return 'server'
  }
  return 'network' // fetch rejects with a TypeError when the phone is offline or the tunnel drops
}

/** The few facts we read back to the reporter so they know they were understood. */
function understoodChips(t: Strings, r: Report): string[] {
  const chips: string[] = []
  if (r.water_depth_cm && r.water_depth_cm > 0) chips.push(t.chipDepth(r.water_depth_cm))
  if (r.water_rising) chips.push(t.chipRising)
  if (r.location_type === 'basement') chips.push(t.chipBasement)
  else if (r.water_in_living_space) chips.push(t.chipLivingSpace)
  if (r.location_type === 'car') chips.push(t.chipCar)
  const p = r.people_at_risk
  if (p.trapped) chips.push(t.chipTrapped)
  if (p.medical || r.needs.medical) chips.push(t.chipMedical)
  if (p.elderly) chips.push(t.chipElderly)
  if (p.children) chips.push(t.chipChildren)
  if (p.disabled) chips.push(t.chipDisabled)
  if (p.count != null && p.count >= 2) chips.push(t.chipPeople(p.count))
  return chips
}

export default function ReportPage() {
  const [lang, setLang] = useState<UiLanguage>(initialLanguage)
  const t = STRINGS[lang]
  const rtl = isRtl(lang)

  const [screen, setScreen] = useState<Screen>('idle')
  const [micBlocked] = useState<MicErrorCode | null>(micUnavailableReason)
  const [micProblem, setMicProblem] = useState<MicProblem | null>(null)
  const [micReady, setMicReady] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [recording, setRecording] = useState<Recording | null>(null)
  const [playing, setPlaying] = useState(false)
  const [text, setText] = useState('')
  const [showDetails, setShowDetails] = useState(false)
  const [photo, setPhoto] = useState<Photo | null>(null)
  const [photoBusy, setPhotoBusy] = useState(false)
  const [loc, setLoc] = useState<LocState>({ kind: 'locating' })
  const [address, setAddress] = useState('')
  const [sendProblem, setSendProblem] = useState<SendProblem | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [pollGaveUp, setPollGaveUp] = useState(false)
  const [announcement, setAnnouncement] = useState('')

  const mountedRef = useRef(false)
  const recorderRef = useRef<WavRecorder | null>(null)
  const tickRef = useRef(0)
  const abortRef = useRef<AbortController | null>(null)
  const pollRef = useRef(0)
  const pollGeneration = useRef(0)
  const ringRef = useRef<HTMLSpanElement | null>(null)
  const headingRef = useRef<HTMLHeadingElement | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)
  const detailsRef = useRef<HTMLTextAreaElement | null>(null)
  // Object URLs live in refs too, so the unmount cleanup can revoke the latest ones.
  const recordingUrlRef = useRef<string | null>(null)
  const photoUrlRef = useRef<string | null>(null)
  const firstScreen = useRef(true)

  // Language: <html dir/lang> (so the whole page mirrors for Arabic), tab title, remembered choice.
  useEffect(() => {
    document.documentElement.lang = lang
    document.documentElement.dir = rtl ? 'rtl' : 'ltr'
    document.title = t.docTitle
    saveLanguage(lang)
  }, [lang, rtl, t])

  // Leaving /report (the SPA never does today, but be tidy): restore the default direction.
  useEffect(
    () => () => {
      document.documentElement.dir = 'ltr'
      document.documentElement.lang = 'en'
    },
    [],
  )

  // Ask the browser for a fix; the answer lands in `loc` whenever it arrives.
  const requestFix = useCallback(() => {
    requestLocation().then(
      (fix) => {
        if (mountedRef.current) setLoc({ kind: 'found', fix })
      },
      (reason: LocationFailure) => {
        if (mountedRef.current) setLoc({ kind: 'failed', reason })
      },
    )
  }, [])

  // The Retry button: back to "locating" while the new attempt runs.
  const locate = () => {
    setLoc({ kind: 'locating' })
    requestFix()
  }

  const stopTick = () => {
    window.clearInterval(tickRef.current)
    tickRef.current = 0
  }

  const stopPolling = () => {
    pollGeneration.current += 1
    window.clearTimeout(pollRef.current)
  }

  // Mount: ask for the location right away (it takes a few seconds). Unmount: release the mic,
  // stop network work and free the in-memory audio/photo.
  useEffect(() => {
    mountedRef.current = true
    requestFix() // `loc` already starts as "locating"
    return () => {
      mountedRef.current = false
      recorderRef.current?.cancel()
      recorderRef.current = null
      stopTick()
      stopPolling()
      abortRef.current?.abort()
      abortRef.current = null
      if (recordingUrlRef.current) URL.revokeObjectURL(recordingUrlRef.current)
      if (photoUrlRef.current) URL.revokeObjectURL(photoUrlRef.current)
    }
  }, [requestFix])

  // Move focus to the new screen's heading so screen readers (and keyboard users) start there.
  // While recording, focus stays on the big button, which has become the Stop button.
  useEffect(() => {
    if (firstScreen.current) {
      firstScreen.current = false
      return
    }
    if (screen !== 'recording') headingRef.current?.focus()
  }, [screen])

  const announce = (message: string) => setAnnouncement(message)

  const setLevel = (level: number) => {
    const ring = ringRef.current
    if (ring) ring.style.transform = `scale(${(1 + level * 0.18).toFixed(3)})`
  }

  // ---- recording -------------------------------------------------------------------------

  const keepRecording = (blob: Blob, seconds: number) => {
    stopTick()
    recorderRef.current = null
    setMicReady(false)
    setLevel(0)
    if (seconds < 0.6 || blob.size <= 44) {
      setMicProblem('empty')
      setScreen(recordingUrlRef.current ? 'review' : 'idle')
      return
    }
    if (recordingUrlRef.current) URL.revokeObjectURL(recordingUrlRef.current)
    const url = URL.createObjectURL(blob)
    recordingUrlRef.current = url
    setRecording({ blob, url, seconds })
    setPlaying(false)
    setScreen('review')
    announce(t.annStopped(t.seconds(Math.round(seconds))))
  }

  // Must stay synchronous up to rec.start(): iOS only unlocks audio inside the tap itself.
  const startRecording = () => {
    if (recorderRef.current) return
    audioRef.current?.pause()
    setMicProblem(null)
    setSendProblem(null)
    setMicReady(false)
    setElapsed(0)
    const rec = new WavRecorder({
      maxSeconds: MAX_SECONDS,
      onLevel: setLevel,
      onAutoStop: (wav, seconds) => {
        if (recorderRef.current === rec && mountedRef.current) keepRecording(wav, seconds)
      },
    })
    recorderRef.current = rec
    setScreen('recording')
    rec.start().then(
      () => {
        if (recorderRef.current !== rec) return
        setMicReady(true)
        announce(t.annRecording)
        tickRef.current = window.setInterval(() => setElapsed(rec.elapsed), 200)
      },
      (err: unknown) => {
        if (recorderRef.current !== rec || !mountedRef.current) return // cancelled meanwhile
        recorderRef.current = null
        setMicProblem(classifyMicError(err))
        setScreen(recordingUrlRef.current ? 'review' : 'idle')
      },
    )
  }

  const stopRecording = () => {
    const rec = recorderRef.current
    if (!rec) return
    if (!micReady) {
      // Still waiting for the permission prompt: treat Stop as Cancel.
      cancelRecording()
      return
    }
    recorderRef.current = null // a second tap while stopping must not build a second file
    const seconds = rec.elapsed
    void rec.stop().then((wav) => {
      if (mountedRef.current) keepRecording(wav, seconds)
    })
  }

  const cancelRecording = () => {
    recorderRef.current?.cancel()
    recorderRef.current = null
    stopTick()
    setMicReady(false)
    setLevel(0)
    setScreen(recordingUrlRef.current ? 'review' : 'idle')
    announce(t.annCancelled)
  }

  const togglePlay = () => {
    const audio = audioRef.current
    if (!audio) return
    if (audio.paused) {
      void audio.play()?.catch(() => setPlaying(false))
    } else {
      audio.pause()
    }
  }

  // ---- photo -----------------------------------------------------------------------------

  const onPhotoPicked = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files && event.target.files[0]
    event.target.value = '' // so picking the same photo again still fires change
    if (!file) return
    setPhotoBusy(true)
    preparePhoto(file).then((prepared) => {
      if (!mountedRef.current) return
      if (photoUrlRef.current) URL.revokeObjectURL(photoUrlRef.current)
      const previewUrl = URL.createObjectURL(prepared.blob)
      photoUrlRef.current = previewUrl
      setPhoto({ ...prepared, previewUrl })
      setPhotoBusy(false)
    })
  }

  const removePhoto = () => {
    if (photoUrlRef.current) URL.revokeObjectURL(photoUrlRef.current)
    photoUrlRef.current = null
    setPhoto(null)
  }

  // ---- sending ---------------------------------------------------------------------------

  const startPolling = (id: number) => {
    stopPolling()
    const generation = pollGeneration.current
    const startedAt = Date.now()
    const tick = async () => {
      try {
        const latest = await api.getReport(id)
        if (!mountedRef.current || generation !== pollGeneration.current) return
        setReport(latest)
        if (latest.ai_status !== 'pending') {
          announce(latest.confirmation_message?.trim() || t.annUnderstood)
          return
        }
      } catch {
        if (!mountedRef.current || generation !== pollGeneration.current) return
        // a missed poll is fine; the report is already saved
      }
      if (Date.now() - startedAt >= POLL_FOR_MS) {
        setPollGaveUp(true)
        return
      }
      pollRef.current = window.setTimeout(tick, POLL_EVERY_MS)
    }
    pollRef.current = window.setTimeout(tick, POLL_EVERY_MS)
  }

  const send = async () => {
    const typed = text.trim()
    const backTo: Screen = screen === 'typing' ? 'typing' : 'review'
    if (!recording && !typed) {
      setSendProblem(screen === 'typing' ? 'textEmpty' : 'missing')
      return
    }
    audioRef.current?.pause()
    setSendProblem(null)
    setScreen('sending')
    announce(t.annSending)

    const controller = new AbortController()
    abortRef.current = controller
    let timedOut = false
    const timer = window.setTimeout(() => {
      timedOut = true
      controller.abort()
    }, SEND_TIMEOUT_MS)
    const fix = loc.kind === 'found' ? loc.fix : null

    try {
      const saved = await createReport(
        {
          audio: recording ? recording.blob : null,
          audioFilename: 'voice-note.wav',
          photo: photo ? photo.blob : null,
          photoFilename: photo ? photo.filename : undefined,
          text: typed || undefined,
          lat: fix ? fix.lat : null,
          lng: fix ? fix.lng : null,
          accuracyM: fix ? fix.accuracy : null,
          addressText: address.trim() || undefined,
          uiLanguage: lang,
        },
        controller.signal,
      )
      if (!mountedRef.current) return
      setReport(saved)
      setPollGaveUp(false)
      setScreen('done')
      announce(t.annReceived(saved.id))
      if (saved.ai_status === 'pending') startPolling(saved.id)
    } catch (err) {
      // Aborted because the page is closing: nothing to show.
      if (!mountedRef.current || abortRef.current !== controller) return
      // Everything the reporter made (recording, text, photo) is still in state for "Try again".
      setSendProblem(classifySendError(err, timedOut))
      setScreen(backTo)
    } finally {
      window.clearTimeout(timer)
      if (abortRef.current === controller) abortRef.current = null
    }
  }

  const startOver = () => {
    stopPolling()
    audioRef.current?.pause()
    if (recordingUrlRef.current) URL.revokeObjectURL(recordingUrlRef.current)
    recordingUrlRef.current = null
    removePhoto()
    setRecording(null)
    setPlaying(false)
    setText('')
    setShowDetails(false)
    setReport(null)
    setPollGaveUp(false)
    setSendProblem(null)
    setMicProblem(null)
    setElapsed(0)
    // Location and typed address stay: the next report is most likely from the same place.
    setScreen('idle')
  }

  const goTyping = () => {
    setSendProblem(null)
    setScreen('typing')
  }

  const openDetails = () => {
    setShowDetails(true)
    window.setTimeout(() => detailsRef.current?.focus(), 0)
  }

  // ---- pieces ----------------------------------------------------------------------------

  const languageSwitcher = (
    <div className="fl-r-langs" role="group" aria-label={t.langSwitcher}>
      {LANGUAGES.map((code) => (
        <button
          key={code}
          type="button"
          lang={code}
          className={code === lang ? 'fl-r-lang is-on' : 'fl-r-lang'}
          aria-pressed={code === lang}
          aria-label={STRINGS[code].langFull}
          onClick={() => setLang(code)}
        >
          {STRINGS[code].langShort}
        </button>
      ))}
    </div>
  )

  // The first screen shows only the status line (RECORD is the one action there); the address
  // field appears on the review and typing screens, right before sending.
  const renderLocation = (withAddress: boolean) => (
    <div className="fl-r-loc">
      {loc.kind === 'locating' && (
        <p className="fl-r-locline">
          <span className="fl-r-dot is-busy" aria-hidden="true" />
          {t.locating}
        </p>
      )}
      {loc.kind === 'found' && (
        <p className="fl-r-locline is-ok">
          <PinIcon size={22} className="fl-r-locicon" />
          <span>{t.locFound(Math.max(1, Math.round(loc.fix.accuracy)))}</span>
        </p>
      )}
      {loc.kind === 'failed' && (
        <>
          <p className="fl-r-locline is-off">
            <PinIcon size={22} className="fl-r-locicon" />
            <span>
              {loc.reason === 'denied' ? t.locDenied : loc.reason === 'insecure' ? t.locInsecure : t.locUnavailable}
            </span>
            {loc.reason !== 'insecure' && loc.reason !== 'unsupported' && (
              <button type="button" className="fl-r-inline" onClick={locate}>
                {t.locRetry}
              </button>
            )}
          </p>
          {withAddress && (
            <>
              <label className="fl-r-label" htmlFor="fl-r-address">
                {t.addressLabel}
              </label>
              <input
                id="fl-r-address"
                className="fl-r-input"
                type="text"
                dir="auto"
                autoComplete="street-address"
                value={address}
                placeholder={t.addressPlaceholder}
                onChange={(e) => setAddress(e.target.value)}
              />
              <p className="fl-r-hint">{t.addressHint}</p>
            </>
          )}
        </>
      )}
    </div>
  )

  const photoBlock = (
    <div className="fl-r-photo">
      <input
        ref={fileRef}
        className="fl-r-file"
        type="file"
        accept="image/*"
        capture="environment"
        tabIndex={-1}
        aria-hidden="true"
        onChange={onPhotoPicked}
      />
      {photo ? (
        <div className="fl-r-photorow">
          <img className="fl-r-thumb" src={photo.previewUrl} alt="" />
          <div className="fl-r-photometa">
            <p className="fl-r-photook">
              <CheckIcon size={20} /> {t.photoAdded}
            </p>
            <button type="button" className="fl-r-inline" onClick={removePhoto}>
              {t.removePhoto}
            </button>
          </div>
        </div>
      ) : (
        <>
          <button
            type="button"
            className="fl-r-btn fl-r-btn-quiet"
            disabled={photoBusy}
            aria-busy={photoBusy}
            onClick={() => fileRef.current?.click()}
          >
            <CameraIcon size={26} className="fl-r-btnicon" />
            {t.addPhoto}
          </button>
          <p className="fl-r-hint">{t.photoOptional}</p>
        </>
      )}
    </div>
  )

  // Shown at the top of the screen, under the heading that receives focus after a failed send;
  // at the bottom it would hide behind the sticky Send button.
  const sendError = sendProblem && (
    <div className="fl-r-alert" role="alert">
      {sendMessage(t, sendProblem)}
    </div>
  )

  const sendButtonLabel =
    sendProblem && sendProblem !== 'missing' && sendProblem !== 'textEmpty' ? t.tryAgain : t.send

  // ---- screens ---------------------------------------------------------------------------

  let body: ReactNode
  if (screen === 'idle' || screen === 'recording') {
    const isRec = screen === 'recording'
    const fill = Math.min(100, (elapsed / MAX_SECONDS) * 100)
    body = (
      <section className="fl-r-screen is-center">
        <h1 className="fl-r-title" ref={headingRef} tabIndex={-1}>
          {isRec ? t.recordingTitle : t.idleTitle}
        </h1>
        <p className="fl-r-lead">{isRec ? (micReady ? t.recordingLead : t.allowMic) : t.idleLead}</p>

        {micBlocked && !isRec ? (
          <div className="fl-r-blocked">
            <div className="fl-r-alert" role="alert">
              {micMessage(t, micBlocked)}
            </div>
            <button type="button" className="fl-r-btn fl-r-btn-main" onClick={goTyping}>
              <PencilIcon size={26} className="fl-r-btnicon" />
              {t.typeYourReport}
            </button>
          </div>
        ) : (
          <>
            <div className={isRec ? 'fl-r-hero is-rec' : 'fl-r-hero'}>
              <span className="fl-r-ring" ref={ringRef} aria-hidden="true" />
              <button
                type="button"
                className="fl-r-big"
                aria-label={isRec ? t.stopAria : t.recordAria}
                onClick={isRec ? stopRecording : startRecording}
              >
                <span className="fl-r-water" style={{ height: isRec ? `${fill}%` : '0%' }} aria-hidden="true" />
                <span className="fl-r-bigface">
                  {isRec ? (
                    <>
                      <span className="fl-r-clock" dir="ltr">
                        {formatClock(elapsed)}
                      </span>
                      <span className="fl-r-biglabel">
                        <StopIcon size={30} className="fl-r-stopicon" />
                        {t.stop}
                      </span>
                    </>
                  ) : (
                    <>
                      <MicIcon size={72} />
                      <span className="fl-r-biglabel">{t.record}</span>
                    </>
                  )}
                </span>
              </button>
            </div>

            {micProblem && !isRec && (
              <div className="fl-r-alert" role="alert">
                {micMessage(t, micProblem)}
                {(micProblem === 'denied' || micProblem === 'notfound' || micProblem === 'unsupported') && (
                  <button type="button" className="fl-r-btn fl-r-btn-main fl-r-alertbtn" onClick={goTyping}>
                    {t.typeYourReport}
                  </button>
                )}
              </div>
            )}

            {isRec ? (
              <>
                <p className="fl-r-hint">{t.autoStopNote}</p>
                <button type="button" className="fl-r-textbtn" onClick={cancelRecording}>
                  {t.cancel}
                </button>
              </>
            ) : (
              <>
                <p className="fl-r-say">{t.recordHint}</p>
                {renderLocation(false)}
                <button type="button" className="fl-r-textbtn" onClick={goTyping}>
                  <PencilIcon size={22} className="fl-r-btnicon" />
                  {t.typeInstead}
                </button>
              </>
            )}
          </>
        )}
      </section>
    )
  } else if (screen === 'review') {
    body = (
      <section className="fl-r-screen">
        <h1 className="fl-r-title" ref={headingRef} tabIndex={-1}>
          {t.reviewTitle}
        </h1>
        <p className="fl-r-lead">{t.reviewLead}</p>
        {sendError}

        {recording && (
          <div className="fl-r-player">
            <audio
              ref={audioRef}
              src={recording.url}
              preload="auto"
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              onEnded={() => setPlaying(false)}
            />
            <button
              type="button"
              className="fl-r-play"
              aria-label={playing ? t.pause : t.play}
              onClick={togglePlay}
            >
              {playing ? <PauseIcon size={34} /> : <PlayIcon size={34} />}
            </button>
            <div className="fl-r-playmeta">
              <span className="fl-r-playlabel">{playing ? t.pause : t.play}</span>
              <span className="fl-r-playtime" dir="ltr">
                {formatClock(recording.seconds)}
              </span>
            </div>
          </div>
        )}

        <button type="button" className="fl-r-btn fl-r-btn-quiet" onClick={startRecording}>
          <MicIcon size={26} className="fl-r-btnicon" />
          {t.recordAgain}
        </button>

        {micProblem && (
          <div className="fl-r-alert" role="alert">
            {micMessage(t, micProblem)}
          </div>
        )}

        {showDetails ? (
          <div className="fl-r-field">
            <label className="fl-r-label" htmlFor="fl-r-details">
              {t.detailsLabel}
            </label>
            <textarea
              id="fl-r-details"
              ref={detailsRef}
              className="fl-r-textarea is-short"
              dir="auto"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          </div>
        ) : (
          <button type="button" className="fl-r-textbtn is-start" onClick={openDetails}>
            <PencilIcon size={22} className="fl-r-btnicon" />
            {t.addDetails}
          </button>
        )}

        {photoBlock}
        {renderLocation(true)}

        <button type="button" className="fl-r-btn fl-r-btn-main fl-r-send" onClick={send}>
          {sendButtonLabel}
        </button>
      </section>
    )
  } else if (screen === 'typing') {
    body = (
      <section className="fl-r-screen">
        <h1 className="fl-r-title" ref={headingRef} tabIndex={-1}>
          {t.typeTitle}
        </h1>
        <p className="fl-r-lead">{t.typeLead}</p>
        {sendError}

        {micBlocked && (
          <div className="fl-r-note" role="note">
            {micMessage(t, micBlocked)}
          </div>
        )}

        <div className="fl-r-field">
          <label className="fl-r-label" htmlFor="fl-r-text">
            {t.typeLabel}
          </label>
          <textarea
            id="fl-r-text"
            className="fl-r-textarea"
            dir="auto"
            value={text}
            placeholder={t.typePlaceholder}
            onChange={(e) => {
              setText(e.target.value)
              if (sendProblem === 'textEmpty' || sendProblem === 'missing') setSendProblem(null)
            }}
          />
        </div>

        {photoBlock}
        {renderLocation(true)}

        <button type="button" className="fl-r-btn fl-r-btn-main fl-r-send" onClick={send}>
          {sendButtonLabel}
        </button>

        {!micBlocked && (
          <button type="button" className="fl-r-textbtn" onClick={recording ? () => setScreen('review') : startRecording}>
            <MicIcon size={22} className="fl-r-btnicon" />
            {t.useVoice}
          </button>
        )}
      </section>
    )
  } else if (screen === 'sending') {
    body = (
      <section className="fl-r-screen is-center" aria-busy="true">
        <span className="fl-r-spinner" aria-hidden="true" />
        <h1 className="fl-r-title" ref={headingRef} tabIndex={-1}>
          {t.sending}
        </h1>
        <p className="fl-r-lead">{t.sendingLead}</p>
      </section>
    )
  } else {
    const r = report!
    const pending = r.ai_status === 'pending' && !pollGaveUp
    const chips = pending ? [] : understoodChips(t, r)
    const place = r.address_text || r.location_hint
    const message = r.confirmation_message?.trim() || t.savedGeneric
    body = (
      <section className="fl-r-screen is-center">
        <span className="fl-r-check" aria-hidden="true">
          <CheckIcon size={64} />
        </span>
        <h1 className="fl-r-title" ref={headingRef} tabIndex={-1}>
          {t.doneTitle}
        </h1>
        <p className="fl-r-number" dir="auto">
          {t.reportNumber(r.id)}
        </p>

        <div className="fl-r-card">
          {pending ? (
            <p className="fl-r-thinking">
              <span className="fl-r-dot is-busy" aria-hidden="true" />
              {t.understanding}
            </p>
          ) : (
            <>
              <p className="fl-r-message" dir="auto">
                {r.ai_status === 'pending' ? t.savedGeneric : message}
              </p>
              {(chips.length > 0 || place) && (
                <>
                  <h2 className="fl-r-subtitle">{t.understoodHeading}</h2>
                  <ul className="fl-r-chips">
                    {place && (
                      <li className="fl-r-chip is-place" dir="auto">
                        <PinIcon size={18} className="fl-r-chipicon" />
                        {place}
                      </li>
                    )}
                    {chips.map((chip) => (
                      <li key={chip} className="fl-r-chip">
                        {chip}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </div>

        <a className="fl-r-call" href="tel:911">
          <PhoneIcon size={26} className="fl-r-btnicon" />
          {t.done911}
        </a>

        <button type="button" className="fl-r-btn fl-r-btn-quiet" onClick={startOver}>
          {t.sendAnother}
        </button>
      </section>
    )
  }

  return (
    <div className={rtl ? 'fl-report is-rtl' : 'fl-report'} lang={lang} dir={rtl ? 'rtl' : 'ltr'}>
      <header className="fl-r-top">
        <span className="fl-r-brand" lang="en" dir="ltr">
          <MarkIcon size={26} className="fl-r-mark" />
          FloodLine
        </span>
        {languageSwitcher}
      </header>
      <a className="fl-r-911" href="tel:911">
        <PhoneIcon size={22} className="fl-r-911icon" />
        <span>{t.emergency}</span>
      </a>
      <main className="fl-r-main">{body}</main>
      <p className="fl-r-sr" role="status" aria-live="polite">
        {announcement}
      </p>
    </div>
  )
}
