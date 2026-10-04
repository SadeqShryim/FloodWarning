"""Live check of the AI and geocoding modules against the real services (budgeted).

    cd backend
    ..\\.venv\\Scripts\\python.exe scripts\\live_ai_check.py            # Gemini (if a key is set) + geocoding
    ..\\.venv\\Scripts\\python.exe scripts\\live_ai_check.py --no-geo   # skip Nominatim/Overpass
    ..\\.venv\\Scripts\\python.exe scripts\\live_ai_check.py --wav      # also send the 16 kHz WAV fixture

Gemini: one extract_report per mp3 fixture plus one build_briefing (4 calls; --wav adds one).
Without GEMINI_API_KEY it says so and shows what the keyword fallback makes of the same texts.
Geocoding: at most 5 HTTP requests to Nominatim/Overpass, spaced 1 per second, with a throwaway cache.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `app` importable when run directly

from app import ai, briefing, config, fallback_rules, geocode, urgency  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Arabic on a Windows console

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
GEO_BUDGET = 5
GEO_QUERIES = [("forward", "Warren and Schaefer"), ("forward", "Dix and Vernor"), ("reverse", (42.3437, -83.1663))]

# Places for the briefing sample (real Dearborn intersections, approximate).
SAMPLE_PLACES = [
    ("Warren Ave & Schaefer Rd", 42.34562, -83.17286),
    ("Warren Ave & Miller Rd", 42.34570, -83.16310),
    ("Dix Ave & Vernor Hwy", 42.30330, -83.15560),
]


def _row_from(extraction, *, rid: int, place: tuple, engine: str, status: str = "new", ai_status: str = "done") -> dict:
    row = extraction.model_dump()
    row.update({
        "id": rid, "status": status, "ai_status": ai_status, "ai_engine": engine,
        "address_text": place[0], "lat": place[1], "lng": place[2], "input_type": "voice",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    row.update(urgency.score_report(row))
    return row


def _print_extraction(name: str, extraction, engine: str, latency_ms: int | None, expected: dict, row: dict) -> None:
    print(f"\n=== {name}  engine={engine}  latency={latency_ms if latency_ms is not None else '-'} ms")
    print(f"language: {extraction.language} (expected {expected['expected_language']})")
    print(f"original: {extraction.transcript_original}")
    print(f"english:  {extraction.transcript_english}")
    print(f"summary:  {extraction.ai_summary}")
    print(f"confirm:  {extraction.confirmation_message}")
    fields = extraction.model_dump(exclude={"transcript_original", "transcript_english", "ai_summary",
                                            "confirmation_message", "language"})
    print("fields:   " + json.dumps(fields, ensure_ascii=False))
    print(f"urgency:  {row['urgency_level']} {row['urgency_score']} {row['urgency_reasons']}"
          f"  (expected {expected['expected_level']})")


async def check_gemini(include_wav: bool) -> list[dict]:
    manifest = json.loads((FIXTURES / "voice_notes.json").read_text(encoding="utf-8"))
    notes = list(manifest["notes"])
    if include_wav:
        notes.append({**notes[1], "file": manifest["wav"]["file"]})
    rows = []
    if not ai.ai_enabled():
        print("GEMINI_API_KEY is not configured: skipping live Gemini calls (0 used).")
        print("Showing the keyword fallback on the fixture texts instead.")
    for i, note in enumerate(notes):
        place = SAMPLE_PLACES[i % len(SAMPLE_PLACES)]
        if ai.ai_enabled():
            audio = (FIXTURES / note["file"]).read_bytes()
            mime = "audio/wav" if note["file"].endswith(".wav") else "audio/mpeg"
            try:
                extraction, engine, latency = await ai.extract_report(
                    audio=audio, audio_mime=mime, text=None, photo=None, photo_mime=None,
                    ui_language=note["expected_language"],
                )
            except ai.AIError as exc:
                print(f"\n=== {note['file']}: AIError({exc})")
                continue
        else:
            started = time.perf_counter()
            extraction = fallback_rules.extract_from_text(note["text"], note["expected_language"])
            engine, latency = "rules", int((time.perf_counter() - started) * 1000)
        row = _row_from(extraction, rid=i + 1, place=place, engine=engine)
        rows.append(row)
        _print_extraction(note["file"], extraction, engine, latency, note, row)
    print(f"\nactive model: {ai.active_model()}")
    return rows


async def check_briefing(rows: list[dict]) -> None:
    sample = list(rows)
    # Two extra open reports near Warren & Schaefer so a cluster forms.
    extra = fallback_rules.extract_from_text("Water in the basement, about 30 cm, sewage coming up from the drain")
    sample.append(_row_from(extra, rid=90, place=("Warren Ave & Schaefer Rd", 42.34600, -83.17200), engine="rules"))
    extra = fallback_rules.extract_from_text("The street is flooded at Schaefer, cars stalled")
    sample.append(_row_from(extra, rid=91, place=("Schaefer Rd near Warren Ave", 42.34400, -83.17300), engine="rules"))
    started = time.perf_counter()
    out = await briefing.build_briefing(sample)
    print(f"\n=== briefing  engine={out['engine']}  latency={int((time.perf_counter() - started) * 1000)} ms")
    print(out["text"])
    for h in out["hotspots"]:
        print(f"  hotspot {h['label']}: {h['level']} r={h['radius_m']} m ids={h['report_ids']}")


async def check_geocoding() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="floodline-geo-"))
    config.GEOCODE_CACHE_PATH = tmp / "geocode_cache.json"  # throwaway cache so requests really happen
    geocode.clear_memory_cache()
    print(f"\n=== geocoding (budget {GEO_BUDGET} HTTP requests, 1/s)")
    for kind, query in GEO_QUERIES:
        if geocode.request_count >= GEO_BUDGET:
            print(f"budget reached, skipping {query}")
            break
        before = geocode.request_count
        started = time.perf_counter()
        if kind == "forward":
            result = await geocode.geocode(query)
        else:
            result = await geocode.reverse_geocode(*query)
        ms = int((time.perf_counter() - started) * 1000)
        print(f"{kind} {query!r}: {result}  ({geocode.request_count - before} requests, {ms} ms)")
    print(f"total geocoding requests: {geocode.request_count}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-geo", action="store_true", help="skip the live Nominatim/Overpass queries")
    parser.add_argument("--no-gemini", action="store_true", help="skip Gemini even if a key is set")
    parser.add_argument("--wav", action="store_true", help="also send the WAV fixture (one more Gemini call)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.no_gemini:
        config.GEMINI_API_KEY = None
    print(f"Gemini model chain: {ai.model_chain()}  (enabled={ai.ai_enabled()})")
    rows = await check_gemini(args.wav)
    await check_briefing(rows)
    if not args.no_geo:
        await check_geocoding()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
