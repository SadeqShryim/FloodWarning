"""Seed data (contract section 10) and the storm-mode controller. No network, no database."""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app import config, seed_data, storm
from app.models import Report
from app.storm import POOL, StormController
from app.urgency import score_report
from app.util import haversine_m, parse_iso

ARABIC = re.compile(r"[؀-ۿ]")
HOUSE_NUMBER = re.compile(r"\b\d+\b(?! block)")  # "7000 block of Chase Rd" is fine, "7012 Chase Rd" is not
NOW = datetime(2026, 10, 4, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def seeds():
    return seed_data.seed_rows(NOW)


def levels(rows):
    return [score_report(r)["urgency_level"] for r in rows]


def in_viewbox(lat, lng):
    west, south, east, north = config.DEARBORN_VIEWBOX
    return south <= lat <= north and west <= lng <= east


# ---------------------------------------------------------------- seeds


def test_exactly_25_seeds_with_the_level_mix(seeds):
    assert len(seeds) == 25
    counts = Counter(levels(seeds))
    assert counts["CRITICAL"] == 4
    assert 7 <= counts["HIGH"] <= 8
    assert 7 <= counts["MEDIUM"] <= 8
    assert 5 <= counts["LOW"] <= 6


def test_seed_languages_and_statuses(seeds):
    assert Counter(r["language"] for r in seeds) == {"en": 10, "ar": 9, "es": 6}
    statuses = Counter(r["status"] for r in seeds)
    assert statuses == {"new": 19, "dispatched": 4, "resolved": 2}
    dispatched_levels = [lvl for r, lvl in zip(seeds, levels(seeds)) if r["status"] == "dispatched"]
    assert dispatched_levels.count("CRITICAL") == 1
    for r in seeds:
        assert r["ui_language"] == r["language"]
    assert Counter(r["input_type"] for r in seeds)["voice"] >= 20


def test_seed_times_spread_over_150_minutes(seeds):
    times = [parse_iso(r["created_at"]) for r in seeds]
    for t in times:
        assert NOW - timedelta(minutes=150) <= t <= NOW
    assert max(times) - min(times) >= timedelta(minutes=120)  # actually spread out
    assert all(r["updated_at"] == r["created_at"] for r in seeds)


def test_seed_fields_complete_and_valid(seeds):
    for r in seeds:
        assert "id" not in r
        assert not {"urgency_score", "urgency_level", "urgency_reasons"} & r.keys()
        assert r["ai_status"] == "done" and r["ai_engine"] == "seed"
        assert 1800 <= r["ai_latency_ms"] <= 4200
        assert r["is_simulated"] is True and r["location_source"] == "seed"
        assert set(r["hazards"]) == {"electrical", "sewage", "gas", "structural"}
        assert set(r["people_at_risk"]) == {"elderly", "children", "disabled", "medical", "trapped", "count"}
        assert set(r["needs"]) == {"evacuation", "pumping", "medical", "supplies"}
        for key in ("transcript_original", "transcript_english", "ai_summary", "confirmation_message"):
            assert isinstance(r[key], str) and r[key].strip(), (key, r["address_text"])
        assert "911" in r["confirmation_message"]
        # Same shape the API serves (Literal location_type, status, etc.).
        api = {k: v for k, v in r.items() if k not in ("seed_audio", "audio_path", "audio_mime", "photo_path", "photo_mime")}
        Report.model_validate({**api, "id": 1})


def test_seed_locations_are_real_dearborn_intersections(seeds):
    for r in seeds:
        assert in_viewbox(r["lat"], r["lng"]), r["address_text"]
        assert r["lat"] == round(r["lat"], 5) and r["lng"] == round(r["lng"], 5)
        text = r["address_text"]
        assert "&" in text or " block of " in text, text
        assert not HOUSE_NUMBER.search(text), text
    assert len({r["address_text"] for r in seeds}) == 25  # every seed at its own corner


def test_seed_transcripts_in_the_right_script(seeds):
    for r in seeds:
        has_arabic = bool(ARABIC.search(r["transcript_original"]))
        assert has_arabic == (r["language"] == "ar"), r["address_text"]
        assert bool(ARABIC.search(r["confirmation_message"])) == (r["language"] == "ar")
        assert not ARABIC.search(r["transcript_english"]) and not ARABIC.search(r["ai_summary"])
        if r["language"] == "en":
            assert r["transcript_original"] == r["transcript_english"]


def test_seed_audio_files_exist_and_are_small(seeds):
    with_audio = [r for r in seeds if r.get("seed_audio")]
    assert len(with_audio) >= 6
    assert all(n >= 2 for n in Counter(r["language"] for r in with_audio).values())
    assert set(Counter(r["language"] for r in with_audio)) == {"en", "ar", "es"}
    assert levels(with_audio).count("CRITICAL") >= 2
    for r in with_audio:
        name = r["seed_audio"]
        assert re.fullmatch(r"seed_\d{2}_(en|ar|es)\.mp3", name), name
        assert name.endswith(f"_{r['language']}.mp3")
        path = config.SEED_AUDIO_DIR / name
        assert path.is_file(), path
        assert 5_000 < path.stat().st_size < 150 * 1024, path


def test_seed_rows_are_fresh_copies():
    a = seed_data.seed_rows(NOW)
    a[0]["hazards"]["gas"] = True
    a[0]["people_at_risk"]["count"] = 99
    b = seed_data.seed_rows(NOW)
    assert b[0]["hazards"]["gas"] is False
    assert b[0]["people_at_risk"]["count"] != 99


# ---------------------------------------------------------------- storm pool


def test_storm_pool_quality():
    assert len(POOL) >= 24
    scored = [score_report({**item, "ai_status": "done"})["urgency_level"] for item in POOL]
    assert 5 <= scored.count("CRITICAL") <= 7
    assert {"HIGH", "MEDIUM", "LOW"} <= set(scored)
    langs = Counter(item["language"] for item in POOL)
    assert set(langs) == {"en", "ar", "es"} and min(langs.values()) >= 5
    seed_places = {r["address_text"] for r in seed_data.seed_rows(NOW)}
    fresh = [item for item in POOL if item["address_text"] not in seed_places]
    assert len(fresh) >= len(POOL) * 0.75
    for item in POOL:
        assert in_viewbox(item["lat"], item["lng"]), item["address_text"]
        assert not HOUSE_NUMBER.search(item["address_text"])
        for key in seed_data.EXTRACTION_KEYS:
            assert key in item
        for key in ("transcript_original", "transcript_english", "ai_summary", "confirmation_message"):
            assert item[key].strip()
        assert bool(ARABIC.search(item["transcript_original"])) == (item["language"] == "ar")


# ---------------------------------------------------------------- storm controller


class Fakes:
    """Stand-ins for service.add_report / apply_update and the storm.state publisher."""

    def __init__(self, fail_first_add: bool = False):
        self.added: list[dict] = []
        self.updates: list[tuple[int, dict]] = []
        self.states: list[tuple[bool, int]] = []
        self.fail_first_add = fail_first_add
        self._next_id = 100

    async def add_report(self, row: dict) -> dict:
        if self.fail_first_add:
            self.fail_first_add = False
            raise RuntimeError("database hiccup")
        self._next_id += 1
        stored = {**row, "id": self._next_id}
        self.added.append(stored)
        return stored

    async def apply_update(self, report_id: int, fields: dict) -> dict | None:
        self.updates.append((report_id, fields))
        return {"id": report_id, **fields}

    async def on_state(self, running: bool, injected: int) -> None:
        self.states.append((running, injected))


def make(fakes: Fakes, delay=(0.01, 0.03)) -> StormController:
    controller = StormController(fakes.add_report, fakes.apply_update, fakes.on_state)
    controller.finish_delay_s = delay
    return controller


async def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


def other_tasks():
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


def run(coro_fn):
    """asyncio.run with a loop exception handler that turns 'Task was destroyed' and friends into failures."""
    problems: list[dict] = []

    async def wrapper():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: problems.append(ctx))
        await coro_fn()

    asyncio.run(wrapper())
    assert not problems, problems


