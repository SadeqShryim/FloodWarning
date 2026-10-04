"""FloodLine HTTP app: the JSON API, the live event stream, and the built frontend, in one process.

Run: python -m uvicorn app.main:app --app-dir backend --port 8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import signal
import threading
import time
import uuid
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import ai, briefing, config, db, service, storm
from .events import RESYNC, broker
from .models import Briefing, PublicUrlUpdate, StatusUpdate
from .util import utc_now_iso

# Uvicorn configures only its own loggers; give ours (app.*) somewhere to go. No-op if the
# root logger is already set up (e.g. by a test runner or an embedding script).
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

SSE_HEARTBEAT_S = 15.0  # comment line that keeps proxies and the browser from timing the stream out
UI_LANGUAGES = ("en", "ar", "es")

# Explicit types for files we serve: on Windows, mimetypes reads the registry, which can claim
# .js is text/plain, and browsers refuse to run module scripts served that way.
STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".wasm": "application/wasm",
}

# Some browsers label recorded audio with a video/* container type.
AUDIO_MIME_ALIASES = {"video/webm": "audio/webm", "video/mp4": "audio/mp4", "video/ogg": "audio/ogg"}

NOT_BUILT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>FloodLine</title></head>
<body style="font-family: system-ui, sans-serif; background: #0f1419; color: #e6e8eb; padding: 2rem; line-height: 1.5">
<h1>FloodLine</h1>
<p>Frontend not built: run <code>npm run build</code> in <code>frontend/</code>, then reload.</p>
<p>The API is up: <a style="color: #7cb7ff" href="/api/health">/api/health</a></p>
</body></html>
"""


# ---------------------------------------------------------------- background tasks

# Strong references to fire-and-forget tasks: asyncio only keeps weak ones, so an unreferenced
# task can be garbage-collected mid-flight. Each task removes itself when it finishes.
_background: set[asyncio.Task] = set()
_enrichments: dict[int, asyncio.Task] = {}  # report id -> its running AI task (one at a time per report)


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    task.add_done_callback(_log_task_failure)
    return task


def _log_task_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("background task %s failed", task.get_name(), exc_info=task.exception())


def start_enrichment(report_id: int) -> asyncio.Task:
    """Run service.enrich_report in the background, unless it is already running for this report."""
    running = _enrichments.get(report_id)
    if running is not None and not running.done():
        return running
    task = spawn(service.enrich_report(report_id), name=f"enrich-{report_id}")
    _enrichments[report_id] = task

    def forget(done: asyncio.Task) -> None:
        if _enrichments.get(report_id) is done:
            del _enrichments[report_id]

    task.add_done_callback(forget)
    task.add_done_callback(_announce_model_change)
    return task


# The ai_model the dashboards were last told about (their header badge shows it).
_announced_model: str | None = None


def _announce_model_change(_task: asyncio.Future | None = None) -> None:
    """Push the config when the Gemini model that answers changes: the warm-up found the first model
    closed to this key, or a quota error moved reports to a fallback. A live dashboard never re-reads
    /api/config on its own, so without this its badge would keep naming a model that is not in use."""
    global _announced_model
    enabled, model = _ai_status()
    # Only a switch between two models counts: the key is read once at startup, so AI on/off
    # never changes while the server runs.
    if not enabled or _announced_model is None or model == _announced_model:
        return
    _announced_model = model
    broker.publish({"type": "config.updated", "config": app_config(app)})


async def _cancel_tasks(tasks: list[asyncio.Task]) -> None:
    loop = asyncio.get_running_loop()
    mine = [t for t in tasks if not t.done() and t.get_loop() is loop]
    for task in mine:
        task.cancel()
    if mine:
        await asyncio.gather(*mine, return_exceptions=True)


async def cancel_enrichments() -> None:
    await _cancel_tasks(list(_enrichments.values()))
    await service.cancel_late_labels()


# ---------------------------------------------------------------- shutdown

# Set when the server is stopping. Open SSE streams watch it: uvicorn only runs the lifespan
# shutdown after every response has finished, so without this a connected dashboard would hold
# Ctrl+C for the whole graceful-shutdown timeout and end in a cancellation traceback.
_stopping = threading.Event()


