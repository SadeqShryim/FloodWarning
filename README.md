# FloodLine

**When the water comes up, the reports come in, in five languages, to nobody. FloodLine turns them into a ranked rescue map in seconds.**

When Dearborn, Michigan floods, anyone can report it by **voice note from a phone, in any language, with no app and no account**. They scan a QR code, tap one big button, and speak. FloodLine's AI (Google Gemini 2.5 Flash) transcribes and translates the note and pulls out the facts that matter: how deep the water is, whether it is rising, who is at risk, and what hazards are present. Clear, explainable rules then rank every report. Responders see one live map and one queue, auto-translated and ordered by who needs help first.

Built at Hack Dearborn 2026 (track: *Shaping Society*, theme: *Conjure Reality*).

> **FloodLine is a hackathon demo, not an emergency service.** It does not replace 911. If a life is in danger, call 911.

---

## The 3-minute demo

1. **Scan.** The dashboard shows a QR code. A judge scans it with their phone, which opens the reporting page (no install, no login). They tap **RECORD**, speak (try Arabic: *"الميّ عم تفوت عالبيت وجدّي ما فيه يطلع الدرج"*), and tap **SEND**.
2. **Pin drops.** About 5 seconds later a pin appears on the dashboard map, ranked **CRITICAL**, with the English translation, the original transcript, the water depth and who is at risk. The phone shows a confirmation in the reporter's own language.
3. **The queue.** About 25 reports across Dearborn (East Dearborn, Southend, Ford Woods, the Southfield Freeway underpasses...) are ranked by urgency. Click one to see the details and play the voice note, then mark it **Dispatched**.
4. **Storm mode.** Press *Storm mode*: new reports pour in live, each one processed and ranked as it lands, and the queue reshuffles in front of you. Press *AI Sitrep* for a briefing for the incident commander, with hotspot circles drawn on the map.
5. **Close.** "No app to download. Any language. Grandma can do this."

---

## Quickstart (Windows)

Requirements: **Python 3.12** (x64 build from python.org, which installs the `py` launcher), **Node.js 20+**, and an internet connection.

```powershell
git clone <this repo> floodline
cd floodline
powershell -ExecutionPolicy Bypass -File .\setup.ps1   # venv, Python + npm packages, cloudflared, .env
notepad .env                                           # optional: paste GEMINI_API_KEY=...
.\run.ps1 --open                                       # build, start, open the tunnel, open the dashboard
```

You get a free Gemini key at <https://aistudio.google.com/apikey>. **No key? Everything still works.** Reports are kept and ranked with a keyword fallback (typed reports) or flagged *needs review* (voice notes).

When it starts, `run.ps1` prints something like:

```
========================================================================
  FloodLine is running

  Dashboard (this laptop):  http://localhost:8000/dashboard
  Phones (scan the QR):     https://some-random-words.trycloudflare.com/report
  Public dashboard:         https://some-random-words.trycloudflare.com/dashboard

  Reports loaded: 25    AI: Gemini (gemini-2.5-flash)
  Press Ctrl+C to stop.
========================================================================
```

The QR code on the dashboard already points at the phone URL. **Ctrl+C** stops everything.

### `run.ps1` options

`run.ps1` passes its arguments to `run.py` (you can also run `.venv\Scripts\python.exe run.py ...` directly).

| Option | What it does |
|---|---|
| `--open` | Open the dashboard in your default browser |
| `--no-tunnel` | Local only: no public https link (phones can only use typed reports on the same Wi-Fi) |
| `--port 8000` | Local port (default 8000) |
| `--reset` | Delete the data folder first: fresh database with the 25 demo reports |
| `--build` | Rebuild the frontend even if `frontend/dist` exists (it is built automatically the first time) |
| `--skip-build` | Never build the frontend (API only, if there is no build yet) |

The dashboard also has a **Reset demo** button that restores the 25 demo reports without restarting.

### Developing