def test_storm_injects_pending_then_finishes_and_stops_at_max():
    fakes = Fakes()

    async def scenario():
        controller = make(fakes)
        controller.start(max_reports=3, min_interval_s=0.01, max_interval_s=0.02)
        assert controller.running
        await wait_until(lambda: not controller.running and len(fakes.updates) == 3)
        assert controller.injected == 3
        await asyncio.sleep(0.05)  # nothing more arrives after max_reports
        assert len(fakes.added) == 3
        await controller.stop()  # harmless after a natural end
        assert other_tasks() == []

    run(scenario)

    for row in fakes.added:
        assert row["ai_status"] == "pending"
        assert row["location_source"] == "storm"
        assert row["is_simulated"] is True
        assert not row.get("transcript_original")
        assert row["status"] == "new"
        assert set(row["people_at_risk"]) == {"elderly", "children", "disabled", "medical", "trapped", "count"}
    finished_ids = sorted(rid for rid, _ in fakes.updates)
    assert finished_ids == sorted(r["id"] for r in fakes.added)
    for report_id, fields in fakes.updates:
        assert fields["ai_status"] == "done"
        assert fields["ai_engine"] == "storm-sim"
        assert isinstance(fields["ai_latency_ms"], int)
        assert fields["transcript_original"]
        for key in seed_data.EXTRACTION_KEYS:
            assert key in fields
    # Jitter stays near the pool location (~120 m).
    for row, item in zip(fakes.added, POOL):
        assert row["address_text"] == item["address_text"]
        assert haversine_m(row["lat"], row["lng"], item["lat"], item["lng"]) <= 125
    assert fakes.states[0] == (True, 0)
    assert (True, 1) in fakes.states and (True, 2) in fakes.states
    assert fakes.states[-1] == (False, 3)


