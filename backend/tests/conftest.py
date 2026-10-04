"""Shared fixtures for the backend-core tests (test_api.py, test_db_events.py).

Every test gets its own data directory under tmp_path and fakes for everything that would
touch the network or depend on other modules' current behavior (AI, geocoding, urgency rules,
seed data). Tests that need specific behavior monkeypatch those fakes again.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import ai, config, db, geocode, main, seed_data, urgency
from app.events import broker
from app.models import Extraction


def fake_score(report: Any) -> dict:
    """Stand-in for urgency.score_report: trapped -> CRITICAL, everything else MEDIUM."""
    if report.get("ai_status") == "pending":
        return {"urgency_score": None, "urgency_level": None, "urgency_reasons": []}
    people = report.get("people_at_risk") or {}
    if people.get("trapped"):
        return {"urgency_score": 90, "urgency_level": "CRITICAL", "urgency_reasons": ["trapped"]}
    return {"urgency_score": 50, "urgency_level": "MEDIUM", "urgency_reasons": ["fake"]}


def make_extraction(**overrides: Any) -> Extraction:
    data: dict[str, Any] = {
        "language": "ar",
        "transcript_original": "المية وصلت للركبة بالقبو وأمي ما بتقدر تطلع",
        "transcript_english": "The water reached the knee in the basement and my mother cannot get out",
        "ai_summary": "Elderly woman trapped in basement, knee-deep water",
        "confirmation_message": "استلمنا بلاغك. إذا كانت الحياة في خطر اتصل بـ 911.",
        "water_depth_cm": 50,
        "location_type": "basement",
        "location_hint": None,
        "water_in_living_space": True,
        "water_rising": True,
        "hazards": {"electrical": False, "sewage": True, "gas": False, "structural": False},
        "people_at_risk": {"elderly": True, "trapped": True, "count": 2},
        "needs": {"evacuation": True},
    }
    data.update(overrides)
    return Extraction.model_validate(data)


async def _no_geocode(query: str) -> None:
    return None


async def _no_reverse(lat: float, lng: float) -> None:
    return None


async def _ai_should_not_run(**kwargs: Any) -> Any:
    raise ai.AIError("AI is disabled in tests unless a test enables it")


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Point every storage path at tmp_path and give the db module a fresh connection."""
    data_dir = tmp_path / "data"
    env = SimpleNamespace(
        tmp=tmp_path,
        data_dir=data_dir,
        db_path=data_dir / "floodline.db",
        upload_dir=data_dir / "uploads",
        seed_audio_dir=tmp_path / "seed_audio",
        dist_dir=tmp_path / "dist",  # does not exist unless a test builds one
    )
    monkeypatch.setattr(config, "DATA_DIR", env.data_dir)
    monkeypatch.setattr(config, "DB_PATH", env.db_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", env.upload_dir)
    monkeypatch.setattr(config, "GEOCODE_CACHE_PATH", data_dir / "geocode_cache.json")
    monkeypatch.setattr(config, "SEED_AUDIO_DIR", env.seed_audio_dir)
    monkeypatch.setattr(config, "FRONTEND_DIST", env.dist_dir)
    db.close()
    db.init_db()
    yield env
    db.close()  # release the file so Windows can delete tmp_path


@pytest.fixture
def app_env(db_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """db_env plus app settings and fakes: no seeds, AI off, no network, predictable urgency."""
    monkeypatch.setattr(config, "SEED_ON_START", False)
    monkeypatch.setattr(config, "PUBLIC_URL", None)
    monkeypatch.setattr(config, "POST_WAIT_S", 5.0)
    monkeypatch.setattr(config, "AI_TIMEOUT_S", 5.0)
    monkeypatch.setattr(ai, "ai_enabled", lambda: False)
    monkeypatch.setattr(ai, "active_model", lambda: None)
    monkeypatch.setattr(ai, "extract_report", _ai_should_not_run)
    monkeypatch.setattr(geocode, "geocode", _no_geocode)
    monkeypatch.setattr(geocode, "reverse_geocode", _no_reverse)
    monkeypatch.setattr(urgency, "score_report", fake_score)
    monkeypatch.setattr(seed_data, "seed_rows", lambda now: [])
    return db_env


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every event published to the broker during the test, in order (still delivered as usual)."""
    events: list[dict] = []
    original = broker.publish

    def record(event: dict) -> None:
        events.append(event)
        original(event)

    monkeypatch.setattr(broker, "publish", record)
    return events


@pytest.fixture
def client(app_env: SimpleNamespace) -> Iterator[TestClient]:
    with TestClient(main.app) as test_client:
        yield test_client


def wait_for_report(
    client: TestClient, report_id: int, predicate: Callable[[dict], bool], timeout: float = 10.0
) -> dict:
    """Poll GET /api/reports/{id} until predicate(report) holds; fail after timeout seconds."""
    deadline = time.monotonic() + timeout
    report: dict = {}
    while time.monotonic() < deadline:
        report = client.get(f"/api/reports/{report_id}").json()
        if predicate(report):
            return report
        time.sleep(0.05)
    raise AssertionError(f"report {report_id} never reached the expected state; last seen: {report}")