```powershell
.\run.ps1 --no-tunnel                      # backend on :8000
cd frontend; npm run dev                   # Vite on :5173, proxies /api to :8000
cd backend; ..\.venv\Scripts\python.exe -m pytest -q   # tests (no network, Gemini mocked)
.venv\Scripts\python.exe scripts\check_tunnel.py      # can phones reach a tunnel from this network?
```

---

## How it works

```
  Phone (any browser)                     Laptop: one Python process (FastAPI + uvicorn)
 ┌──────────────────┐   https      ┌──────────────────────────────────────────────────────────┐
 │ /report          │  quick       │  POST /api/reports                                       │
 │  RECORD → WAV    ├──tunnel─────►│   1. save the report + audio  (status: processing)       │
 │  GPS / address   │ (cloudflared)│   2. AI step, in parallel with geocoding:                │
 │  optional photo  │              │        Gemini 2.5 Flash: audio → transcript,             │
 │  EN / عربي / ES  │◄─────────────┤        translation, depth, people, hazards, place said  │
 │ "Report received"│ confirmation │        Nominatim (OpenStreetMap): address ↔ coordinates  │
 └──────────────────┘ in their own │   3. urgency rules → CRITICAL / HIGH / MEDIUM / LOW      │
                      language     │   4. SQLite + live event to every dashboard              │
                                   │                                                          │
  Laptop browser                   │  GET /api/events (Server-Sent Events)                    │
 ┌──────────────────┐              │  POST /api/briefing  → AI sitrep + hotspot clusters      │
 │ /dashboard       │◄─────────────┤  serves the built React app (frontend/dist)              │
 │ Leaflet + OSM map│   live       └──────────────────────────────────────────────────────────┘
 │ ranked queue     │   updates
 │ QR code, Sitrep  │
 └──────────────────┘
```

- **Frontend:** React + TypeScript (Vite). The phone page is deliberately light: no frameworks beyond React, system fonts, large tap targets, and a WAV recorder built on the Web Audio API that works on old phones. The dashboard uses Leaflet with OpenStreetMap tiles, so there is **no map API key**.
- **Backend:** FastAPI, SQLite, and an in-process event broker. FastAPI serves the built frontend, so the whole app is one process: no accounts, no microservices, no deploy pipeline.
- **Tunnel:** a free Cloudflare *quick tunnel* gives the laptop a public `https://` address. Phones need https because browsers only allow the microphone on secure pages.

### The AI pipeline

Gemini is used three ways, all tied to the map:

1. **Voice note → ranked pin.** One Gemini call gets the audio (plus the photo and any typed text) and returns structured JSON: a verbatim transcript in the original script, a faithful English translation, a one-line summary for responders, a calm confirmation in the reporter's language, the water depth in cm (it converts "knee-deep" or "two feet"), the location type, whether the water is rising, people at risk (elderly, children, disabled, medical, trapped, count), hazards (electrical, sewage, gas, structural), and needs. The prompt knows about Dearborn's large Arabic-speaking community (Lebanese, Yemeni and Iraqi dialects) and about mixed-language speech.
2. **A place you *say* → a pin.** If the phone has no GPS fix, the place the reporter mentions ("we're at Warren and Schaefer") goes to OpenStreetMap's Nominatim, with an Overpass lookup for street intersections, and the report lands on the map anyway.
3. **AI Sitrep.** Open reports are clustered into hotspots (deterministic: reports within 700 m, starting from the most urgent), drawn as circles on the map. Gemini writes a 3-5 sentence briefing for the incident commander: what is worst, where it clusters, and what to send first.

Thinking is turned off for speed, and the call has a hard timeout. If `gemini-2.5-flash` is unavailable or rate-limited, FloodLine tries the fallback models in order (`GEMINI_FALLBACK_MODELS`).

### Never lose a report

Saving comes first and the AI enriches after:

