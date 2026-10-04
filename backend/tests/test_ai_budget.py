"""Model chain under time pressure, its per-process memory, and the startup warm-up. Fake client, no network."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from google.genai import errors

from app import ai, config

GOOD = json.dumps({
    "transcript_original": "Water in the basement up to my knees", "language": "en",
    "transcript_english": "Water in the basement up to my knees", "ai_summary": "Knee-deep basement water",
    "confirmation_message": "We received your report. If a life is in danger, call 911.",
    "water_depth_cm": 50, "location_type": "basement", "location_hint": None,
    "water_in_living_space": False, "water_rising": False,
    "hazards": {"electrical": False, "sewage": False, "gas": False, "structural": False},
    "people_at_risk": {"elderly": False, "children": False, "disabled": False, "medical": False, "trapped": False,
                       "count": None},
    "needs": {"evacuation": False, "pumping": True, "medical": False, "supplies": False},
})
CHAIN = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]


def not_found(model: str) -> errors.ClientError:
    return errors.ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND",
                                              "message": f"models/{model} is not found for API version v1beta"}})


class Fake:
    """Per-model script: exception to raise, "hang" to never answer, or text to return."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, float]] = []

    async def generate_content(self, *, model, contents, config):  # noqa: A002
        self.calls.append((model, time.monotonic()))
        outcome = self.script[model].pop(0) if self.script.get(model) else "hang"
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "hang":
            await asyncio.sleep(60)
        return SimpleNamespace(text=outcome)

    @property
    def models(self):
        return [m for m, _ in self.calls]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "GEMINI_MODEL", CHAIN[0])
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", CHAIN[1:])
    monkeypatch.setattr(config, "AI_TIMEOUT_S", 15.0)
    for name, value in (("_last_model", None), ("_unavailable", set()), ("_cooldown", {}), ("_thinking_step", {}),
                        ("_state_key", "test-key"), ("_warmup_task", None), ("_warmup_started", False)):
        monkeypatch.setattr(ai, name, value)

    def install(script):
        models = Fake(script)
        monkeypatch.setattr(ai, "_get_client", lambda: SimpleNamespace(aio=SimpleNamespace(models=models)))
        return models

    return install


def extract():
    return asyncio.run(ai.extract_report(audio=b"RIFF0000WAVEfmt ", audio_mime="audio/wav", text=None, photo=None,
                                         photo_mime=None, ui_language="en"))


def test_hanging_first_model_leaves_time_for_the_next(fake, monkeypatch):
    monkeypatch.setattr(config, "AI_TIMEOUT_S", 1.5)
    monkeypatch.setattr(ai, "RESERVE_S", 0.5)
    monkeypatch.setattr(ai, "MIN_ATTEMPT_S", 0.1)
    models = fake({"gemini-2.5-flash": ["hang"], "gemini-2.5-flash-lite": [GOOD]})
    started = time.monotonic()
    _, engine, _ = extract()
    elapsed = time.monotonic() - started
    assert engine == "gemini-2.5-flash-lite"
    assert elapsed < 1.5
    # The first model got its share (budget minus the reserve), not the whole budget.
    gap = models.calls[1][1] - models.calls[0][1]
    assert 0.6 < gap < 1.0, gap
    # A slow model is not "unavailable": the next report tries it again.
    assert ai.model_chain() == CHAIN


def test_every_model_hanging_ends_with_timeout_inside_the_budget(fake, monkeypatch):
    monkeypatch.setattr(config, "AI_TIMEOUT_S", 1.2)
    monkeypatch.setattr(ai, "MIN_ATTEMPT_S", 0.1)
    fake({m: ["hang"] for m in CHAIN})
    started = time.monotonic()
    with pytest.raises(ai.AIError, match="^timeout$"):
        extract()
    assert time.monotonic() - started < 1.3


def test_unavailable_models_are_skipped_for_the_rest_of_the_process(fake):
    models = fake({"gemini-2.5-flash": [not_found("gemini-2.5-flash")],
                   "gemini-2.5-flash-lite": [not_found("gemini-2.5-flash-lite")],
                   "gemini-flash-latest": [GOOD, GOOD, GOOD]})
    for _ in range(3):
        assert extract()[1] == "gemini-flash-latest"
    assert models.models == CHAIN + ["gemini-flash-latest"] * 2


