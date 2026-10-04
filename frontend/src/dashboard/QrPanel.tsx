// QR code judges scan to open /report on their phone. Sized to scan from a few meters off a projector.
import { QRCodeSVG } from 'qrcode.react'
import { useState, type FormEvent } from 'react'
import { api } from '../api'
import type { AppConfig } from '../types'

interface QrPanelProps {
  config: AppConfig | null
  onConfig: (config: AppConfig) => void
  onClose: () => void
}

export default function QrPanel({ config, onConfig, onClose }: QrPanelProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Without a public (tunnel) URL, phones on another network cannot reach the laptop, and
  // browsers only allow the microphone over https. Show the local address but say so: once the
  // config has loaded (before that the tunnel URL is simply not known yet), and only on a plain-http
  // page (a dashboard opened through the tunnel already shows a phone-ready address).
  const reportUrl = config?.report_url ?? `${window.location.origin}/report`
  const isLocal = !config?.report_url && window.location.protocol !== 'https:'
  const warn = isLocal && config !== null

  const startEdit = () => {
    setDraft(config?.public_url ?? '')
    setError(null)
    setEditing(true)
  }

  const save = async (event: FormEvent) => {
    event.preventDefault()
    const value = draft.trim()
    if (value && !/^https?:\/\//i.test(value)) {
      setError('Start the address with https://')
      return
    }
    setSaving(true)
    setError(null)
    try {
      onConfig(await api.setPublicUrl(value || null))
      setEditing(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save the address.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="fl-qr" aria-label="Report from your phone">
      <div className="fl-qr-head">
        <h2>Report from your phone</h2>
        <button type="button" className="fl-icon-btn" onClick={onClose} aria-label="Hide QR code">
          <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
            <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </button>
      </div>
      <div className="fl-qr-code">
        <QRCodeSVG value={reportUrl} size={232} level="M" marginSize={2} bgColor="#ffffff" fgColor="#0e1419" title={reportUrl} />
      </div>
      <p className="fl-qr-url">{reportUrl}</p>
      {warn && (
        <p className="fl-qr-warn">
          This is the laptop's local address. Phones need the https tunnel URL for voice: paste it below.
        </p>
      )}
      {editing ? (
        <form className="fl-qr-form" onSubmit={save}>
          <label htmlFor="fl-public-url">Public https address</label>
          <input
            id="fl-public-url"
            type="url"
            inputMode="url"
            placeholder="https://example.trycloudflare.com"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            autoFocus
            spellCheck={false}
          />
          <div className="fl-qr-form-row">
            <button type="submit" className="fl-btn is-primary" disabled={saving}>
              {saving ? 'Saving...' : 'Save address'}
            </button>
            <button type="button" className="fl-btn is-ghost" disabled={saving} onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
          {error && (
            <p className="fl-error" role="alert">
              {error}
            </p>
          )}
        </form>
      ) : (
        <button type="button" className="fl-link-btn" onClick={startEdit}>
          {isLocal ? 'Paste the tunnel address' : 'Change address'}
        </button>
      )}
    </section>
  )
}
