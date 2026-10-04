"""Gemini step with a fake client: model chain, thinking retry, timeout, parsing. No network."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from google.genai import errors

from app import ai, config

GOOD = {
    "transcript_original": "  جدتي بالبيسمنت والمي وصلت لركبها  ",
    "language": "AR-lb",
    "transcript_english": "My grandmother is in the basement and the water reached her knees",
    "ai_summary": "Elderly woman in basement, knee-deep water",
    "confirmation_message": "وصلنا بلاغك. إذا في خطر على الحياة، اتصل بـ 911.",
    "water_depth_cm": 50,
    "location_type": "basement",
    "location_hint": "Warren Ave and Schaefer Rd",
    "water_in_living_space": True,
    "water_rising": True,
    "hazards": {"electrical": False, "sewage": False, "gas": False, "structural": False},
    "people_at_risk": {"elderly": True, "children": False, "disabled": False, "medical": False, "trapped": True, "count": 1},
    "needs": {"evacuation": True, "pumping": True, "medical": False, "supplies": False},
}


def api_error(code: int, status: str, message: str = "boom", details: list | None = None) -> errors.APIError:
    """A real SDK exception, built the way google-genai builds it from the error JSON body."""
    cls = errors.ServerError if code >= 500 else errors.ClientError
    body = {"error": {"code": code, "status": status, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return cls(code, body)


class FakeModels:
    """Plays back a script per model: an exception to raise, a string to return, or a coroutine function."""

    def __init__(self, script: dict[str, list]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, object]] = []

    async def generate_content(self, *, model, contents, config):  # noqa: A002 - SDK's argument name
        self.calls.append((model, config))
        outcome = self.script[model].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome()
        return SimpleNamespace(text=outcome)


@pytest.fixture
def fake(monkeypatch):
    """Configure a key, a 3-model chain and a fake client; reset the module's memory between tests."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", ["gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-2.5-flash"])
    monkeypatch.setattr(config, "AI_TIMEOUT_S", 5.0)
    monkeypatch.setattr(ai, "_last_model", None)
    monkeypatch.setattr(ai, "_unavailable", set())
    monkeypatch.setattr(ai, "_cooldown", {})
    monkeypatch.setattr(ai, "_thinking_step", {})
    monkeypatch.setattr(ai, "_state_key", "test-key")

    holder = {}

    def install(script):
        models = FakeModels(script)
        holder["models"] = models
        monkeypatch.setattr(ai, "_get_client", lambda: SimpleNamespace(aio=SimpleNamespace(models=models)))
        return models

    return install


def run_extract(**overrides):
    kwargs = dict(audio=b"RIFF....WAVEfmt ", audio_mime="audio/x-wav", text=None, photo=None, photo_mime=None,
                  ui_language="ar")
    kwargs.update(overrides)
    return asyncio.run(ai.extract_report(**kwargs))


def test_model_chain_is_deduplicated(fake):
    assert ai.model_chain() == ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]


def test_chain_moves_on_for_404_and_429_and_remembers_the_answering_model(fake):
    models = fake({
        "gemini-2.5-flash": [api_error(404, "NOT_FOUND")],
        "gemini-2.5-flash-lite": [api_error(429, "RESOURCE_EXHAUSTED")],
        "gemini-flash-latest": [json.dumps(GOOD)],
    })
    extraction, engine, latency_ms = run_extract()
    assert engine == "gemini-flash-latest"
    assert [m for m, _ in models.calls] == ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]
    assert ai.active_model() == "gemini-flash-latest"
    assert extraction.people_at_risk.trapped is True
    assert latency_ms >= 0
    # The 404 model is skipped from now on; the 429 one sits out its cooldown (quota comes back).
    assert ai.model_chain() == ["gemini-flash-latest"]


def test_chain_moves_on_for_503(fake):
    fake({
        "gemini-2.5-flash": [api_error(503, "UNAVAILABLE")],
        "gemini-2.5-flash-lite": [json.dumps(GOOD)],
    })
    _, engine, _ = run_extract()
    assert engine == "gemini-2.5-flash-lite"


def test_all_models_out_of_quota_raises_quota(fake):
    fake({m: [api_error(429, "RESOURCE_EXHAUSTED")] for m in ai.model_chain()})
    with pytest.raises(ai.AIError, match="^quota$"):
        run_extract()


