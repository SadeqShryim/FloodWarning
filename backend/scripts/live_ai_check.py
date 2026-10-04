"""One-command live check of the Gemini step (and optionally geocoding), for when the API key arrives.

    .venv\\Scripts\\python.exe backend\\scripts\\live_ai_check.py                 # 4 calls: 3 voice notes + sitrep
    .venv\\Scripts\\python.exe backend\\scripts\\live_ai_check.py --max-calls 1   # just the Arabic grandmother
    .venv\\Scripts\\python.exe backend\\scripts\\live_ai_check.py --model gemini-flash-latest
    .venv\\Scripts\\python.exe backend\\scripts\\live_ai_check.py --geo           # + spoken places -> map pins

Every call goes through the same code the server uses (ai.extract_report / briefing.build_briefing),
with the configured model chain, or only --model. For each call it prints which model answered,
the latency, the urgency level and reasons, the English translation, whether the confirmation came
back in the reporter's language, and PASS/FAIL against what the fixture expects (the Arabic
grandmother in the basement must come out CRITICAL).

Budget: at most --max-calls logical Gemini calls (default 4). A call can need more than one HTTP
request when a model answers 404/403/429 and the next one is tried; those are counted and printed
too (refusals do not use quota). Without GEMINI_API_KEY nothing is sent. --geo adds at most 6
Nominatim/Overpass requests, spaced 1 per second, with a throwaway cache.
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
GEO_BUDGET = 6

# Real Dearborn intersections (from OSM) so the sitrep has something to cluster.
SAMPLE_PLACES = [
    ("Warren Ave & Schaefer Rd", 42.343985, -83.176874),
    ("Warren Ave & Miller Rd", 42.34430, -83.16290),
    ("Dix Ave & Vernor Hwy", 42.30418, -83.14509),
]


class CountingClient:
    """Wraps the SDK client and counts the HTTP calls generate_content makes."""

    def __init__(self, client):
        self._client = client
        self.requests: list[str] = []
        outer = self

        class _Models:
            async def generate_content(self, *, model, contents, config):  # noqa: A002
                outer.requests.append(model)
                return await client.aio.models.generate_content(model=model, contents=contents, config=config)

        class _Aio:
            models = _Models()

        self.aio = _Aio()


def _row_from(extraction, *, rid: int, place: tuple, engine: str) -> dict:
    row = extraction.model_dump()
    row.update({
        "id": rid, "status": "new", "ai_status": "done", "ai_engine": engine,
        "address_text": place[0], "lat": place[1], "lng": place[2], "input_type": "voice",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    row.update(urgency.score_report(row))
    return row


def _get(extraction, dotted: str):
    value = extraction
    for part in dotted.split("."):
        value = getattr(value, part)
    return value


def _check(extraction, note: dict, row: dict) -> list[str]:
    """PASS/FAIL lines against the fixture's expectations."""
    lines = []
    for key, want in note["expected"].items():
        if key == "location_hint_contains":
            got = extraction.location_hint or ""
            ok = all(w.lower() in got.lower() for w in want)
        elif key == "water_depth_cm":
            got = extraction.water_depth_cm
            ok = got is not None and abs(got - want) <= max(10, want * 0.25)
        else:
            got = _get(extraction, key)
            ok = got == want
        lines.append(f"  {'PASS' if ok else 'FAIL'}  {key}: got {got!r}, expected {want!r}")
    ok = row["urgency_level"] == note["expected_level"]
    lines.append(f"  {'PASS' if ok else 'FAIL'}  urgency: got {row['urgency_level']}, expected {note['expected_level']}")
    return lines


def _confirmation_language_ok(extraction, expected: str) -> tuple[bool, str]:
    got = fallback_rules.detect_language(extraction.confirmation_message or "", "en")
    return got == expected, got