- The report (audio, photo, location, text) is written to the database **before** the AI runs. The dashboard immediately shows it as *processing...*.
- If the AI fails (no key, timeout, quota, bad answer), the report is **kept** and marked **needs review**. A typed report is still analyzed by a keyword fallback (English, Arabic and Spanish). A *needs review* report never ranks below MEDIUM, so an unverified report cannot sink. Responders can press **Retry AI** later.
- If the phone loses its connection, the recording stays on the phone with a *Try again* button.
- Reports that were still processing when the server stopped are picked up again on the next start.

### Urgency rules

The AI only *extracts facts*. The ranking is plain, explainable rules, and the chips on every report are exactly the rules that fired. "Vulnerable" means elderly, disabled or children. "Living space" means water in a living area or a report from a home.

| Level | Score | When any of these is true |
|---|---|---|
| **CRITICAL** | 80-100 | someone **trapped** · **medical** emergency · **electrical hazard + standing water** · **vulnerable person + water rising + living space** |
| **HIGH** | 60-79 | **≥ 30 cm of water in a living space** · **sewage** · a **vulnerable** person present · **gas leak** or **structural damage** |
| **MEDIUM** | 35-59 | a **basement** report or water in a living area · a **home or car** with standing water |
| **LOW** | 5-34 | everything else (street flooding, information only) |

Inside a band, the score grows with danger: more rules fired, deeper water, water rising, each vulnerable person, each extra hazard, more people. Adding danger never lowers a score. The queue puts open reports first (then dispatched, then resolved). Within each group it shows reports still being processed on top, then sorts by level, score, and newest first.

---

## API summary

All JSON. Errors look like `{"detail": "..."}`.

| Method & path | What it does |
|---|---|
| `GET /api/health` | `{"ok", "reports", "ai_enabled", "ai_model"}` |
| `GET /api/config` | Public URL, phone report URL, AI status, storm state, map center/zoom |
| `POST /api/config/public-url` | `{"public_url": "https://..."}`: set the address phones use (run.py does this for you) |
| `GET /api/reports` | All reports, in queue order |
| `GET /api/reports/{id}` | One report |
| `POST /api/reports` | Multipart: `audio`?, `photo`?, `text`?, `lat`?, `lng`?, `accuracy_m`?, `address_text`?, `ui_language` (en/ar/es). Needs audio or text. Saves, runs the AI (waits a few seconds for it), returns the report |
| `PATCH /api/reports/{id}` | `{"status": "new" \| "dispatched" \| "resolved"}` |
| `POST /api/reports/{id}/reprocess` | Run the AI again (e.g. after adding a key) |
| `GET /api/reports/{id}/audio`, `/photo` | The original voice note or photo |
| `GET /api/events` | Server-Sent Events: `hello`, `report.created`, `report.updated`, `storm.state`, `config.updated`, `reset` |
| `POST /api/storm/start`, `/api/storm/stop` | Simulated storm: realistic reports arrive every few seconds |
| `POST /api/briefing` | AI sitrep text + hotspot circles |
| `POST /api/admin/reset` | Stop the storm, clear reports and uploads, reload the 25 demo reports |

The dashboard lives at `/dashboard` and the phone page at `/report`.

---

## Configuration

Settings come from environment variables or a `.env` file in the repo root (copy `.env.example`). All are optional.

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | (empty) | Google AI Studio key. Empty = keyword fallback only. `GOOGLE_API_KEY` also works |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model for voice notes and the sitrep |
| `GEMINI_FALLBACK_MODELS` | `gemini-2.5-flash-lite,gemini-flash-latest` | Tried in order when the main model is unavailable or rate-limited |
| `PUBLIC_URL` | (empty) | The https address phones use. `run.py` sets it automatically from the tunnel |
| `FLOODLINE_DATA_DIR` | `data` | SQLite database, uploaded audio/photos, geocoding cache |
| `FLOODLINE_SEED` | `1` | Load the 25 demo reports when the database is empty |
| `FLOODLINE_AI_TIMEOUT` | `15` | Seconds before the AI step gives up (the report is kept) |
| `FLOODLINE_POST_WAIT` | `9` | Seconds the phone waits for the AI before showing "Understanding your report..." |
| `NOMINATIM_URL` | `https://nominatim.openstreetmap.org` | Geocoder |
| `NOMINATIM_USER_AGENT` | `FloodLine/0.1 (...)` | Identifies the app to OpenStreetMap, as their usage policy asks |