def _hook_exit_signals() -> None:
    """Chain onto uvicorn's Ctrl+C / SIGTERM handlers so _stopping is set the moment a stop is requested."""
    if threading.current_thread() is not threading.main_thread():
        return  # signal handlers can only be set from the main thread (embedded servers, tests)
    names = ("SIGINT", "SIGTERM", "SIGBREAK")  # SIGBREAK: Ctrl+Break on Windows
    for sig in [getattr(signal, n) for n in names if hasattr(signal, n)]:
        previous = signal.getsignal(sig)
        if not callable(previous) or getattr(previous, "_floodline_hook", False):
            continue

        def handler(signum: int, frame: Any, previous: Any = previous) -> None:
            _stopping.set()
            previous(signum, frame)

        handler._floodline_hook = True  # type: ignore[attr-defined]
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


# ---------------------------------------------------------------- app state helpers


def _clean_public_url(value: str | None) -> str | None:
    """None/blank -> None; an http(s) URL -> itself without the trailing slash; anything else -> ValueError."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError("public_url must be an http:// or https:// URL")
    return value.rstrip("/")


def _ai_status() -> tuple[bool, str | None]:
    try:
        enabled = bool(ai.ai_enabled())
        return enabled, ai.active_model() if enabled else None
    except Exception:
        log.exception("could not read the AI status")
        return False, None


def app_config(app: FastAPI) -> dict:
    """The AppConfig dict (see models.AppConfig)."""
    public_url = getattr(app.state, "public_url", None)
    controller = getattr(app.state, "storm", None)
    enabled, model = _ai_status()
    return {
        "public_url": public_url,
        "report_url": f"{public_url}/report" if public_url else None,
        "ai_enabled": enabled,
        "ai_model": model,
        "storm_running": bool(controller.running) if controller else False,
        "storm_injected": int(controller.injected) if controller else 0,
        "map_center": list(config.MAP_CENTER),
        "map_zoom": config.MAP_ZOOM,
    }


def _storm_state(app: FastAPI) -> dict:
    controller = app.state.storm
    return {"running": bool(controller.running), "injected": int(controller.injected)}


async def _publish_storm_state(running: bool, injected: int) -> None:
    broker.publish({"type": "storm.state", "running": bool(running), "injected": int(injected)})


def _clear_uploads() -> None:
    upload_dir = Path(config.UPLOAD_DIR)
    if not upload_dir.is_dir():
        return
    for path in upload_dir.iterdir():
        if path.is_file():
            try:
                path.unlink()
            except OSError:  # e.g. Windows: a file still open by a download in progress
                log.warning("could not delete upload %s", path.name)


def _seed() -> int:
    try:
        return service.insert_seed_rows()
    except Exception:
        log.exception("loading the demo reports failed; starting without them")
        return 0


async def _resume_pending() -> None:
    """Reports left "pending" by a previous run (server stopped mid-AI) would spin forever otherwise."""
    for row in db.list_reports():
        if row["ai_status"] != "pending":
            continue
        if row["is_simulated"]:
            # A storm report whose simulated finish never ran: nothing real to process.
            await service.apply_update(row["id"], {"ai_status": "failed", "ai_error": "interrupted by restart"})
        else:
            log.info("report %s was still pending at startup; re-running the AI step", row["id"])
            start_enrichment(row["id"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    _stopping.clear()
    _hook_exit_signals()
    db.init_db()
    if config.SEED_ON_START and db.count_reports() == 0:
        log.info("loaded %d demo reports", _seed())
    try:
        app.state.public_url = _clean_public_url(config.PUBLIC_URL)
    except ValueError:
        log.warning("ignoring PUBLIC_URL %r: not an http(s) URL", config.PUBLIC_URL)
        app.state.public_url = None
    app.state.storm = storm.StormController(service.add_report, service.apply_update, _publish_storm_state)
    await _resume_pending()
    global _announced_model
    enabled, model = _ai_status()
    _announced_model = model if enabled else None
    log.info("FloodLine ready: %d reports, AI %s", db.count_reports(), model if enabled else "off (keyword fallback)")
    # One tiny Gemini call in the background, so the first judge's voice note does not pay for the
    # SDK import, the TLS handshake and the models this key cannot use. Returns at once; no-op
    # without a key, under pytest, or with FLOODLINE_AI_WARMUP=0.
    warm_up = ai.schedule_warm_up()
    if warm_up is not None:
        warm_up.add_done_callback(_announce_model_change)
    try:
        yield
    finally:
        _stopping.set()
        try:
            await app.state.storm.stop()
        except Exception:
            log.exception("stopping storm mode failed")
        await ai.stop_warm_up()
        await _cancel_tasks(list(_background))
        await service.cancel_late_labels()
        db.close()


app = FastAPI(title="FloodLine", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------- health and config


@app.get("/api/health")
async def health() -> dict:
    enabled, model = _ai_status()
    return {"ok": True, "reports": db.count_reports(), "ai_enabled": enabled, "ai_model": model}


@app.get("/api/config")
async def get_config(request: Request) -> dict:
    return app_config(request.app)


@app.post("/api/config/public-url")
async def set_public_url(body: PublicUrlUpdate, request: Request) -> dict:
    try:
        request.app.state.public_url = _clean_public_url(body.public_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    current = app_config(request.app)
    broker.publish({"type": "config.updated", "config": current})
    return current


# ---------------------------------------------------------------- reports


def _parse_float(value: str | None, low: float, high: float) -> float | None:
    """Lenient number parsing for form fields: garbage or out-of-range values are ignored, not errors."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not low <= number <= high:
        return None
    return number


