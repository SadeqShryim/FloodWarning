"""The only code that writes reports, so urgency scores and live events always stay in step.

    add_report      insert + urgency + "report.created"
    apply_update    update + urgency recomputed on the merged row + "report.updated"
    enrich_report   the AI pipeline (contract section 8): save first, enrich after, never lose a report
    insert_seed_rows  load the demo reports (no events: nobody is listening at startup)

Other modules are called through their module attribute (ai.extract_report, geocode.geocode, ...)
rather than imported names, so tests can monkeypatch them.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import time
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ai, config, db, fallback_rules, geocode, seed_data, urgency
from .events import broker
from .models import Report
from .util import parse_iso

log = logging.getLogger(__name__)

# Row columns the AI step (or the keyword fallback) fills in.
EXTRACTION_FIELDS = (
    "language",
    "transcript_original",
    "transcript_english",
    "ai_summary",
    "confirmation_message",
    "water_depth_cm",
    "location_type",
    "location_hint",
    "water_in_living_space",
    "water_rising",
    "hazards",
    "people_at_risk",
    "needs",
)
REPORT_FIELDS = tuple(Report.model_fields)

# Extension <-> MIME type for the uploads we expect. mimetypes on Windows reads the registry
# and gets some of these wrong (or knows nothing), so the common ones are pinned here.
EXT_TO_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}
MIME_TO_EXT = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

_PENDING_URGENCY = {"urgency_score": None, "urgency_level": None, "urgency_reasons": []}


def mime_for_path(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    return EXT_TO_MIME.get(suffix) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


# ---------------------------------------------------------------- shapes


def to_api(row: Mapping[str, Any]) -> dict:
    """Row dict -> models.Report shape: file paths swapped for URLs, nothing else exposed."""
    complete = db.normalize_row(row)  # fills defaults for any key a caller left out
    out = {name: complete[name] for name in REPORT_FIELDS if name in complete}
    report_id = complete["id"]
    out["audio_url"] = f"/api/reports/{report_id}/audio" if complete.get("audio_path") else None
    out["photo_url"] = f"/api/reports/{report_id}/photo" if complete.get("photo_path") else None
    return out


_STATUS_RANK = {"new": 0, "dispatched": 1, "resolved": 2}
_LEVEL_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _timestamp(value: Any) -> float:
    try:
        return parse_iso(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def queue_order(rows: Iterable[Mapping[str, Any]]) -> list:
    """Same order as compareReports in frontend/src/types.ts: open first, processing on top,
    then CRITICAL..LOW, higher score, newest."""
    return sorted(
        rows,
        key=lambda r: (
            _STATUS_RANK.get(r.get("status") or "new", 0),
            0 if r.get("ai_status") == "pending" else 1,
            -_LEVEL_RANK.get(r.get("urgency_level") or "", 0),
            -(r.get("urgency_score") or 0),
            -_timestamp(r.get("created_at")),
        ),
    )


# ---------------------------------------------------------------- urgency + writes


def _score(row: Mapping[str, Any]) -> dict:
    """urgency.score_report, but a bug there must never block saving a report."""
    if row.get("ai_status") == "pending":
        return dict(_PENDING_URGENCY)
    try:
        result = urgency.score_report(row)
        return {
            "urgency_score": result.get("urgency_score"),
            "urgency_level": result.get("urgency_level"),
            "urgency_reasons": list(result.get("urgency_reasons") or []),
        }
    except Exception:
        log.exception("urgency scoring failed for report %s; ranking it MEDIUM / needs review", row.get("id"))
        return {"urgency_score": 50, "urgency_level": "MEDIUM", "urgency_reasons": ["needs review"]}


async def add_report(row: dict) -> dict:
    """Insert a row dict, publish "report.created", return the stored row (with its id)."""
    values = db.normalize_row(row)
    values.update(_score(values))
    stored = db.insert_report(values)
    broker.publish({"type": "report.created", "report": to_api(stored)})
    return stored


async def apply_update(report_id: int, fields: dict) -> dict | None:
    """Update a report, recompute urgency on the merged row, publish "report.updated".

    Returns the stored row, or None when the report no longer exists (e.g. a demo reset raced
    with a storm finish or an AI result).
    """
    current = db.get_report(report_id)
    if current is None:
        log.info("update for report %s skipped: it no longer exists", report_id)
        return None
    merged = db.normalize_row({**current, **fields, "id": report_id})
    changes = {**fields, **_score(merged)}
    stored = db.update_report(report_id, changes)
    if stored is None:
        return None
    broker.publish({"type": "report.updated", "report": to_api(stored)})
    return stored


# ---------------------------------------------------------------- AI pipeline


def _extraction_dict(extraction: Any) -> dict:
    if hasattr(extraction, "model_dump"):
        data = extraction.model_dump()
    elif isinstance(extraction, Mapping):
        data = dict(extraction)
    else:
        raise TypeError(f"unexpected extraction type {type(extraction).__name__}")
    out = {name: data[name] for name in EXTRACTION_FIELDS if name in data}
    for name in ("transcript_original", "transcript_english", "ai_summary", "confirmation_message", "location_hint"):
        if isinstance(out.get(name), str):
            out[name] = out[name].strip() or None
    return out


def _failure_reason(exc: BaseException | None, ai_on: bool) -> str:
    if not ai_on:
        return "AI not configured"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, ai.AIError):
        message = " ".join(str(exc).split())
        return message[:160] or "AI error"
    if exc is None:
        return "AI error"
    return f"AI error: {type(exc).__name__}"[:160]


def upload_path(relative: str | None) -> Path | None:
    """Absolute path of a stored upload, refusing anything that escapes UPLOAD_DIR."""
    if not relative:
        return None
    base = Path(config.UPLOAD_DIR).resolve()
    path = (base / relative).resolve()
    if base not in path.parents:
        return None
    return path


async def _read_upload(relative: str | None) -> bytes | None:
    path = upload_path(relative)
    if path is None or not path.is_file():
        return None
    return await asyncio.to_thread(path.read_bytes)


async def _call_ai(**kwargs: Any) -> tuple[Any, str, int]:
    # ai.extract_report enforces config.AI_TIMEOUT_S itself; this outer limit is only a safety net.
    return await asyncio.wait_for(ai.extract_report(**kwargs), timeout=config.AI_TIMEOUT_S + 5)


async def _location_work(row: Mapping[str, Any]) -> dict:
    """Location fixes that do not need the AI. Returns fields to merge (possibly empty)."""
    has_coords = row.get("lat") is not None and row.get("lng") is not None
    try:
        if has_coords and not row.get("address_text"):
            label = await geocode.reverse_geocode(row["lat"], row["lng"])
            return {"address_text": label} if label else {}
        if not has_coords and row.get("address_text"):
            hit = await geocode.geocode(row["address_text"])
            if hit and hit.get("lat") is not None and hit.get("lng") is not None:
                return {"lat": float(hit["lat"]), "lng": float(hit["lng"]), "location_source": "typed"}
    except Exception:
        log.exception("location lookup failed for report %s", row.get("id"))
    return {}


async def _place_from_hint(hint: str, row: Mapping[str, Any], fields: dict) -> None:
    """A place the reporter mentioned -> coordinates, when nothing better is known."""
    try:
        hit = await geocode.geocode(hint)
    except Exception:
        log.exception("geocoding the spoken place %r failed", hint)
        return
    if not hit or hit.get("lat") is None or hit.get("lng") is None:
        return
    fields.update({"lat": float(hit["lat"]), "lng": float(hit["lng"]), "location_source": "spoken"})
    if not row.get("address_text") and not fields.get("address_text") and hit.get("label"):
        fields["address_text"] = hit["label"]


async def enrich_report(report_id: int) -> dict:
    """Run the AI pipeline for one saved report and store the result. Never raises (except cancellation).

    Returns the final row dict, or {} when the report vanished.
    """
    started = time.monotonic()
    try:
        return await _enrich(report_id, started)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # last line of defence: the report stays saved and gets flagged
        log.exception("enrichment crashed for report %s", report_id)
        try:
            row = await apply_update(report_id, {"ai_status": "failed", "ai_error": f"internal error: {type(exc).__name__}"})
            return row or {}
        except Exception:
            log.exception("could not even mark report %s as failed", report_id)
            return {}


async def _enrich(report_id: int, started: float) -> dict:
    row = db.get_report(report_id)
    if row is None:
        log.info("report %s: enrichment skipped, report no longer exists", report_id)
        return {}
    ui_language = row.get("ui_language") or "en"
    audio = await _read_upload(row.get("audio_path"))
    photo = await _read_upload(row.get("photo_path"))
    # Typed words: the whole report for text reports. For a voice note, transcript_original only
    # holds typed words until a model has written its own transcript there.
    text = row.get("transcript_original") if (row.get("input_type") == "text" or row.get("ai_engine") in (None, "rules")) else None
    text = (text or "").strip() or None
    if row.get("input_type") == "voice" and audio is None:
        log.warning("report %s: voice note file is missing", report_id)

    try:
        ai_on = bool(ai.ai_enabled())
    except Exception:
        log.exception("ai.ai_enabled() failed; treating AI as off")
        ai_on = False

    async def no_ai() -> None:
        return None

    ai_call = (
        _call_ai(audio=audio, audio_mime=row.get("audio_mime"), text=text, photo=photo,
                 photo_mime=row.get("photo_mime"), ui_language=ui_language)
        if ai_on and (audio or text or photo)
        else no_ai()
    )
    ai_result, location_fields = await asyncio.gather(ai_call, _location_work(row), return_exceptions=True)
    if isinstance(location_fields, BaseException):
        if isinstance(location_fields, asyncio.CancelledError):
            raise location_fields
        location_fields = {}
    fields: dict[str, Any] = dict(location_fields)

    error: BaseException | None = ai_result if isinstance(ai_result, BaseException) else None
    if isinstance(error, asyncio.CancelledError):
        raise error
    extraction: dict | None = None
    engine: str | None = None
    latency_ms: int | None = None
    if ai_on and ai_result is not None and error is None:
        try:
            raw, engine, latency_ms = ai_result
            extraction = _extraction_dict(raw)
            if not extraction.get("transcript_original"):
                extraction, error = None, ai.AIError("empty transcript")
        except Exception as exc:  # malformed result from the AI module
            extraction, error = None, exc
    elif ai_on and error is None:
        error = ai.AIError("nothing to process")

    if extraction is not None:
        fields.update(extraction)
        fields.update({"ai_status": "done", "ai_engine": engine, "ai_latency_ms": _int_or_none(latency_ms), "ai_error": None})
        reason = None
    else:
        reason = _failure_reason(error, ai_on)
        fields.update(_fallback_fields(text, ui_language, row))
        fields.update({"ai_status": "failed", "ai_error": reason})

    # A place the reporter mentioned is the last resort for a pin (no GPS, no typed address found).
    has_coords = (row.get("lat") is not None and row.get("lng") is not None) or "lat" in fields
    hint = fields.get("location_hint")
    if not has_coords and hint:
        await _place_from_hint(hint, row, fields)

    final = await apply_update(report_id, fields)
    total_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "report %s enriched: ai_status=%s engine=%s error=%s ai_latency_ms=%s total_ms=%d location=%s",
        report_id,
        fields.get("ai_status"),
        fields.get("ai_engine"),
        reason,
        fields.get("ai_latency_ms"),
        total_ms,
        (final or {}).get("location_source"),
    )
    return final or {}


def _fallback_fields(text: str | None, ui_language: str, row: Mapping[str, Any]) -> dict:
    """What we can still say about a report the AI did not process."""
    try:
        if text:
            fields = _extraction_dict(fallback_rules.extract_from_text(text, ui_language))
            fields["ai_engine"] = "rules"
            return fields
        if row.get("input_type") == "voice":
            failed = fallback_rules.failed_audio_fields(ui_language) or {}
            return {name: failed[name] for name in ("ai_summary", "confirmation_message") if name in failed}
    except Exception:
        log.exception("keyword fallback failed for report %s", row.get("id"))
    return {}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- demo data


def insert_seed_rows() -> int:
    """Insert the demo reports (no events published). Returns how many were inserted."""
    rows = seed_data.seed_rows(datetime.now(timezone.utc))
    upload_dir = Path(config.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    inserted = 0
    for source in rows:
        row = dict(source)
        audio_name = row.pop("seed_audio", None)
        if audio_name:
            audio_src = Path(config.SEED_AUDIO_DIR) / str(audio_name)
            if audio_src.is_file():
                stored_name = f"{uuid.uuid4().hex}{audio_src.suffix.lower()}"
                shutil.copyfile(audio_src, upload_dir / stored_name)
                row["audio_path"] = stored_name
                row["audio_mime"] = mime_for_path(audio_src)
            else:
                log.warning("seed audio %s not found in %s; seeding without it", audio_name, config.SEED_AUDIO_DIR)
        values = db.normalize_row(row)
        values.update(_score(values))
        db.insert_report(values)
        inserted += 1
    return inserted