def test_storm_start_is_idempotent():
    fakes = Fakes()

    async def scenario():
        controller = make(fakes)
        controller.start(max_reports=2, min_interval_s=0.01, max_interval_s=0.01)
        controller.start(max_reports=2, min_interval_s=0.01, max_interval_s=0.01)  # no second loop
        controller.start(max_reports=50)
        await wait_until(lambda: not controller.running and len(fakes.updates) == 2)
        await asyncio.sleep(0.05)
        assert len(fakes.added) == 2
        assert other_tasks() == []

    run(scenario)
    assert fakes.states.count((True, 0)) == 1


def test_storm_stop_is_prompt_and_flushes_pending_finishes():
    fakes = Fakes()

    async def scenario():
        controller = make(fakes, delay=(30.0, 30.0))  # finishes would take 30 s on their own
        controller.start(max_reports=24, min_interval_s=30.0, max_interval_s=30.0)
        await wait_until(lambda: len(fakes.added) == 1)
        started = time.monotonic()
        await controller.stop()
        assert time.monotonic() - started < 1.0
        assert not controller.running
        assert len(fakes.updates) == 1  # the pending report got its fields instead of staying "processing"
        assert other_tasks() == []
        await asyncio.sleep(0.05)
        assert len(fakes.added) == 1

    run(scenario)
    assert fakes.states[0] == (True, 0)
    assert fakes.states[-1] == (False, 1)
    assert fakes.states.count((False, 1)) == 1  # one stop notification, not two


def test_storm_can_restart_after_stop_with_new_places():
    fakes = Fakes()

    async def scenario():
        controller = make(fakes)
        controller.start(max_reports=2, min_interval_s=0.01, max_interval_s=0.01)
        await wait_until(lambda: not controller.running)
        await controller.stop()
        controller.start(max_reports=1, min_interval_s=0.01, max_interval_s=0.01)
        await wait_until(lambda: not controller.running and len(fakes.updates) == 3)
        assert controller.injected == 1
        await controller.stop()
        assert other_tasks() == []

    run(scenario)
    assert [r["address_text"] for r in fakes.added] == [item["address_text"] for item in POOL[:3]]


def test_storm_survives_errors_in_callbacks():
    fakes = Fakes(fail_first_add=True)

    async def broken_update(report_id, fields):
        raise RuntimeError("update failed")

    async def scenario():
        controller = StormController(fakes.add_report, broken_update, fakes.on_state)
        controller.finish_delay_s = (0.01, 0.01)
        controller.start(max_reports=2, min_interval_s=0.01, max_interval_s=0.01)
        await wait_until(lambda: not controller.running)
        await controller.stop()
        assert other_tasks() == []

    run(scenario)
    # The first add raised and was logged; the loop went on and injected two real reports.
    assert len(fakes.added) == 2


def test_storm_stop_when_idle_is_a_noop():
    fakes = Fakes()

    async def scenario():
        controller = make(fakes)
        await controller.stop()
        assert not controller.running and controller.injected == 0

    run(scenario)
    assert fakes.states == []


def test_jitter_stays_within_radius():
    import random

    rng = random.Random(7)
    for _ in range(500):
        lat, lng = storm._jitter(42.31, -83.2, rng)
        assert haversine_m(42.31, -83.2, lat, lng) <= storm.JITTER_M + 1