def _clean_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:limit] if value else None


async def _read_limited(upload: UploadFile | None, limit: int, what: str) -> bytes | None:
    """The upload's bytes (None if absent or empty); 413 when it is over the limit."""
    if upload is None:
        return None
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"{what} is too large (limit {limit // (1024 * 1024)} MB)")
    return data or None


# Leading bytes of the formats phones and browsers record or photograph in.
_MAGIC: list[tuple[int, bytes, str]] = [
    (0, b"RIFF", ".wav"),  # checked together with "WAVE" below
    (0, b"OggS", ".ogg"),
    (0, b"\x1a\x45\xdf\xa3", ".webm"),  # Matroska / WebM
    (4, b"ftyp", ".m4a"),  # MP4 / M4A family
    (0, b"ID3", ".mp3"),
    (0, b"fLaC", ".flac"),
    (0, b"\xff\xd8\xff", ".jpg"),
    (0, b"\x89PNG", ".png"),
]


def _sniff(data: bytes, kind: str) -> str | None:
    """Extension guessed from the file's first bytes, or None."""
    for offset, magic, suffix in _MAGIC:
        if data[offset:offset + len(magic)] == magic:
            if suffix == ".wav" and data[8:12] != b"WAVE":
                continue
            return suffix if service.EXT_TO_MIME[suffix].startswith(f"{kind}/") else None
    if kind == "audio" and len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0:
        return ".mp3"  # bare MPEG audio frame
    return None


def _file_type(upload: UploadFile, kind: str, data: bytes = b"") -> tuple[str, str]:
    """(extension, MIME type) for an upload: the filename's extension, else the content type, else
    the file's own leading bytes. The MIME type is stored in its canonical form so playback works."""
    mime = (upload.content_type or "").split(";")[0].strip().lower()
    if kind == "audio":
        mime = AUDIO_MIME_ALIASES.get(mime, mime)
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in service.EXT_TO_MIME:
        suffix = service.MIME_TO_EXT.get(mime) or _sniff(data, kind) or ".bin"
    if not mime or mime == "application/octet-stream" or not mime.startswith(f"{kind}/"):
        mime = service.EXT_TO_MIME.get(suffix, mime or "application/octet-stream")
    if mime in service.MIME_TO_EXT:  # aliases like audio/x-wav, audio/x-m4a, image/jpg
        mime = service.EXT_TO_MIME[service.MIME_TO_EXT[mime]]
    return suffix, mime


