"""Races and odd inputs that could break the live demo: resets and storms colliding with phone
reports, retry spam, cancelled or crashed AI steps, slow dashboards, and strange uploads.

These drive the real app in-process (lifespan + httpx ASGITransport) so several requests can be
in flight at once on one event loop, which TestClient cannot do. Nothing touches the network.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app import ai, config, db, main, service, storm
from app.events import broker
from conftest import make_extraction

WAV = b"RIFF\x24\x08\x00\x00WAVEfmt " + b"\x00" * 2032


@contextlib.asynccontextmanager
async def running_app() -> AsyncIterator[httpx.AsyncClient]:
    """The app with its lifespan running, plus a client that can send concurrent requests."""
    async with main.lifespan(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
            yield client


def slow_ai(monkeypatch: pytest.MonkeyPatch, delay: float, **extraction: Any) -> dict:
    """AI on, answering after `delay` seconds. Returns a dict that counts calls."""
    calls = {"n": 0}

    async def extract(**kwargs: Any) -> Any:
        calls["n"] += 1
        await asyncio.sleep(delay)
        return make_extraction(**extraction), "gemini-2.5-flash", int(delay * 1000)

    monkeypatch.setattr(ai, "ai_enabled", lambda: True)
    monkeypatch.setattr(ai, "active_model", lambda: "gemini-2.5-flash")
    monkeypatch.setattr(ai, "extract_report", extract)
    return calls


async def wait_until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------- reset vs. in-flight work


def test_reset_while_a_phone_is_waiting_still_answers_the_phone(app_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The phone's POST waits on the AI task; a reset cancels that task. The phone must get a
    normal answer (its report was saved), not a dropped connection."""
    slow_ai(monkeypatch, 3.0)
    monkeypatch.setattr(config, "POST_WAIT_S", 5.0)

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with running_app() as client:
            post = asyncio.create_task(
                client.post("/api/reports", files={"audio": ("v.wav", WAV, "audio/wav")}, data={"ui_language": "ar"})
            )
            await wait_until(lambda: db.count_reports() == 1)
            await asyncio.sleep(0.1)
            reset = await client.post("/api/admin/reset")
            return await asyncio.wait_for(post, 10), reset

    phone, reset = asyncio.run(scenario())
    assert reset.status_code == 200
    assert phone.status_code == 200, phone.text
    assert phone.json()["id"] == 1


