"""Unit tests for the storage layer, the event broker and the service helpers (no HTTP)."""
from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from app import ai, config, db, fallback_rules, seed_data, service, urgency, util
from app.events import RESYNC, Broker
from conftest import fake_score

# ---------------------------------------------------------------- db


def test_insert_normalizes_and_round_trips(db_env: Any) -> None:
    stored = db.insert_report(
        {
            "id": 99,  # ignored: ids belong to SQLite
            "lat": "42.31",
            "lng": -83.2,
            "transcript_original": "الماء يرتفع في الطابق السفلي",
            "water_rising": 1,
            "water_in_living_space": "true",
            "hazards": {"electrical": 1},
            "people_at_risk": '{"elderly": true, "count": "3"}',
            "urgency_reasons": ["elderly", "water rising"],
            "not_a_column": "dropped",
        }
    )
    assert stored["id"] == 1
    assert stored["lat"] == 42.31 and stored["lng"] == -83.2
    assert stored["transcript_original"] == "الماء يرتفع في الطابق السفلي"
    assert stored["water_rising"] is True and stored["water_in_living_space"] is True
    assert stored["is_simulated"] is False
    assert stored["hazards"] == {"electrical": True, "sewage": False, "gas": False, "structural": False}
    assert stored["people_at_risk"] == {
        "elderly": True, "children": False, "disabled": False, "medical": False, "trapped": False, "count": 3,
    }
    assert stored["needs"] == {"evacuation": False, "pumping": False, "medical": False, "supplies": False}
    assert stored["urgency_reasons"] == ["elderly", "water rising"]
    # Defaults for a new report.
    assert stored["status"] == "new" and stored["ai_status"] == "pending"
    assert stored["ui_language"] == "en" and stored["input_type"] == "voice"
    assert stored["created_at"].endswith("Z") and stored["updated_at"] == stored["created_at"]
    assert "not_a_column" not in stored
    assert db.get_report(1) == stored
    assert set(stored) == set(db.ALL_COLUMNS)


def test_insert_keeps_given_timestamps(db_env: Any) -> None:
    stored = db.insert_report({"created_at": "2026-10-04T09:00:00Z"})
    assert stored["created_at"] == "2026-10-04T09:00:00Z"
    assert stored["updated_at"] == "2026-10-04T09:00:00Z"


def test_update_sets_updated_at_and_handles_missing(db_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "_last_write_ms", 0)
    monkeypatch.setattr(db, "_now_ms", lambda: 1_791_108_000_000)  # 2026-10-04T10:00:00.000Z
    db.insert_report({"status": "new"})
    monkeypatch.setattr(db, "_now_ms", lambda: 1_791_108_300_250)  # 2026-10-04T10:05:00.250Z
    updated = db.update_report(1, {"status": "dispatched", "needs": {"pumping": True}, "id": 7, "bogus": 1})
    assert updated is not None
    assert updated["id"] == 1
    assert updated["status"] == "dispatched"
    assert updated["needs"]["pumping"] is True and updated["needs"]["supplies"] is False
    assert updated["created_at"] == "2026-10-04T10:00:00.000Z"  # inserts get millisecond stamps too
    assert updated["updated_at"] == "2026-10-04T10:05:00.250Z"  # milliseconds: see the next test
    assert db.update_report(42, {"status": "resolved"}) is None