def _save_upload(data: bytes, suffix: str) -> str:
    """Write to UPLOAD_DIR under a random name; returns the name (relative path stored in the row)."""
    upload_dir = Path(config.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{suffix}"
    (upload_dir / name).write_bytes(data)
    return name


def _get_or_404(report_id: int) -> dict:
    row = db.get_report(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return row


@app.get("/api/reports")
async def list_reports() -> list[dict]:
    return [service.to_api(row) for row in service.queue_order(db.list_reports())]


@app.get("/api/reports/{report_id}")
async def get_report(report_id: int) -> dict:
    return service.to_api(_get_or_404(report_id))


@app.post("/api/reports")
async def create_report(
    audio: UploadFile | None = File(None),
    photo: UploadFile | None = File(None),
    text: str | None = Form(None),
    lat: str | None = Form(None),
    lng: str | None = Form(None),
    accuracy_m: str | None = Form(None),
    address_text: str | None = Form(None),
    ui_language: str | None = Form(None),
) -> dict:
    audio_bytes = await _read_limited(audio, config.MAX_AUDIO_BYTES, "Voice note")
    photo_bytes = await _read_limited(photo, config.MAX_PHOTO_BYTES, "Photo")
    text = _clean_text(text, 5000)
    if audio_bytes is None and text is None:
        raise HTTPException(status_code=422, detail="Send a voice note or some text.")

    language = (ui_language or "").strip().lower()
    language = language if language in UI_LANGUAGES else "en"
    lat_value = _parse_float(lat, -90, 90)
    lng_value = _parse_float(lng, -180, 180)
    if lat_value is None or lng_value is None:  # half a coordinate is no coordinate
        lat_value = lng_value = None
    accuracy = _parse_float(accuracy_m, 0, 1_000_000) if lat_value is not None else None
    address = _clean_text(address_text, 200)

    row: dict[str, Any] = {
        "lat": lat_value,
        "lng": lng_value,
        "accuracy_m": accuracy,
        "location_source": "gps" if lat_value is not None else ("typed" if address else None),
        "address_text": address,
        "ui_language": language,
        "input_type": "voice" if audio_bytes else "text",
        "transcript_original": text,  # the typed words; the AI step replaces this for voice notes
        "status": "new",
        "ai_status": "pending",
        "is_simulated": False,
    }
    if audio_bytes and audio is not None:
        suffix, mime = _file_type(audio, "audio", audio_bytes)
        row["audio_path"], row["audio_mime"] = _save_upload(audio_bytes, suffix), mime
    if photo_bytes and photo is not None:
        suffix, mime = _file_type(photo, "image", photo_bytes)
        row["photo_path"], row["photo_mime"] = _save_upload(photo_bytes, suffix), mime

    stored = await service.add_report(row)
    report_id = stored["id"]
    task = start_enrichment(report_id)
    # asyncio.wait neither cancels the AI step when we stop waiting nor raises when that step is
    # cancelled (a demo reset does that): either way the phone still gets a normal answer.
    done, _ = await asyncio.wait({task}, timeout=config.POST_WAIT_S)
    if not done:
        log.info("report %s: answering while the AI step is still running", report_id)
    elif task.cancelled():
        log.info("report %s: the AI step was cancelled (demo reset?); answering with the saved report", report_id)
    return service.to_api(db.get_report(report_id) or stored)


@app.patch("/api/reports/{report_id}")
async def update_status(report_id: int, body: StatusUpdate) -> dict:
    _get_or_404(report_id)
    row = await service.apply_update(report_id, {"status": body.status})
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return service.to_api(row)


@app.post("/api/reports/{report_id}/reprocess")
async def reprocess(report_id: int) -> dict:
    current = _get_or_404(report_id)
    running = _enrichments.get(report_id)
    if running is not None and not running.done():
        return service.to_api(current)  # already being processed: do not start a second AI call
    row = await service.apply_update(report_id, {"ai_status": "pending", "ai_error": None})
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    start_enrichment(report_id)
    return service.to_api(row)


def _serve_upload(report_id: int, kind: str) -> FileResponse:
    row = _get_or_404(report_id)
    path = service.upload_path(row.get(f"{kind}_path"))
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail=f"No {kind} for this report")
    mime = row.get(f"{kind}_mime") or service.mime_for_path(path)
    # FileResponse answers Range requests (206), which Safari needs before it will play audio.
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/reports/{report_id}/audio")
async def get_audio(report_id: int) -> FileResponse:
    return _serve_upload(report_id, "audio")


@app.get("/api/reports/{report_id}/photo")
async def get_photo(report_id: int) -> FileResponse:
    return _serve_upload(report_id, "photo")


# ---------------------------------------------------------------- live events (SSE)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def stream():
        queue = broker.subscribe()
        try:
            yield _sse({"type": "hello", "server_time": utc_now_iso()})
            last_sent = time.monotonic()
            # Wake up at least twice a second so a server shutdown ends the stream promptly.
            poll_s = min(0.5, SSE_HEARTBEAT_S)
            while not _stopping.is_set():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=poll_s)
                except asyncio.TimeoutError:
                    if time.monotonic() - last_sent >= SSE_HEARTBEAT_S:
                        yield ": ping\n\n"
                        last_sent = time.monotonic()
                    continue
                if event is RESYNC:
                    # This dashboard fell too far behind (see events.py). Ending the stream makes
                    # EventSource reconnect (after 1 s), and the new "hello" reloads everything.
                    yield "retry: 1000\n\n"
                    break
                yield _sse(event)
                last_sent = time.monotonic()
        finally:
            # Starlette cancels this generator as soon as the client disconnects.
            broker.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- storm, briefing, admin