async def check_voice_notes(max_calls: int, include_wav: bool, counter: CountingClient | None) -> tuple[list[dict], int, int]:
    manifest = json.loads((FIXTURES / "voice_notes.json").read_text(encoding="utf-8"))
    notes = list(manifest["notes"])
    if include_wav:
        notes.insert(0, {**notes[1], "file": manifest["wav"]["file"]})
    rows, calls, failures = [], 0, 0
    for i, note in enumerate(notes):
        if calls >= max_calls:
            print(f"\n(skipping {note['file']}: --max-calls {max_calls} reached)")
            continue
        audio = (FIXTURES / note["file"]).read_bytes()
        mime = "audio/wav" if note["file"].endswith(".wav") else "audio/mpeg"
        before = len(counter.requests) if counter else 0
        calls += 1
        started = time.perf_counter()
        try:
            extraction, engine, latency = await ai.extract_report(
                audio=audio, audio_mime=mime, text=None, photo=None, photo_mime=None,
                ui_language=note["expected_language"],
            )
        except ai.AIError as exc:
            failures += 1
            tried = counter.requests[before:] if counter else []
            print(f"\n=== {note['file']}: FAILED after {int((time.perf_counter() - started) * 1000)} ms: {exc}"
                  f"  (models tried: {', '.join(tried) or '-'})")
            continue
        place = SAMPLE_PLACES[i % len(SAMPLE_PLACES)]
        row = _row_from(extraction, rid=i + 1, place=place, engine=engine)
        rows.append(row)
        lang_ok, conf_lang = _confirmation_language_ok(extraction, note["expected_language"])
        tried = counter.requests[before:] if counter else [engine]
        print(f"\n=== {note['file']}")
        print(f"model:        {engine}  (HTTP requests: {len(tried)}: {', '.join(tried)})")
        print(f"latency:      {latency} ms")
        print(f"urgency:      {row['urgency_level']} {row['urgency_score']}  reasons: {', '.join(row['urgency_reasons'])}")
        print(f"language:     {extraction.language} (expected {note['expected_language']})")
        print(f"original:     {extraction.transcript_original}")
        print(f"english:      {extraction.transcript_english}")
        print(f"summary:      {extraction.ai_summary}  ({len(extraction.ai_summary.split())} words)")
        print(f"confirmation: {extraction.confirmation_message}")
        print(f"  {'PASS' if lang_ok else 'FAIL'}  confirmation in the reporter's language "
              f"({conf_lang}, expected {note['expected_language']})")
        print(f"place said:   {extraction.location_hint!r}")
        checks = _check(extraction, note, row)
        for line in checks:
            print(line)
        failures += sum(1 for line in checks if "FAIL" in line) + (0 if lang_ok else 1)
    return rows, calls, failures


async def check_briefing(rows: list[dict]) -> None:
    sample = list(rows)
    extra = fallback_rules.extract_from_text("Water in the basement, about 30 cm, sewage coming up from the drain")
    sample.append(_row_from(extra, rid=90, place=("Warren Ave & Schaefer Rd", 42.34420, -83.17650), engine="rules"))
    extra = fallback_rules.extract_from_text("The street is flooded at Schaefer, cars stalled")
    sample.append(_row_from(extra, rid=91, place=("Schaefer Rd near Warren Ave", 42.34300, -83.17680), engine="rules"))
    started = time.perf_counter()
    out = await briefing.build_briefing(sample)
    print(f"\n=== sitrep  engine={out['engine']}  latency={int((time.perf_counter() - started) * 1000)} ms")
    print(out["text"])
    for h in out["hotspots"]:
        print(f"  hotspot {h['label']}: {h['level']} r={h['radius_m']} m ids={h['report_ids']}")


async def check_geocoding(hints: list[str]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="floodline-geo-"))
    config.GEOCODE_CACHE_PATH = tmp / "geocode_cache.json"  # throwaway cache so requests really happen
    geocode.clear_memory_cache()
    queries = hints or ["Warren and Schaefer"]
    print(f"\n=== geocoding spoken places (budget {GEO_BUDGET} HTTP requests, 1/s)")
    for query in queries:
        if geocode.request_count >= GEO_BUDGET - 1:
            print(f"budget reached, skipping {query!r}")
            continue
        before = geocode.request_count
        started = time.perf_counter()
        result = await geocode.geocode(query)
        ms = int((time.perf_counter() - started) * 1000)
        print(f"{query!r}: {result}  ({geocode.request_count - before} requests, {ms} ms)")
    print(f"total geocoding requests: {geocode.request_count}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="test only this model (no fallbacks), e.g. gemini-2.5-flash")
    parser.add_argument("--max-calls", type=int, default=4, help="logical Gemini calls at most (default 4)")
    parser.add_argument("--no-briefing", action="store_true", help="skip the sitrep call")
    parser.add_argument("--wav", action="store_true", help="send the 16 kHz WAV fixture first (what phones record)")
    parser.add_argument("--geo", action="store_true", help="also geocode the places the notes mention (<= 6 requests)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show the AI module's log lines")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    if args.model:
        config.GEMINI_MODEL = args.model
        config.GEMINI_FALLBACK_MODELS = []
    print(f"model chain: {ai.model_chain()}   timeout: {config.AI_TIMEOUT_S} s   max calls: {args.max_calls}")
    if not ai.ai_enabled():
        print("GEMINI_API_KEY is not set (.env or environment): no Gemini calls made.")
        if args.geo:
            await check_geocoding([])
        return 2

    counter = CountingClient(await ai._client_for_call())
    ai._get_client = lambda: counter  # count every HTTP call the chain makes
    voice_budget = args.max_calls - (0 if args.no_briefing else 1) if args.max_calls > 1 else args.max_calls
    rows, calls, failures = await check_voice_notes(voice_budget, args.wav, counter)
    if not args.no_briefing and calls < args.max_calls:
        await check_briefing(rows)
        calls += 1
    print(f"\nlogical calls: {calls}   HTTP requests to Gemini: {len(counter.requests)}   "
          f"unavailable to this key: {sorted(ai._unavailable) or 'none'}   answered last: {ai.active_model()}")
    if args.geo:
        await check_geocoding([r["location_hint"] for r in rows if r.get("location_hint")])
    print("RESULT:", "all checks passed" if failures == 0 else f"{failures} check(s) failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