def test_all_models_missing_raises_model_unavailable(fake):
    fake({m: [api_error(404, "NOT_FOUND")] for m in ai.model_chain()})
    with pytest.raises(ai.AIError, match="^model unavailable$"):
        run_extract()


def test_thinking_config_rejection_moves_to_the_next_setting(fake):
    models = fake({"gemini-2.5-flash": [api_error(400, "INVALID_ARGUMENT", "thinking budget not supported"),
                                         json.dumps(GOOD)]})
    _, engine, _ = run_extract()
    assert engine == "gemini-2.5-flash"
    first, second = models.calls[0][1], models.calls[1][1]
    assert first.thinking_config.thinking_budget == 0
    assert second.thinking_config.thinking_level.value == "LOW" and second.thinking_config.thinking_budget is None
    # Remembered: the next call starts with the setting that worked.
    assert ai._thinking_config("gemini-2.5-flash").thinking_level.value == "LOW"


def test_thinking_level_minimal_rejected_by_newest_flash_falls_back(fake, monkeypatch):
    # gemini-3.8-flash: "minimal is not supported and returns an error" (ai.google.dev, Oct 2026).
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", [])
    models = fake({"gemini-3.5-flash": [
        api_error(400, "INVALID_ARGUMENT", "Thinking level MINIMAL is not supported for this model."),
        api_error(400, "INVALID_ARGUMENT", "Thinking level LOW is not supported for this model."),
        json.dumps(GOOD),
    ]})
    _, engine, _ = run_extract()
    assert engine == "gemini-3.5-flash"
    sent = [cfg.thinking_config for _, cfg in models.calls]
    assert sent[0].thinking_level.value == "MINIMAL" and sent[1].thinking_level.value == "LOW" and sent[2] is None


def test_other_400_moves_on_and_reports_the_error(fake):
    models = fake({m: [api_error(400, "INVALID_ARGUMENT", "audio broken")] for m in
                   ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]})
    with pytest.raises(ai.AIError, match=r"^error: 400 audio broken"):
        run_extract()
    # One request per model; "audio broken" is not about thinking, so no thinking retries.
    assert [m for m, _ in models.calls] == ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]
    assert ai._thinking_step == {}


def test_thinking_config_per_model_family():
    assert ai._thinking_config("gemini-2.5-flash").thinking_budget == 0
    assert ai._thinking_config("gemini-2.5-flash-lite").thinking_budget == 0
    assert ai._thinking_config("gemini-3-flash-preview").thinking_level.value == "MINIMAL"
    assert ai._thinking_config("gemini-3.5-flash").thinking_level.value == "MINIMAL"
    assert ai._thinking_config("gemini-3.5-flash-lite").thinking_level.value == "MINIMAL"
    assert ai._thinking_config("gemini-flash-lite-latest").thinking_level.value == "MINIMAL"
    # 3.7/3.8 Flash reject "minimal"; the -latest alias follows the newest Flash.
    assert ai._thinking_config("gemini-3.8-flash").thinking_level.value == "LOW"
    assert ai._thinking_config("gemini-3.7-flash").thinking_level.value == "LOW"
    assert ai._thinking_config("gemini-flash-latest").thinking_level.value == "LOW"
    assert ai._thinking_config("gemini-3.1-pro-preview").thinking_level.value == "LOW"
    assert ai._thinking_config("gemini-2.0-flash") is None
    # Never both fields at once (that is a 400).
    for model in ("gemini-2.5-flash", "gemini-3.8-flash", "gemini-flash-latest", "gemini-3.5-flash-lite"):
        for option in ai._thinking_options(model):
            assert option is None or option.thinking_budget is None or option.thinking_level is None


def test_temperature_only_for_gemini_2(fake):
    assert ai._temperature("gemini-2.5-flash") == 0.2
    assert ai._temperature("gemini-flash-latest") is None
    assert ai._temperature("gemini-3.8-flash") is None


def test_timeout_raises_timeout(fake, monkeypatch):
    async def slow():
        await asyncio.sleep(2)
        return SimpleNamespace(text=json.dumps(GOOD))

    fake({"gemini-2.5-flash": [slow]})
    monkeypatch.setattr(config, "AI_TIMEOUT_S", 0.05)
    with pytest.raises(ai.AIError, match="^timeout$"):
        run_extract()