class StormStart(BaseModel):
    max_reports: int | None = Field(default=None, ge=1, le=500)
    min_interval_s: float | None = Field(default=None, ge=0.1, le=120)
    max_interval_s: float | None = Field(default=None, ge=0.1, le=120)


@app.post("/api/storm/start")
async def storm_start(request: Request, body: StormStart | None = Body(default=None)) -> dict:
    options = body or StormStart()
    max_reports = options.max_reports if options.max_reports is not None else 24
    min_interval = options.min_interval_s if options.min_interval_s is not None else 2.0
    max_interval = options.max_interval_s if options.max_interval_s is not None else 4.5
    max_interval = max(max_interval, min_interval)
    request.app.state.storm.start(max_reports=max_reports, min_interval_s=min_interval, max_interval_s=max_interval)
    return _storm_state(request.app)


@app.post("/api/storm/stop")
async def storm_stop(request: Request) -> dict:
    await request.app.state.storm.stop()
    return _storm_state(request.app)


@app.post("/api/briefing")
async def make_briefing() -> dict:
    reports = db.list_reports()
    try:
        result = await briefing.build_briefing(reports)
        return Briefing.model_validate(result).model_dump()
    except Exception:
        log.exception("briefing failed; answering with a minimal one")
        open_count = sum(1 for r in reports if r["status"] == "new")
        return {
            "text": f"{open_count} open reports. The automatic briefing is unavailable right now.",
            "hotspots": [],
            "generated_at": utc_now_iso(),
            "engine": "rules",
        }


@app.post("/api/admin/reset")
async def admin_reset(request: Request) -> dict:
    try:
        await request.app.state.storm.stop()
    except Exception:
        log.exception("stopping storm mode during reset failed")
    await cancel_enrichments()
    db.clear_reports()
    _clear_uploads()  # the geocode cache lives in DATA_DIR, not here, so it survives
    seeded = _seed()
    log.info("demo reset: %d demo reports loaded", seeded)
    broker.publish({"type": "reset"})
    return {"ok": True, "count": db.count_reports()}


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def api_not_found(rest: str) -> None:
    # Keeps unknown API paths from falling through to the SPA (a JSON client would get HTML).
    raise HTTPException(status_code=404, detail="Not found")


# ---------------------------------------------------------------- frontend


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/dashboard", status_code=307)


@app.get("/{full_path:path}", include_in_schema=False)
async def frontend(full_path: str):
    if full_path == "api":
        raise HTTPException(status_code=404, detail="Not found")
    dist = Path(config.FRONTEND_DIST).resolve()
    index = dist / "index.html"
    if not index.is_file():
        return HTMLResponse(NOT_BUILT_HTML)
    if full_path:
        candidate = (dist / full_path).resolve()
        if dist in candidate.parents and candidate.is_file():
            # Vite puts content hashes in asset names, so those can be cached forever.
            cache = "public, max-age=31536000, immutable" if full_path.startswith("assets/") else "no-cache"
            media_type = STATIC_MIME.get(candidate.suffix.lower()) or service.mime_for_path(candidate)
            return FileResponse(candidate, media_type=media_type, headers={"Cache-Control": cache})
    # Client-side routes (/dashboard, /report, ...) all load the single-page app.
    return FileResponse(index, media_type=STATIC_MIME[".html"], headers={"Cache-Control": "no-cache"})