def test_updates_in_the_same_millisecond_are_still_ordered(db_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without an AI key, "pending" and "failed" land within a millisecond or two of each other.
    # Dashboards keep the copy with the newest updated_at (an HTTP response can arrive after the
    # live event), so every write needs a strictly later stamp, even when the clock has not moved.
    monkeypatch.setattr(db, "_last_write_ms", 0)
    monkeypatch.setattr(db, "_now_ms", lambda: 1_791_108_300_250)
    inserted = db.insert_report({})
    pending = db.update_report(1, {"ai_status": "pending"})
    failed = db.update_report(1, {"ai_status": "failed"})
    assert pending is not None and failed is not None
    assert inserted["created_at"] == inserted["updated_at"] == "2026-10-04T10:05:00.250Z"
    assert pending["updated_at"] == "2026-10-04T10:05:00.251Z"
    assert failed["updated_at"] == "2026-10-04T10:05:00.252Z"
    assert util.parse_iso(failed["updated_at"]) > util.parse_iso(pending["updated_at"])


def test_list_count_clear_restarts_ids(db_env: Any) -> None:
    for _ in range(3):
        db.insert_report({})
    assert [r["id"] for r in db.list_reports()] == [1, 2, 3]
    assert db.count_reports() == 3
    db.clear_reports()
    assert db.count_reports() == 0 and db.list_reports() == []
    assert db.insert_report({})["id"] == 1


def test_close_and_reopen_keeps_data_in_wal_mode(db_env: Any) -> None:
    db.insert_report({"address_text": "Dix Ave & Vernor Hwy"})
    db.close()
    assert db.get_report(1)["address_text"] == "Dix Ave & Vernor Hwy"  # reopened lazily
    with db._lock:
        (mode,) = db._connection().execute("PRAGMA journal_mode").fetchone()
    assert mode.lower() == "wal"


def test_switching_db_path_switches_files(db_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    db.insert_report({})
    monkeypatch.setattr(config, "DB_PATH", db_env.tmp / "other" / "second.db")
    assert db.count_reports() == 0
    monkeypatch.setattr(config, "DB_PATH", db_env.db_path)
    assert db.count_reports() == 1


def test_init_db_creates_directories(db_env: Any) -> None:
    assert db_env.data_dir.is_dir()
    assert db_env.upload_dir.is_dir()


def test_concurrent_writes_from_threads(db_env: Any) -> None:
    def worker() -> None:
        for _ in range(20):
            row = db.insert_report({"status": "new"})
            db.update_report(row["id"], {"status": "resolved"})

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert db.count_reports() == 80
    assert all(r["status"] == "resolved" for r in db.list_reports())


# ---------------------------------------------------------------- broker


def test_broker_fans_out_and_unsubscribes() -> None:
    async def scenario() -> None:
        broker = Broker()
        first, second = broker.subscribe(), broker.subscribe()
        broker.publish({"type": "reset"})
        assert first.get_nowait() == {"type": "reset"}
        assert second.get_nowait() == {"type": "reset"}
        broker.unsubscribe(first)
        broker.unsubscribe(first)  # twice is harmless
        broker.publish({"type": "storm.state", "running": True, "injected": 1})
        assert first.empty()
        assert second.get_nowait()["type"] == "storm.state"
        assert broker.subscriber_count == 1

    asyncio.run(scenario())


def test_broker_cuts_off_a_full_queue_with_a_resync_marker() -> None:
    async def scenario() -> None:
        broker = Broker(queue_size=2)
        slow, fast = broker.subscribe(), broker.subscribe()
        for n in range(5):
            broker.publish({"type": "report.created", "n": n})  # must not block or raise
            assert fast.get_nowait()["n"] == n  # a reader that keeps up gets every event
        # The stalled one lost its stale backlog and got one marker telling its stream to end.
        assert slow.qsize() == 1 and slow.get_nowait() is RESYNC
        assert broker.subscriber_count == 1
        broker.unsubscribe(slow)  # what the SSE generator's finally does; harmless now

    asyncio.run(scenario())


def test_broker_publish_from_a_worker_thread() -> None:
    async def scenario() -> dict:
        broker = Broker()
        queue = broker.subscribe()
        await asyncio.to_thread(broker.publish, {"type": "reset"})
        return await asyncio.wait_for(queue.get(), timeout=2)

    assert asyncio.run(scenario()) == {"type": "reset"}


def test_broker_forgets_subscribers_of_a_closed_loop() -> None:
    broker = Broker()

    async def subscribe_only() -> None:
        broker.subscribe()

    asyncio.run(subscribe_only())  # the loop closes with the queue still subscribed
    broker.publish({"type": "reset"})
    assert broker.subscriber_count == 0


# ---------------------------------------------------------------- service helpers


def test_to_api_hides_paths_and_adds_urls() -> None:
    row = db.normalize_row({"id": 5, "audio_path": "abc.wav", "audio_mime": "audio/wav", "photo_path": None,
                            "created_at": "2026-10-04T10:00:00Z", "updated_at": "2026-10-04T10:00:00Z"})
    api_row = service.to_api(row)
    assert api_row["audio_url"] == "/api/reports/5/audio"
    assert api_row["photo_url"] is None
    for hidden in ("audio_path", "audio_mime", "photo_path", "photo_mime"):
        assert hidden not in api_row
    from app.models import Report

    assert set(api_row) == set(Report.model_fields)
    Report.model_validate(api_row)  # the shape the frontend expects


def test_add_report_scores_and_survives_a_broken_scorer(db_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urgency, "score_report", fake_score)
    pending = asyncio.run(service.add_report({"ai_status": "pending"}))
    assert pending["urgency_level"] is None and pending["urgency_reasons"] == []
    done = asyncio.run(service.add_report({"ai_status": "done", "people_at_risk": {"trapped": True}}))
    assert done["urgency_level"] == "CRITICAL"

    def broken(report: Any) -> dict:
        raise ValueError("bug in the rules")

    monkeypatch.setattr(urgency, "score_report", broken)
    kept = asyncio.run(service.add_report({"ai_status": "done"}))
    assert kept["id"] == 3
    assert kept["urgency_level"] == "MEDIUM" and kept["urgency_reasons"] == ["needs review"]


def test_apply_update_recomputes_urgency_and_handles_missing(db_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urgency, "score_report", fake_score)
    row = asyncio.run(service.add_report({"ai_status": "pending"}))
    updated = asyncio.run(service.apply_update(row["id"], {"ai_status": "done", "people_at_risk": {"trapped": True}}))
    assert updated["urgency_level"] == "CRITICAL" and updated["urgency_score"] == 90
    assert asyncio.run(service.apply_update(999, {"status": "resolved"})) is None


def test_enrich_report_never_raises(db_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urgency, "score_report", fake_score)
    monkeypatch.setattr(ai, "ai_enabled", lambda: True)

    async def weird_result(**kwargs: Any) -> Any:
        return "not a tuple"

    def broken_fallback(text: str, ui_language: str = "en") -> Any:
        raise RuntimeError("fallback bug")

    monkeypatch.setattr(ai, "extract_report", weird_result)
    monkeypatch.setattr(fallback_rules, "extract_from_text", broken_fallback)
    row = asyncio.run(service.add_report({"input_type": "text", "transcript_original": "help", "ai_status": "pending"}))
    result = asyncio.run(service.enrich_report(row["id"]))
    assert result["ai_status"] == "failed"
    assert result["transcript_original"] == "help"  # nothing the reporter said was lost
    assert asyncio.run(service.enrich_report(12345)) == {}


def test_queue_order_matches_frontend_rules() -> None:
    rows = [
        {"id": 1, "status": "resolved", "ai_status": "done", "urgency_level": "CRITICAL", "urgency_score": 99,
         "created_at": "2026-10-04T10:00:00Z"},
        {"id": 2, "status": "new", "ai_status": "done", "urgency_level": "LOW", "urgency_score": 20,
         "created_at": "2026-10-04T10:00:00Z"},
        {"id": 3, "status": "new", "ai_status": "pending", "urgency_level": None, "urgency_score": None,
         "created_at": "2026-10-04T09:00:00Z"},
        {"id": 4, "status": "new", "ai_status": "done", "urgency_level": "HIGH", "urgency_score": 61,
         "created_at": "2026-10-04T08:00:00Z"},
        {"id": 5, "status": "new", "ai_status": "done", "urgency_level": "HIGH", "urgency_score": 75,
         "created_at": "2026-10-04T07:00:00Z"},
        {"id": 6, "status": "dispatched", "ai_status": "done", "urgency_level": "CRITICAL", "urgency_score": 90,
         "created_at": "2026-10-04T10:00:00Z"},
    ]
    assert [r["id"] for r in service.queue_order(rows)] == [3, 5, 4, 2, 6, 1]


def test_insert_seed_rows_copies_audio_and_scores(db_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    db_env.seed_audio_dir.mkdir()
    (db_env.seed_audio_dir / "seed_03_es.mp3").write_bytes(b"mp3")
    monkeypatch.setattr(urgency, "score_report", fake_score)
    monkeypatch.setattr(
        seed_data,
        "seed_rows",
        lambda now: [
            {"ai_status": "done", "seed_audio": "seed_03_es.mp3", "is_simulated": True},
            {"ai_status": "done", "seed_audio": "absent.mp3"},
            {"ai_status": "done"},
        ],
    )
    assert service.insert_seed_rows() == 3
    first, second, third = db.list_reports()
    assert first["audio_mime"] == "audio/mpeg"
    assert (config.UPLOAD_DIR / first["audio_path"]).read_bytes() == b"mp3"
    assert second["audio_path"] is None and third["audio_path"] is None
    assert all(r["urgency_level"] == "MEDIUM" for r in (first, second, third))


def test_upload_path_refuses_escapes(db_env: Any) -> None:
    assert service.upload_path("../floodline.db") is None
    assert service.upload_path(None) is None
    assert service.upload_path("abc.wav") == (config.UPLOAD_DIR / "abc.wav").resolve()