Geocoding follows the Nominatim usage policy: at most one request per second, an identifying User-Agent, and every result cached on disk.

---

## Troubleshooting

**Windows on ARM (Snapdragon laptops).** Use the **x64** build of Python 3.12 (`py -V:3.12`, which `setup.ps1` picks). The Gemini SDK depends on `cryptography`, which does not always ship Windows-ARM64 wheels and fails to compile from source. x64 Python runs under Windows' built-in emulation and installs the prebuilt wheels. cloudflared has no Windows-ARM64 build either, so `setup.ps1` downloads the x64 one, which also runs under emulation. Expect the first start to take a few seconds longer.

**The phone says it cannot use the microphone.** Browsers allow the microphone only on `https://` pages (or `localhost`). Use the `trycloudflare.com` link or QR code, not `http://192.168...`. Without https the page offers **Type instead**, which always works. On iPhone, also check *Settings → Safari → Microphone*.

**No tunnel URL / phones cannot connect.** Run `.venv\Scripts\python.exe scripts\check_tunnel.py`: it opens a test tunnel and fetches through it. Some venue networks block Cloudflare tunnels; try a phone hotspot for the laptop. If cloudflared is missing, run `scripts\get_cloudflared.ps1`. You can also paste any https URL that reaches the laptop into the dashboard's QR panel.

**"This site can't be reached" right after start.** A new tunnel name takes about 10-15 s to appear in DNS, and a device that asks too early remembers "not found" for about a minute. `run.py` waits until the name is live before it shows the QR code. If the laptop's own browser still says not found, run `ipconfig /flushdns`. A phone can switch Wi-Fi off and on, or simply wait a minute.

**The dashboard says "polling" instead of "live".** Live updates use Server-Sent Events. Some proxies (including tunnels) buffer them, so the dashboard falls back to refreshing every 3 seconds. Use the dashboard on the laptop (`http://localhost:8000/dashboard`) for instant updates. Phones only need the report page.

**Gemini quota / errors.** The free tier has per-minute and per-day request limits. When a model answers 429 (quota), 404 or 5xx, FloodLine moves to the next model in `GEMINI_FALLBACK_MODELS`. If all fail, the report is kept, marked **needs review**, and still ranked (keyword fallback for typed text). The banner and `/api/health` show whether AI is on and which model answered last. Use **Retry AI** on a report once the quota resets.

**"Port 8000 is already in use".** Another FloodLine (or another server) is running. Close it, or use `.\run.ps1 --port 8010`.

**Windows Firewall prompt on first start.** The server listens on all interfaces so phones on the same Wi-Fi can reach it. Allowing *private networks* is enough; the tunnel works either way.

**Start over with fresh demo data.** Press **Reset demo** on the dashboard, or restart with `.\run.ps1 --reset`.

---

## Project layout

```
backend/app/        FastAPI app: API, SQLite, events, AI (Gemini), geocoding, urgency rules, seed + storm data
backend/tests/      pytest suite (network mocked)
backend/seed_audio/ sample voice notes for the demo reports (generated with edge-tts)
frontend/src/       React app: report/ (phone page), dashboard/ (responder view)
run.py, run.ps1     launcher: build, server, tunnel, Ctrl+C handling
setup.ps1           one-time setup
scripts/            get_cloudflared.ps1, check_tunnel.py
```

Map data © OpenStreetMap contributors. FloodLine is a hackathon prototype. Data in the demo is simulated, and demo locations are block- or intersection-level only.