@pytest.mark.parametrize("raw", ["not json at all", "[1, 2]", "", "   "])
def test_bad_output_raises(fake, raw):
    models = fake({m: [raw] for m in ai.model_chain()})
    with pytest.raises(ai.AIError, match="^bad output$"):
        run_extract()
    assert len(models.calls) == 3  # every model got its chance


def test_bad_output_from_one_model_lets_the_next_answer(fake):
    fake({"gemini-2.5-flash": ['{"transcript_original": "cut off'], "gemini-2.5-flash-lite": [json.dumps(GOOD)]})
    extraction, engine, _ = run_extract()
    assert engine == "gemini-2.5-flash-lite" and extraction.people_at_risk.elderly is True


def test_not_configured(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    assert ai.ai_enabled() is False
    assert ai.active_model() is None
    with pytest.raises(ai.AIError, match="^AI not configured$"):
        run_extract()


def test_network_error_moves_on_then_reports_network(fake):
    fake({"gemini-2.5-flash": [ConnectionError("network down")], "gemini-2.5-flash-lite": [json.dumps(GOOD)]})
    _, engine, _ = run_extract()
    assert engine == "gemini-2.5-flash-lite"

    fake({m: [ConnectionError("network down")] for m in ai.model_chain()})
    with pytest.raises(ai.AIError, match=r"^error: network [(]ConnectionError[)]"):
        run_extract()


def test_unexpected_exception_becomes_short_error(fake):
    fake({"gemini-2.5-flash": [ValueError("sdk surprise")]})
    with pytest.raises(ai.AIError, match="^error: ValueError"):
        run_extract()


def test_request_carries_audio_inline_schema_and_typed_text(fake):
    models = fake({"gemini-2.5-flash": [json.dumps(GOOD)]})
    run_extract(text="we are near Warren", photo=b"\xff\xd8jpeg", photo_mime="image/jpg")
    model, cfg = models.calls[0]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema["properties"]["location_type"]["enum"] == ["basement", "home", "street", "car", "other"]
    assert "Dearborn" in cfg.system_instruction


def test_contents_parts():
    parts = ai._build_contents(b"RIFFxxxxWAVE", "audio/x-wav", "typed words", b"jpg", "image/jpg", "es")
    assert parts[0].inline_data.mime_type == "audio/wav"
    assert parts[1].inline_data.mime_type == "image/jpeg"
    assert "Spanish" in parts[-1] and "typed words" in parts[-1]


def test_normalization_clamps_and_cleans():
    data = dict(GOOD, water_depth_cm=900, location_type="House", location_hint="null")
    data["people_at_risk"] = dict(GOOD["people_at_risk"], count=0)
    ex = ai.parse_extraction("```json\n" + json.dumps(data, ensure_ascii=False) + "\n```", ui_language="ar")
    assert ex.water_depth_cm == 500
    assert ex.language == "ar"
    assert ex.transcript_original == "جدتي بالبيسمنت والمي وصلت لركبها"
    assert ex.location_type == "home"
    assert ex.location_hint is None
    assert ex.people_at_risk.count is None

    data = dict(GOOD, water_depth_cm="45.6", language="Spanish", ai_summary="", confirmation_message="",
                transcript_original="el agua sube", transcript_english="the water is rising", hazards="nope")
    ex = ai.parse_extraction(json.dumps(data), ui_language="en")
    assert ex.water_depth_cm == 46
    assert ex.language == "es"
    assert ex.ai_summary == "the water is rising"
    assert "911" in ex.confirmation_message and "Recibimos" in ex.confirmation_message
    assert ex.hazards.electrical is False

    ex = ai.parse_extraction(json.dumps(dict(GOOD, water_depth_cm=-3, language="??")), ui_language="es")
    assert ex.water_depth_cm == 0
    assert ex.language == "ar"  # detected from the Arabic script


def test_audio_mime_aliases():
    assert ai.normalize_audio_mime("audio/x-wav") == "audio/wav"
    assert ai.normalize_audio_mime("audio/wave") == "audio/wav"
    assert ai.normalize_audio_mime("audio/mpeg") == "audio/mp3"
    assert ai.normalize_audio_mime("audio/webm;codecs=opus") == "audio/webm"
    assert ai.normalize_audio_mime("audio/x-m4a") == "audio/mp4"
    assert ai.normalize_audio_mime(None, b"OggS\x00\x02") == "audio/ogg"
    assert ai.normalize_audio_mime("application/octet-stream", b"ID3\x04") == "audio/mp3"