def test_when_everything_is_marked_unavailable_we_still_try(fake):
    ai._unavailable.update(CHAIN)
    models = fake({"gemini-2.5-flash": [GOOD]})
    assert extract()[1] == "gemini-2.5-flash"
    assert models.models == ["gemini-2.5-flash"]
    assert "gemini-2.5-flash" not in ai._unavailable  # it answered: back in the chain


def test_new_key_forgets_what_the_old_key_learned(fake, monkeypatch):
    ai._unavailable.update(CHAIN[:2])
    ai._thinking_step["gemini-2.5-flash"] = 1
    assert ai.model_chain() == ["gemini-flash-latest"]
    monkeypatch.setattr(config, "GEMINI_API_KEY", "another-key")
    assert ai.model_chain() == CHAIN
    assert ai._thinking_step == {}


def test_active_model_before_any_answer_skips_known_dead_models(fake):
    assert ai.active_model() == "gemini-2.5-flash"
    ai._unavailable.add("gemini-2.5-flash")
    assert ai.active_model() == "gemini-2.5-flash-lite"


def test_client_is_built_off_the_event_loop(monkeypatch):
    """Importing google.genai and building the client takes 1-3 s; it must not freeze the loop."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(ai, "_client", None)
    monkeypatch.setattr(ai, "_client_key", None)
    import threading

    seen = {}

    def slow_build():
        seen["thread"] = threading.current_thread() is threading.main_thread()
        time.sleep(0.2)
        return "client"

    monkeypatch.setattr(ai, "_get_client", slow_build)

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.02)

        task = asyncio.create_task(ticker())
        client = await ai._client_for_call()
        task.cancel()
        return client, ticks

    client, ticks = asyncio.run(main())
    assert client == "client" and seen["thread"] is False and ticks >= 5


# --- warm-up ------------------------------------------------------------------------------


def test_warm_up_finds_the_working_model_with_one_tiny_call(fake):
    models = fake({"gemini-2.5-flash": [not_found("gemini-2.5-flash"), GOOD],
                   "gemini-2.5-flash-lite": [""]})  # empty text is fine: a 200 is all we need
    assert asyncio.run(ai.warm_up()) == "gemini-2.5-flash-lite"
    assert models.models == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    assert ai.active_model() == "gemini-2.5-flash-lite"
    # The first real report goes straight to the model that works.
    models.script["gemini-2.5-flash-lite"] = [GOOD]
    assert extract()[1] == "gemini-2.5-flash-lite"
    assert models.models[-1] == "gemini-2.5-flash-lite" and models.models.count("gemini-2.5-flash") == 1


def test_warm_up_never_raises(fake):
    fake({m: [errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "q"}})]
          for m in CHAIN})
    assert asyncio.run(ai.warm_up()) is None


def test_warm_up_does_nothing_without_a_key(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    assert asyncio.run(ai.warm_up()) is None


def test_schedule_warm_up_runs_at_most_once_and_never_blocks(fake, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("FLOODLINE_AI_WARMUP", raising=False)
    models = fake({"gemini-2.5-flash": ["hang"]})

    async def startup():
        started = time.monotonic()
        first = ai.schedule_warm_up()
        second = ai.schedule_warm_up()
        returned_in = time.monotonic() - started
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass
        return first, second, returned_in

    first, second, returned_in = asyncio.run(startup())
    assert first is not None and second is None
    assert returned_in < 0.05
    assert len(models.calls) <= 1
    # Still once per process, also from a later event loop.
    assert asyncio.run(_schedule_later()) is None


async def _schedule_later():
    return ai.schedule_warm_up()


def test_schedule_warm_up_is_off_under_pytest_and_when_disabled(fake, monkeypatch):
    models = fake({})
    assert asyncio.run(_schedule_later()) is None  # PYTEST_CURRENT_TEST is set right now
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("FLOODLINE_AI_WARMUP", "0")
    assert asyncio.run(_schedule_later()) is None
    assert models.calls == []
