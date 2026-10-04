"""HTTP API tests: reports end to end (AI on, off, failing, slow), files, config, storm, briefing,
reset, the SPA rules and the SSE stream. Nothing here touches the network."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import ai, briefing, config, db, fallback_rules, geocode, main, seed_data, storm
from app.events import broker
from conftest import make_extraction, wait_for_report

WAV = b"RIFF" + b"\x00" * 2044  # content does not matter: the AI is faked


def post_text(client: TestClient, text: str = "Water is coming into my basement", **fields: Any) -> httpx.Response:
    return client.post("/api/reports", data={"text": text, "ui_language": "en", **fields})


def post_voice(client: TestClient, audio: bytes = WAV, filename: str = "voice-note.wav",
               mime: str = "audio/wav", **fields: Any) -> httpx.Response:
    return client.post("/api/reports", files={"audio": (filename, audio, mime)}, data={"ui_language": "en", **fields})


def enable_ai(monkeypatch: pytest.MonkeyPatch, extract: Any) -> None:
    monkeypatch.setattr(ai, "ai_enabled", lambda: True)
    monkeypatch.setattr(ai, "active_model", lambda: "gemini-2.5-flash")
    monkeypatch.setattr(ai, "extract_report", extract)


class FakeStorm:
    """Stands in for storm.StormController so storm endpoints can be tested without timers."""

    instances: list[FakeStorm] = []

    def __init__(self, add_report: Any, apply_update: Any, on_state: Any) -> None:
        self.add_report, self.apply_update, self.on_state = add_report, apply_update, on_state
        self.running = False
        self.injected = 0
        self.start_calls: list[dict] = []
        self.stop_calls = 0
        FakeStorm.instances.append(self)

    def start(self, **kwargs: Any) -> None:
        self.start_calls.append(kwargs)
        self.running = True
        self.injected = 3

    async def stop(self) -> None:
        self.stop_calls += 1
        self.running = False


@pytest.fixture
def fake_storm(monkeypatch: pytest.MonkeyPatch) -> type[FakeStorm]:
    FakeStorm.instances = []
    monkeypatch.setattr(storm, "StormController", FakeStorm)
    return FakeStorm


# ---------------------------------------------------------------- health and basic routing


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"ok": True, "reports": 0, "ai_enabled": False, "ai_model": None}


def test_root_redirects_to_dashboard(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard"


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_unknown_api_path_is_json_404(client: TestClient, method: str) -> None:
    response = client.request(method, "/api/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not found"}


def test_frontend_not_built_page(client: TestClient) -> None:
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "Frontend not built" in response.text


def test_spa_fallback_serves_dist(client: TestClient, app_env: Any) -> None:
    dist = app_env.dist_dir
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>FloodLine SPA</title>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log('hi')", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")

    for path in ("/dashboard", "/report", "/some/client/route"):
        response = client.get(path)
        assert response.status_code == 200
        assert "FloodLine SPA" in response.text
        assert response.headers["content-type"].startswith("text/html")

    script = client.get("/assets/index-abc123.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")  # not text/plain from the registry
    assert "immutable" in script.headers["cache-control"]
    assert client.get("/favicon.svg").headers["content-type"].startswith("image/svg+xml")
    # Unknown API paths still never get the SPA.
    assert client.get("/api/nope").status_code == 404


# ---------------------------------------------------------------- creating reports


def test_typed_report_with_ai_disabled_is_saved_with_rules(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, published: list[dict]
) -> None:
    async def fake_geocode(query: str) -> dict | None:
        assert query == "Warren Ave & Schaefer Rd"
        return {"lat": 42.3436, "lng": -83.1757, "label": "Warren Ave & Schaefer Rd, Dearborn"}

    monkeypatch.setattr(geocode, "geocode", fake_geocode)
    response = post_text(client, "Water in my basement, about a foot", address_text="Warren Ave & Schaefer Rd",
                         ui_language="es")
    assert response.status_code == 200
    report = response.json()
    assert report["id"] == 1
    assert report["ai_status"] == "failed"
    assert report["ai_engine"] == "rules"
    assert report["ai_error"] == "AI not configured"
    assert report["input_type"] == "text"
    assert report["ui_language"] == "es"
    assert report["transcript_original"] == "Water in my basement, about a foot"
    assert report["urgency_level"] == "MEDIUM"  # from the fake scorer: no longer pending
    # Typed address geocoded; the typed text is kept as the label.
    assert report["location_source"] == "typed"
    assert (report["lat"], report["lng"]) == (42.3436, -83.1757)
    assert report["address_text"] == "Warren Ave & Schaefer Rd"
    assert report["audio_url"] is None and report["photo_url"] is None
    assert "audio_path" not in report

    assert [r["id"] for r in client.get("/api/reports").json()] == [1]
    types = [e["type"] for e in published]
    assert types == ["report.created", "report.updated"]
    assert published[0]["report"]["ai_status"] == "pending"
    assert published[0]["report"]["urgency_level"] is None
    assert published[1]["report"]["ai_status"] == "failed"


def test_voice_report_with_ai_merges_fields_and_geocodes_spoken_place(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    async def fake_extract(**kwargs: Any) -> tuple[Any, str, int]:
        calls.append(kwargs)
        return make_extraction(location_hint="Warren and Schaefer"), "gemini-2.5-flash", 2345

    geocoded: list[str] = []

    async def fake_geocode(query: str) -> dict | None:
        geocoded.append(query)
        return {"lat": 42.3436, "lng": -83.1757, "label": "Warren Ave & Schaefer Rd"}

    enable_ai(monkeypatch, fake_extract)
    monkeypatch.setattr(geocode, "geocode", fake_geocode)
    response = post_voice(client, ui_language="ar")
    assert response.status_code == 200
    report = response.json()
    assert report["ai_status"] == "done"
    assert report["ai_engine"] == "gemini-2.5-flash"
    assert report["ai_latency_ms"] == 2345
    assert report["ai_error"] is None
    assert report["input_type"] == "voice"
    assert report["language"] == "ar"
    assert report["transcript_original"].startswith("المية")
    assert report["transcript_english"].startswith("The water")
    assert report["water_depth_cm"] == 50
    assert report["location_type"] == "basement"
    assert report["water_rising"] is True
    assert report["people_at_risk"] == {
        "elderly": True, "children": False, "disabled": False, "medical": False, "trapped": True, "count": 2,
    }
    assert report["hazards"]["sewage"] is True and report["hazards"]["electrical"] is False
    assert report["urgency_level"] == "CRITICAL"
    assert report["urgency_reasons"] == ["trapped"]
    # No GPS and no typed address: the place from the voice note becomes the pin.
    assert geocoded == ["Warren and Schaefer"]
    assert report["location_source"] == "spoken"
    assert report["location_hint"] == "Warren and Schaefer"
    assert (report["lat"], report["lng"]) == (42.3436, -83.1757)
    assert report["address_text"] == "Warren Ave & Schaefer Rd"
    assert report["audio_url"] == f"/api/reports/{report['id']}/audio"
    # The AI got the audio bytes and the reporter's language.
    assert calls[0]["audio"] == WAV
    assert calls[0]["audio_mime"] == "audio/wav"
    assert calls[0]["ui_language"] == "ar"
    assert calls[0]["text"] is None and calls[0]["photo"] is None
    assert client.get("/api/config").json()["ai_model"] == "gemini-2.5-flash"


def test_gps_report_gets_reverse_geocoded_address(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    reverse_calls: list[tuple[float, float]] = []

    async def fake_reverse(lat: float, lng: float) -> str | None:
        reverse_calls.append((lat, lng))
        return "Schaefer Rd, East Dearborn"

    async def forward_must_not_run(query: str) -> None:
        raise AssertionError("forward geocoding is not needed when GPS is present")

    monkeypatch.setattr(geocode, "reverse_geocode", fake_reverse)
    monkeypatch.setattr(geocode, "geocode", forward_must_not_run)
    report = post_text(client, lat="42.3401", lng="-83.1702", accuracy_m="12").json()
    assert reverse_calls == [(42.3401, -83.1702)]
    assert report["location_source"] == "gps"
    assert report["address_text"] == "Schaefer Rd, East Dearborn"
    assert report["accuracy_m"] == 12
    assert (report["lat"], report["lng"]) == (42.3401, -83.1702)


@pytest.mark.parametrize(
    ("error", "expected_reason"),
    [(ai.AIError("quota exceeded"), "quota exceeded"), (RuntimeError("boom"), "AI error: RuntimeError")],
)
def test_ai_failure_keeps_the_report_and_its_audio(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception, expected_reason: str
) -> None:
    async def failing_extract(**kwargs: Any) -> Any:
        raise error

    enable_ai(monkeypatch, failing_extract)
    monkeypatch.setattr(
        fallback_rules, "failed_audio_fields",
        lambda ui_language: {"ai_summary": "Voice note - needs review", "confirmation_message": f"received ({ui_language})"},
    )
    response = post_voice(client, ui_language="es")
    assert response.status_code == 200
    report = response.json()
    assert report["ai_status"] == "failed"
    assert report["ai_error"] == expected_reason
    assert report["ai_engine"] is None
    assert report["ai_summary"] == "Voice note - needs review"
    assert report["confirmation_message"] == "received (es)"
    assert report["urgency_level"] == "MEDIUM"
    audio = client.get(report["audio_url"])
    assert audio.status_code == 200
    assert audio.content == WAV
    assert audio.headers["content-type"] == "audio/wav"


def test_slow_ai_returns_pending_then_completes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, published: list[dict]
) -> None:
    monkeypatch.setattr(config, "POST_WAIT_S", 0.1)

    async def slow_extract(**kwargs: Any) -> tuple[Any, str, int]:
        await asyncio.sleep(0.8)
        return make_extraction(), "gemini-2.5-flash", 800

    enable_ai(monkeypatch, slow_extract)
    started = time.monotonic()
    report = post_voice(client).json()
    assert time.monotonic() - started < 0.7  # did not wait for the AI
    assert report["ai_status"] == "pending"
    assert report["urgency_level"] is None and report["urgency_score"] is None
    done = wait_for_report(client, report["id"], lambda r: r["ai_status"] != "pending")
    assert done["ai_status"] == "done"
    assert done["urgency_level"] == "CRITICAL"
    assert [e["type"] for e in published] == ["report.created", "report.updated"]


def test_ai_wait_survives_timeouts_from_the_ai_module(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def timing_out(**kwargs: Any) -> Any:
        raise asyncio.TimeoutError()

    enable_ai(monkeypatch, timing_out)
    report = post_text(client, "Street is flooded on Ford Rd").json()
    assert report["ai_status"] == "failed"
    assert report["ai_error"] == "timeout"
    assert report["ai_engine"] == "rules"


def test_empty_transcript_counts_as_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty(**kwargs: Any) -> tuple[Any, str, int]:
        return make_extraction(transcript_original="   "), "gemini-2.5-flash", 900

    enable_ai(monkeypatch, empty)
    report = post_voice(client).json()
    assert report["ai_status"] == "failed"
    assert report["ai_error"] == "empty transcript"


# ---------------------------------------------------------------- validation


def test_report_needs_audio_or_text(client: TestClient) -> None:
    assert client.post("/api/reports").status_code == 422
    assert client.post("/api/reports", data={"text": "   ", "ui_language": "en"}).status_code == 422
    empty_audio = client.post("/api/reports", files={"audio": ("a.wav", b"", "audio/wav")})
    assert empty_audio.status_code == 422
    assert db.count_reports() == 0


def test_uploads_over_the_limit_are_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MAX_AUDIO_BYTES", 100)
    monkeypatch.setattr(config, "MAX_PHOTO_BYTES", 100)
    assert post_voice(client, audio=b"x" * 101).status_code == 413
    too_big_photo = client.post(
        "/api/reports", data={"text": "flooded"}, files={"photo": ("p.jpg", b"x" * 101, "image/jpeg")}
    )
    assert too_big_photo.status_code == 413
    assert db.count_reports() == 0
    assert post_voice(client, audio=b"x" * 100).status_code == 200


def test_unknown_ui_language_falls_back_to_english(client: TestClient) -> None:
    assert post_text(client, ui_language="fr").json()["ui_language"] == "en"
    assert post_text(client, ui_language="AR").json()["ui_language"] == "ar"


@pytest.mark.parametrize(("lat", "lng"), [("95", "-83.2"), ("42.3", "-200"), ("abc", "-83.2"), ("42.3", ""), ("nan", "1")])
def test_invalid_coordinates_are_ignored(client: TestClient, lat: str, lng: str) -> None:
    response = post_text(client, lat=lat, lng=lng, accuracy_m="10")
    assert response.status_code == 200
    report = response.json()
    assert report["lat"] is None and report["lng"] is None and report["accuracy_m"] is None
    assert report["location_source"] is None


def test_photo_and_audio_types_are_stored(client: TestClient) -> None:
    response = client.post(
        "/api/reports",
        files={
            "audio": ("note.webm", b"webm-bytes", "application/octet-stream"),
            "photo": ("street.jpg", b"jpeg-bytes", "image/jpeg"),
        },
        data={"text": "and here is a photo"},
    )
    report = response.json()
    assert report["input_type"] == "voice"
    audio = client.get(report["audio_url"])
    assert audio.headers["content-type"] == "audio/webm"
    photo = client.get(report["photo_url"])
    assert photo.status_code == 200 and photo.content == b"jpeg-bytes"
    assert photo.headers["content-type"] == "image/jpeg"
    row = db.get_report(report["id"])
    assert row["audio_path"].endswith(".webm") and row["photo_path"].endswith(".jpg")
    assert (config.UPLOAD_DIR / row["audio_path"]).is_file()


# ---------------------------------------------------------------- files


def test_audio_supports_range_requests(client: TestClient) -> None:
    audio = bytes(range(256)) * 8
    report = post_voice(client, audio=audio, filename="note.mp3", mime="audio/mpeg").json()
    full = client.get(report["audio_url"])
    assert full.headers["content-type"] == "audio/mpeg"
    assert full.headers.get("accept-ranges") == "bytes"
    partial = client.get(report["audio_url"], headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206
    assert partial.content == audio[:100]
    assert partial.headers["content-range"] == f"bytes 0-99/{len(audio)}"


def test_missing_files_and_reports_are_404(client: TestClient) -> None:
    report = post_text(client).json()
    assert client.get(f"/api/reports/{report['id']}/audio").status_code == 404
    assert client.get(f"/api/reports/{report['id']}/photo").status_code == 404
    assert client.get("/api/reports/999").status_code == 404
    assert client.get("/api/reports/999/audio").status_code == 404


# ---------------------------------------------------------------- updates


def test_patch_status_publishes_update(client: TestClient, published: list[dict]) -> None:
    report = post_text(client).json()
    published.clear()
    response = client.patch(f"/api/reports/{report['id']}", json={"status": "dispatched"})
    assert response.status_code == 200
    assert response.json()["status"] == "dispatched"
    assert response.json()["urgency_level"] == "MEDIUM"  # recomputed, still consistent
    assert [e["type"] for e in published] == ["report.updated"]
    assert published[0]["report"]["status"] == "dispatched"
    assert client.patch(f"/api/reports/{report['id']}", json={"status": "gone"}).status_code == 422
    assert client.patch("/api/reports/999", json={"status": "resolved"}).status_code == 404


def test_reprocess_reruns_the_ai(client: TestClient, monkeypatch: pytest.MonkeyPatch, published: list[dict]) -> None:
    report = post_voice(client).json()
    assert report["ai_status"] == "failed"

    async def slow_ok(**kwargs: Any) -> tuple[Any, str, int]:
        await asyncio.sleep(0.3)
        return make_extraction(), "gemini-2.5-flash", 300

    enable_ai(monkeypatch, slow_ok)
    published.clear()
    response = client.post(f"/api/reports/{report['id']}/reprocess")
    assert response.status_code == 200
    assert response.json()["ai_status"] == "pending"
    assert response.json()["ai_error"] is None
    # A second click while it runs does not start another AI call.
    assert client.post(f"/api/reports/{report['id']}/reprocess").json()["ai_status"] == "pending"
    done = wait_for_report(client, report["id"], lambda r: r["ai_status"] != "pending")
    assert done["ai_status"] == "done" and done["ai_engine"] == "gemini-2.5-flash"
    assert [e["type"] for e in published] == ["report.updated", "report.updated"]
    assert client.post("/api/reports/999/reprocess").status_code == 404


def test_reports_come_back_in_queue_order(client: TestClient) -> None:
    def add(**fields: Any) -> int:
        base = {"ai_status": "done", "status": "new", "created_at": "2026-10-04T10:00:00Z"}
        return db.insert_report({**base, **fields})["id"]

    resolved = add(status="resolved", urgency_level="CRITICAL", urgency_score=99)
    medium_old = add(urgency_level="MEDIUM", urgency_score=40, created_at="2026-10-04T09:00:00Z")
    critical = add(urgency_level="CRITICAL", urgency_score=85)
    dispatched = add(status="dispatched", urgency_level="CRITICAL", urgency_score=95)
    medium_new = add(urgency_level="MEDIUM", urgency_score=40, created_at="2026-10-04T11:00:00Z")
    pending = add(ai_status="pending")
    high = add(urgency_level="HIGH", urgency_score=70)
    ids = [r["id"] for r in client.get("/api/reports").json()]
    assert ids == [pending, critical, high, medium_new, medium_old, dispatched, resolved]


# ---------------------------------------------------------------- config


def test_config_and_public_url(client: TestClient, published: list[dict]) -> None:
    cfg = client.get("/api/config").json()
    assert cfg == {
        "public_url": None,
        "report_url": None,
        "ai_enabled": False,
        "ai_model": None,
        "storm_running": False,
        "storm_injected": 0,
        "map_center": list(config.MAP_CENTER),
        "map_zoom": config.MAP_ZOOM,
    }
    response = client.post("/api/config/public-url", json={"public_url": " https://abc-def.trycloudflare.com/ "})
    assert response.status_code == 200
    assert response.json()["public_url"] == "https://abc-def.trycloudflare.com"
    assert response.json()["report_url"] == "https://abc-def.trycloudflare.com/report"
    assert published[-1] == {"type": "config.updated", "config": response.json()}
    assert client.get("/api/config").json()["report_url"] == "https://abc-def.trycloudflare.com/report"

    for bad in ("ftp://example.com", "javascript:alert(1)", "example.com", "https://"):
        rejected = client.post("/api/config/public-url", json={"public_url": bad})
        assert rejected.status_code == 422, bad
    assert client.get("/api/config").json()["public_url"] == "https://abc-def.trycloudflare.com"

    cleared = client.post("/api/config/public-url", json={"public_url": None}).json()
    assert cleared["public_url"] is None and cleared["report_url"] is None


def test_public_url_initial_value_comes_from_config(app_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "PUBLIC_URL", "http://192.168.1.20:8000/")
    with TestClient(main.app) as client:
        assert client.get("/api/config").json()["report_url"] == "http://192.168.1.20:8000/report"


# ---------------------------------------------------------------- storm


def test_storm_start_and_stop(app_env: Any, fake_storm: type[FakeStorm], published: list[dict]) -> None:
    with TestClient(main.app) as client:
        controller = fake_storm.instances[-1]
        # Wired to the service layer, as the contract says.
        assert controller.add_report is main.service.add_report
        assert controller.apply_update is main.service.apply_update

        response = client.post("/api/storm/start", json={"max_reports": 5, "min_interval_s": 1, "max_interval_s": 2})
        assert response.json() == {"running": True, "injected": 3}
        assert controller.start_calls[-1] == {"max_reports": 5, "min_interval_s": 1.0, "max_interval_s": 2.0}

        # api.ts sends no body at all: defaults apply.
        assert client.post("/api/storm/start").status_code == 200
        assert controller.start_calls[-1] == {"max_reports": 24, "min_interval_s": 2.0, "max_interval_s": 4.5}
        assert client.post("/api/storm/start", json={"max_reports": 0}).status_code == 422

        cfg = client.get("/api/config").json()
        assert cfg["storm_running"] is True and cfg["storm_injected"] == 3

        assert client.post("/api/storm/stop").json() == {"running": False, "injected": 3}
        assert controller.stop_calls == 1

        asyncio.run(controller.on_state(True, 7))
        assert published[-1] == {"type": "storm.state", "running": True, "injected": 7}
    assert controller.stop_calls == 2  # stopped again on shutdown


# ---------------------------------------------------------------- briefing


def test_briefing_endpoint_shape(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    post_text(client)
    seen: list[list[dict]] = []

    async def fake_build(reports: list[dict]) -> dict:
        seen.append(reports)
        return {
            "text": "One cluster on Warren Ave. Send a pump crew first.",
            "hotspots": [
                {"label": "Warren Ave", "lat": 42.34, "lng": -83.17, "radius_m": 400, "level": "HIGH", "report_ids": [1]}
            ],
            "generated_at": "2026-10-04T15:00:00Z",
            "engine": "gemini-2.5-flash",
        }

    monkeypatch.setattr(briefing, "build_briefing", fake_build)
    result = client.post("/api/briefing").json()
    assert set(result) == {"text", "hotspots", "generated_at", "engine"}
    assert result["engine"] == "gemini-2.5-flash"
    assert result["hotspots"][0] == {
        "label": "Warren Ave", "lat": 42.34, "lng": -83.17, "radius_m": 400.0, "level": "HIGH", "report_ids": [1],
    }
    # It gets full row dicts, not API dicts.
    assert seen[0][0]["id"] == 1 and "hazards" in seen[0][0]


def test_briefing_never_500s(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(reports: list[dict]) -> dict:
        raise RuntimeError("bug")

    monkeypatch.setattr(briefing, "build_briefing", broken)
    response = client.post("/api/briefing")
    assert response.status_code == 200
    assert response.json()["engine"] == "rules" and response.json()["hotspots"] == []


# ---------------------------------------------------------------- seeds and reset


def _fake_seed_rows(now: Any) -> list[dict]:
    common = {
        "ai_status": "done", "ai_engine": "seed", "is_simulated": True, "location_source": "seed",
        "input_type": "voice", "status": "new", "ai_latency_ms": 2000,
    }
    return [
        {**common, "created_at": "2026-10-04T09:00:00Z", "address_text": "Warren Ave & Schaefer Rd",
         "lat": 42.3436, "lng": -83.1757, "language": "ar", "transcript_original": "المية عالية",
         "people_at_risk": {"trapped": True}, "seed_audio": "seed_01_ar.mp3"},
        {**common, "created_at": "2026-10-04T09:30:00Z", "address_text": "7000 block of Chase Rd",
         "lat": 42.33, "lng": -83.18, "language": "en", "transcript_original": "Street flooding",
         "seed_audio": "missing.mp3"},
    ]


def test_seeds_load_on_start_when_empty(app_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    app_env.seed_audio_dir.mkdir()
    (app_env.seed_audio_dir / "seed_01_ar.mp3").write_bytes(b"ID3-fake-mp3")
    monkeypatch.setattr(config, "SEED_ON_START", True)
    monkeypatch.setattr(seed_data, "seed_rows", _fake_seed_rows)
    with TestClient(main.app) as client:
        reports = client.get("/api/reports").json()
        assert [r["urgency_level"] for r in reports] == ["CRITICAL", "MEDIUM"]  # scored on insert
        assert reports[0]["audio_url"] == "/api/reports/1/audio"
        assert reports[1]["audio_url"] is None  # the missing file is skipped, the report is kept
        audio = client.get("/api/reports/1/audio")
        assert audio.content == b"ID3-fake-mp3"
        assert audio.headers["content-type"] == "audio/mpeg"
    # A second start does not seed again: the DB is not empty.
    with TestClient(main.app) as client:
        assert client.get("/api/health").json()["reports"] == 2


def test_admin_reset_reseeds(app_env: Any, fake_storm: type[FakeStorm], monkeypatch: pytest.MonkeyPatch,
                             published: list[dict]) -> None:
    app_env.seed_audio_dir.mkdir()
    (app_env.seed_audio_dir / "seed_01_ar.mp3").write_bytes(b"ID3-fake-mp3")
    monkeypatch.setattr(seed_data, "seed_rows", _fake_seed_rows)
    with TestClient(main.app) as client:
        post_voice(client)
        post_text(client)
        old_upload = db.get_report(1)["audio_path"]
        assert (config.UPLOAD_DIR / old_upload).is_file()
        cache = config.GEOCODE_CACHE_PATH
        cache.write_text("{}", encoding="utf-8")

        response = client.post("/api/admin/reset")
        assert response.json() == {"ok": True, "count": 2}
        assert published[-1] == {"type": "reset"}
        assert fake_storm.instances[-1].stop_calls == 1
        assert not (config.UPLOAD_DIR / old_upload).exists()
        assert cache.exists()  # the geocode cache survives a reset
        reports = client.get("/api/reports").json()
        assert sorted(r["id"] for r in reports) == [1, 2]  # ids restart
        assert all(r["is_simulated"] for r in reports)
        assert client.get("/api/reports/1/audio").content == b"ID3-fake-mp3"


def test_pending_reports_from_a_previous_run_are_resumed(app_env: Any) -> None:
    db.insert_report({"input_type": "text", "transcript_original": "Basement flooding", "ai_status": "pending"})
    db.insert_report({"input_type": "voice", "ai_status": "pending", "is_simulated": True, "location_source": "storm"})
    with TestClient(main.app) as client:
        real = wait_for_report(client, 1, lambda r: r["ai_status"] != "pending")
        assert real["ai_status"] == "failed" and real["ai_engine"] == "rules"
        simulated = client.get("/api/reports/2").json()
        assert simulated["ai_status"] == "failed" and simulated["ai_error"] == "interrupted by restart"


# ---------------------------------------------------------------- SSE


def _sse_scope() -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/events",
        "raw_path": b"/api/events",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"accept", b"text/event-stream")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


async def _drive_sse(on_chunk: Any, timeout: float = 5.0) -> list[dict]:
    """Run GET /api/events at the ASGI level (TestClient buffers whole bodies, so it cannot read an
    endless stream). on_chunk(text) returns True to disconnect. Returns the ASGI messages sent."""
    sent: list[dict] = []
    disconnect = asyncio.Event()
    request_done = False

    async def receive() -> dict:
        nonlocal request_done
        if not request_done:
            request_done = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            if on_chunk(message["body"].decode("utf-8")):
                disconnect.set()

    main._stopping.clear()  # no lifespan runs here; an earlier test's shutdown may have set it
    await asyncio.wait_for(main.app(_sse_scope(), receive, send), timeout=timeout)
    return sent


def test_sse_sends_hello_then_stops_on_disconnect(app_env: Any) -> None:
    chunks: list[str] = []

    def on_chunk(text: str) -> bool:
        chunks.append(text)
        return True  # hang up right after the first event

    sent = asyncio.run(_drive_sse(on_chunk))
    start = sent[0]
    assert start["status"] == 200
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-cache"
    assert headers["x-accel-buffering"] == "no"
    assert chunks[0].startswith("data: ") and chunks[0].endswith("\n\n")
    hello = json.loads(chunks[0][len("data: "):])
    assert hello["type"] == "hello" and hello["server_time"].endswith("Z")
    assert broker.subscriber_count == 0  # the generator cleaned up after the disconnect


def test_sse_delivers_published_events(app_env: Any) -> None:
    chunks: list[str] = []

    def on_chunk(text: str) -> bool:
        chunks.append(text)
        if '"hello"' in text:
            broker.publish({"type": "report.updated", "report": {"id": 1, "transcript_original": "المية"}})
            return False
        return True

    asyncio.run(_drive_sse(on_chunk))
    event = json.loads(chunks[1][len("data: "):])
    assert event["type"] == "report.updated"
    assert event["report"]["transcript_original"] == "المية"  # UTF-8 survives
    assert broker.subscriber_count == 0


def test_sse_heartbeat(app_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "SSE_HEARTBEAT_S", 0.05)
    chunks: list[str] = []

    def on_chunk(text: str) -> bool:
        chunks.append(text)
        return text.startswith(":")

    asyncio.run(_drive_sse(on_chunk))
    assert chunks[-1] == ": ping\n\n"


def test_sse_stream_ends_when_the_server_stops(app_env: Any) -> None:
    def on_chunk(text: str) -> bool:
        if '"hello"' in text:
            main._stopping.set()  # what the Ctrl+C hook does; the client never disconnects
        return False

    sent = asyncio.run(_drive_sse(on_chunk, timeout=3))
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert broker.subscriber_count == 0
    main._stopping.clear()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_sse_over_a_real_server_reads_hello_and_shuts_down(app_env: Any) -> None:
    """Real uvicorn + real socket: read the hello with a bounded read, hang up, and the server stops promptly."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning",
                                           timeout_graceful_shutdown=3))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.05)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as http:
            with http.stream("GET", "/api/events") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                first = next(line for line in response.iter_lines() if line.startswith("data: "))
            assert json.loads(first[len("data: "):])["type"] == "hello"
            assert http.get("/api/health").json()["ok"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive(), "uvicorn hung on shutdown"