def test_reset_during_storm_and_posts_leaves_nothing_pending(app_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    slow_ai(monkeypatch, 0.4)
    monkeypatch.setattr(config, "POST_WAIT_S", 0.1)

    async def scenario() -> list[dict]:
        async with running_app() as client:
            main.app.state.storm.finish_delay_s = (0.3, 0.6)
            await client.post("/api/storm/start", json={"max_reports": 50, "min_interval_s": 0.1, "max_interval_s": 0.1})
            for round_no in range(3):
                posts = [
                    asyncio.create_task(client.post("/api/reports", data={"text": f"water {round_no}-{i}", "lat": "42.31", "lng": "-83.2"}))
                    for i in range(5)
                ]
                await asyncio.sleep(0.25)
                assert (await client.post("/api/admin/reset")).status_code == 200
                for response in await asyncio.gather(*posts):
                    assert response.status_code == 200, response.text
                await client.post("/api/storm/start", json={"max_reports": 50, "min_interval_s": 0.1, "max_interval_s": 0.1})
                await asyncio.sleep(0.2)
            await client.post("/api/storm/stop")
            await asyncio.sleep(1.0)  # let the last AI calls land
            return (await client.get("/api/reports")).json()

    reports = asyncio.run(scenario())
    assert reports, "reset should leave the (empty-seed) DB plus whatever arrived after it"
    stuck = [r["id"] for r in reports if r["ai_status"] == "pending"]
    assert stuck == []


# ---------------------------------------------------------------- storm start/stop spam


def test_storm_start_stop_spam_never_leaves_a_report_processing(app_env: Any) -> None:
    async def scenario() -> tuple[list[dict], dict]:
        async with running_app() as client:
            main.app.state.storm.finish_delay_s = (0.2, 0.5)
            body = {"max_reports": 200, "min_interval_s": 0.1, "max_interval_s": 0.15}
            calls = []
            for _ in range(15):
                calls.append(client.post("/api/storm/start", json=body))
                calls.append(client.post("/api/storm/stop"))
                calls.append(client.post("/api/storm/start", json=body))
            for response in await asyncio.gather(*calls):
                assert response.status_code == 200
            await asyncio.sleep(0.5)
            await client.post("/api/storm/stop")
            state = (await client.get("/api/config")).json()
            return (await client.get("/api/reports")).json(), state

    reports, state = asyncio.run(scenario())
    assert state["storm_running"] is False
    assert [r["id"] for r in reports if r["ai_status"] == "pending"] == []


def test_restarting_storm_while_stop_is_flushing_keeps_new_reports_finishing(app_env: Any) -> None:
    """stop() waits for the old loop; a start() in that window must not have its reports cancelled."""
    async def scenario() -> list[dict]:
        async with running_app() as client:
            controller = main.app.state.storm
            controller.finish_delay_s = (0.3, 0.3)
            body = {"max_reports": 100, "min_interval_s": 0.1, "max_interval_s": 0.1}
            await client.post("/api/storm/start", json=body)
            await asyncio.sleep(0.25)
            stop = asyncio.create_task(client.post("/api/storm/stop"))
            await asyncio.sleep(0)
            await client.post("/api/storm/start", json=body)
            await stop
            await asyncio.sleep(0.6)
            await client.post("/api/storm/stop")
            return (await client.get("/api/reports")).json()

    reports = asyncio.run(scenario())
    assert [r["id"] for r in reports if r["ai_status"] == "pending"] == []


# ---------------------------------------------------------------- AI step cancelled or crashed


def test_reprocess_spam_runs_one_ai_call_at_a_time(app_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = slow_ai(monkeypatch, 0.3)
    monkeypatch.setattr(config, "POST_WAIT_S", 1.0)

    async def scenario() -> dict:
        async with running_app() as client:
            created = (await client.post("/api/reports", files={"audio": ("v.wav", WAV, "audio/wav")})).json()
            assert created["ai_status"] == "done"
            responses = await asyncio.gather(*[client.post(f"/api/reports/{created['id']}/reprocess") for _ in range(30)])
            assert all(r.status_code == 200 for r in responses)
            await asyncio.sleep(0.6)
            return (await client.get(f"/api/reports/{created['id']}")).json()

    final = asyncio.run(scenario())
    assert final["ai_status"] == "done"
    assert calls["n"] == 2  # the POST, then exactly one reprocess


def test_a_cancelled_ai_step_can_be_retried(app_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown cancels running AI steps and leaves those reports "pending" on purpose (the next
    start re-runs them, see test_pending_reports_from_a_previous_run_are_resumed). Within one run,
    "Retry AI" must still work on such a report instead of answering "already running"."""
    calls = slow_ai(monkeypatch, 5.0)
    monkeypatch.setattr(config, "POST_WAIT_S", 0.05)

    async def scenario() -> dict:
        async with running_app() as client:
            created = (await client.post("/api/reports", data={"text": "water in the basement"})).json()
            task = main._enrichments[created["id"]]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            slow_ai(monkeypatch, 0.05)
            retried = (await client.post(f"/api/reports/{created['id']}/reprocess")).json()
            assert retried["ai_status"] == "pending"
            await asyncio.sleep(0.3)
            return (await client.get(f"/api/reports/{created['id']}")).json()

    final = asyncio.run(scenario())
    assert calls["n"] == 1
    assert final["ai_status"] == "done" and final["urgency_level"] is not None


def test_enrichment_crash_marks_failed(app_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "ai_enabled", lambda: True)

    async def boom(**kwargs: Any) -> Any:
        raise ZeroDivisionError("bad")

    monkeypatch.setattr(ai, "extract_report", boom)

    def broken_fallback(text: str, ui_language: str = "en") -> Any:
        raise ValueError("fallback broke too")

    monkeypatch.setattr(service.fallback_rules, "extract_from_text", broken_fallback)

    async def scenario() -> dict:
        async with running_app() as client:
            return (await client.post("/api/reports", data={"text": "help"})).json()

    final = asyncio.run(scenario())
    assert final["ai_status"] == "failed"
    assert final["urgency_level"] == "MEDIUM"


def test_patch_on_a_pending_report_survives_the_ai_result(app_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    slow_ai(monkeypatch, 0.4)
    monkeypatch.setattr(config, "POST_WAIT_S", 0.05)

    async def scenario() -> tuple[dict, dict]:
        async with running_app() as client:
            created = (await client.post("/api/reports", data={"text": "trapped"})).json()
            assert created["ai_status"] == "pending"
            patched = (await client.patch(f"/api/reports/{created['id']}", json={"status": "dispatched"})).json()
            await asyncio.sleep(0.7)
            return patched, (await client.get(f"/api/reports/{created['id']}")).json()

    patched, final = asyncio.run(scenario())
    assert patched["status"] == "dispatched" and patched["urgency_level"] is None
    assert final["status"] == "dispatched" and final["ai_status"] == "done" and final["urgency_level"] == "CRITICAL"


# ---------------------------------------------------------------- live events


def test_sse_connect_disconnect_200_times_leaks_no_subscribers(app_env: Any) -> None:
    async def scenario() -> int:
        async with running_app():
            before = broker.subscriber_count
            response = await main.events()
            for _ in range(200):
                response = await main.events()
                stream = response.body_iterator
                first = await stream.__anext__()
                assert '"hello"' in first
                await stream.aclose()  # what Starlette does when the client hangs up
            assert broker.subscriber_count == before
            return broker.subscriber_count

    assert asyncio.run(scenario()) == 0


def test_a_stalled_dashboard_is_told_to_resync_instead_of_silently_missing_events(app_env: Any) -> None:
    """A subscriber whose queue overflows must not keep a stream that silently lost updates:
    the stream ends, the browser's EventSource reconnects, and the new 'hello' triggers a snapshot."""
    async def scenario() -> list[str]:
        async with running_app():
            response = await main.events()
            stream = response.body_iterator
            chunks = [await stream.__anext__()]  # hello
            for i in range(broker._queue_size + 50):  # the dashboard is not reading meanwhile
                broker.publish({"type": "report.updated", "report": {"id": i}})
            healthy = broker.subscribe()  # a second, live dashboard is unaffected
            broker.publish({"type": "storm.state", "running": True, "injected": 1})
            assert healthy.get_nowait()["type"] == "storm.state"
            broker.unsubscribe(healthy)
            async def drain() -> None:
                async for chunk in stream:
                    chunks.append(chunk)
                    if len(chunks) > broker._queue_size + 100:
                        break

            await asyncio.wait_for(drain(), timeout=5)
            return chunks

    chunks = asyncio.run(scenario())
    assert chunks[-1] == "retry: 1000\n\n", "the stream should end with a reconnect hint"
    assert len(chunks) == 2, "the stale backlog is thrown away; the reconnect reloads everything"
    assert broker.subscriber_count == 0


def test_publish_with_many_stalled_subscribers_stays_fast_and_quiet(app_env: Any, caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> float:
        queues = [broker.subscribe() for _ in range(50)]
        loop = asyncio.get_running_loop()
        started = loop.time()
        for i in range(2000):
            broker.publish({"type": "report.updated", "report": {"id": i}})
        elapsed = loop.time() - started
        for queue in queues:
            broker.unsubscribe(queue)
        return elapsed

    assert asyncio.run(scenario()) < 2.0
    warnings = [r for r in caplog.records if "fell" in r.getMessage()]
    assert len(warnings) == 50  # one line per stalled dashboard, not one per dropped event


# ---------------------------------------------------------------- odd inputs


@pytest.mark.parametrize(
    ("files", "data", "status"),
    [
        ({"audio": ("v.wav", b"", "audio/wav")}, {}, 422),  # 0-byte recording and nothing typed
        ({"audio": ("v.wav", b"", "audio/wav")}, {"text": "  "}, 422),
        ({"audio": ("v.wav", b"", "audio/wav")}, {"text": "basement flooding"}, 200),
        ({}, {"text": "x" * 6000}, 200),
        ({}, {"text": "🌊🌊 المية وصلت للركبة in the basement, help! 🙏"}, 200),
        ({}, {"text": "water", "lat": "NaN", "lng": "-83.2"}, 200),
        ({}, {"text": "water", "lat": "Infinity", "lng": "-Infinity"}, 200),
        ({}, {"text": "water", "lat": "42.31", "lng": "-83.2", "accuracy_m": "-5"}, 200),
        ({}, {"text": "water", "lat": "1e400", "lng": "-83.2"}, 200),
    ],
)
def test_odd_inputs(app_env: Any, files: dict, data: dict, status: int) -> None:
    async def scenario() -> httpx.Response:
        async with running_app() as client:
            return await client.post("/api/reports", files=files or None, data=data)

    response = asyncio.run(scenario())
    assert response.status_code == status, response.text
    if status == 200:
        report = response.json()
        assert report["ai_status"] in ("failed", "done")
        for key in ("lat", "lng", "accuracy_m"):
            assert report[key] is None or math.isfinite(report[key])
        if report["accuracy_m"] is not None:
            assert report["accuracy_m"] >= 0
        if "text" in data and data["text"].strip():
            assert report["transcript_original"] == data["text"].strip()[:5000]


@pytest.mark.parametrize(
    ("filename", "mime", "audio", "want_ext", "want_mime"),
    [
        ("voice-note.wav", "audio/wav", WAV, ".wav", "audio/wav"),
        ("rec.webm", "video/webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 64, ".webm", "audio/webm"),
        ("rec.webm", "audio/webm;codecs=opus", b"\x1a\x45\xdf\xa3" + b"\x00" * 64, ".webm", "audio/webm"),
        ("rec.m4a", "audio/x-m4a", b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64, ".m4a", "audio/mp4"),
        ("rec.mp3", "audio/mp3", b"ID3\x03" + b"\x00" * 64, ".mp3", "audio/mpeg"),
        ("blob", "", WAV, ".wav", "audio/wav"),  # no type, no extension: sniff the bytes
        ("blob", "application/octet-stream", b"OggS" + b"\x00" * 64, ".ogg", "audio/ogg"),
        ("blob", "application/octet-stream", b"\x1a\x45\xdf\xa3" + b"\x00" * 64, ".webm", "audio/webm"),
    ],
)
def test_audio_types_are_stored_playable(app_env: Any, filename: str, mime: str, audio: bytes,
                                         want_ext: str, want_mime: str) -> None:
    async def scenario() -> tuple[dict, dict | None, httpx.Response]:
        async with running_app() as client:
            report = (await client.post("/api/reports", files={"audio": (filename, audio, mime)})).json()
            row = db.get_report(report["id"])
            return report, row, await client.get(report["audio_url"])

    report, row, served = asyncio.run(scenario())
    assert row is not None and row["audio_path"].endswith(want_ext)
    assert row["audio_mime"] == want_mime
    assert served.status_code == 200 and served.headers["content-type"].startswith(want_mime)
    assert served.content == audio


def test_quick_inserts_keep_newest_first_inside_a_level(app_env: Any) -> None:
    """Many reports in the same second: equal level and score must still list newest first,
    the same way the dashboard sorts them (compareReports), or the queue jumps on reload."""
    async def scenario() -> list[dict]:
        async with running_app() as client:
            for i in range(30):
                await client.post("/api/reports", data={"text": f"water report {i}"})
            return (await client.get("/api/reports")).json()

    reports = asyncio.run(scenario())
    stamps = [r["created_at"] for r in reports]
    assert len(set(stamps)) == len(stamps), "created_at must tell quick reports apart"
    ids = [r["id"] for r in reports]
    assert ids == sorted(ids, reverse=True)


def test_twenty_concurrent_posts_while_storm_runs(app_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    slow_ai(monkeypatch, 0.2)

    async def scenario() -> list[dict]:
        async with running_app() as client:
            main.app.state.storm.finish_delay_s = (0.1, 0.3)
            await client.post("/api/storm/start", json={"max_reports": 100, "min_interval_s": 0.1, "max_interval_s": 0.1})
            responses = await asyncio.gather(*[
                client.post("/api/reports", files={"audio": ("v.wav", WAV, "audio/wav")},
                            data={"lat": "42.31", "lng": str(-83.2 + i / 1000), "address_text": "Warren & Schaefer"})
                for i in range(20)
            ])
            assert all(r.status_code == 200 for r in responses), [r.text for r in responses if r.status_code != 200]
            await client.post("/api/storm/stop")
            return (await client.get("/api/reports")).json()

    reports = asyncio.run(scenario())
    assert len([r for r in reports if not r["is_simulated"]]) == 20
    assert [r["id"] for r in reports if r["ai_status"] == "pending"] == []
