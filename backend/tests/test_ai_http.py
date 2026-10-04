"""The real google-genai SDK against a fake HTTP server: what goes over the wire, and how real error
bodies are handled. httpx.MockTransport stands in for generativelanguage.googleapis.com, so nothing
leaves the machine even if a real GEMINI_API_KEY is in .env.

Checked against ai.google.dev (Oct 2026): generateContent takes contents[].parts[] with inlineData
{mimeType, data(base64)}, systemInstruction, and generationConfig {responseMimeType, responseJsonSchema,
temperature, maxOutputTokens, thinkingConfig {thinkingBudget | thinkingLevel}}. The SDK writes the
nested inlineData/thinkingConfig keys in snake_case (mime_type, thinking_budget); the API's proto-JSON
parser accepts both spellings, and this is what the SDK sends for everyone.
"""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from google import genai
from google.genai import types

from app import ai, config

GOOD = {
    "transcript_original": "جدتي بالبيسمنت والمي وصلت لركبها وعم تطلع",
    "language": "ar",
    "transcript_english": "My grandmother is in the basement, the water reached her knees and keeps rising",
    "ai_summary": "Elderly woman trapped in basement, knee-deep water rising",
    "confirmation_message": "وصلنا بلاغك: جدتك بالقبو والمي للركبة وعم تطلع. إذا في خطر على الحياة، اتصل بـ 911.",
    "water_depth_cm": 50, "location_type": "basement", "location_hint": "Warren Ave and Schaefer Rd",
    "water_in_living_space": True, "water_rising": True,
    "hazards": {"electrical": False, "sewage": False, "gas": False, "structural": False},
    "people_at_risk": {"elderly": True, "children": False, "disabled": False, "medical": False, "trapped": True,
                       "count": 1},
    "needs": {"evacuation": True, "pumping": False, "medical": False, "supplies": False},
}

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
CHAIN = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]


def ok_body(text: str) -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}], "role": "model"}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 300, "totalTokenCount": 1200},
        "modelVersion": "fake",
    }


def error_body(code: int, status: str, message: str, details: list | None = None) -> dict:
    body: dict = {"error": {"code": code, "message": message, "status": status}}
    if details:
        body["error"]["details"] = details
    return body


# Error bodies the way the Gemini API sends them.
NOT_FOUND = error_body(404, "NOT_FOUND", "models/gemini-2.5-flash is not found for API version v1beta, or is not "
                       "supported for generateContent. Call ListModels to see the list of available models.")
PERMISSION = error_body(403, "PERMISSION_DENIED", "Your API key does not have permission for this resource.")
KEY_INVALID = error_body(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.", [
    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID", "domain": "googleapis.com",
     "metadata": {"service": "generativelanguage.googleapis.com"}},
])
QUOTA = error_body(429, "RESOURCE_EXHAUSTED", "You exceeded your current quota, please check your plan and billing "
                   "details.", [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
         "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"},
])
NO_FREE_TIER = error_body(429, "RESOURCE_EXHAUSTED", "Quota exceeded for metric: generativelanguage.googleapis.com/"
                          "generate_content_free_tier_requests, limit: 0, model: gemini-2.5-flash")
OVERLOADED = error_body(503, "UNAVAILABLE", "The model is overloaded. Please try again later.")
INTERNAL = error_body(500, "INTERNAL", "An internal error has occurred.")
THINKING_MINIMAL = error_body(400, "INVALID_ARGUMENT", "Thinking level MINIMAL is not supported for this model.")


class FakeGemini:
    """Answers per model from a script of (status, body) pairs and records every request."""

    def __init__(self, script: dict[str, list[tuple[int, dict]]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.requests: list[tuple[str, dict, httpx.Headers]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        self.requests.append((model, json.loads(request.content), request.headers))
        status, body = self.script[model].pop(0)
        return httpx.Response(status, json=body)

    @property
    def models(self) -> list[str]:
        return [m for m, _, _ in self.requests]


_ROUTER: dict = {}
_CLIENT: list = []


def _shared_client():
    """One real SDK client for the module (building one takes ~1 s); requests go to the current fake."""
    if not _CLIENT:
        transport = httpx.MockTransport(lambda request: _ROUTER["handler"](request))
        _CLIENT.append(genai.Client(api_key="test-key",
                                    http_options=types.HttpOptions(async_client_args={"transport": transport})))
    return _CLIENT[0]


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "GEMINI_MODEL", CHAIN[0])
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", CHAIN[1:])
    monkeypatch.setattr(config, "AI_TIMEOUT_S", 5.0)
    for name, value in (("_last_model", None), ("_unavailable", set()), ("_cooldown", {}), ("_thinking_step", {}),
                        ("_state_key", "test-key")):
        monkeypatch.setattr(ai, name, value)

    def install(script):
        fake = FakeGemini(script)
        _ROUTER["handler"] = fake
        client = _shared_client()
        monkeypatch.setattr(ai, "_get_client", lambda: client)
        return fake

    return install


def extract(**overrides):
    kwargs = dict(audio=WAV, audio_mime="audio/x-wav", text="We are near Warren", photo=JPEG, photo_mime="image/jpg",
                  ui_language="ar")
    kwargs.update(overrides)
    return asyncio.run(ai.extract_report(**kwargs))


def _get(d: dict, camel: str, snake: str):
    return d[camel] if camel in d else d[snake]


@pytest.mark.parametrize("model", CHAIN)
def test_request_payload_per_model(server, monkeypatch, model):
    monkeypatch.setattr(config, "GEMINI_MODEL", model)
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", [])
    fake = server({model: [(200, ok_body(json.dumps(GOOD)))]})
    extraction, engine, _ = extract()
    assert engine == model and extraction.people_at_risk.trapped is True

    (sent_model, body, headers), = fake.requests
    assert sent_model == model
    assert headers["x-goog-api-key"] == "test-key"
    assert set(body) == {"contents", "systemInstruction", "generationConfig"}

    # One user turn: audio inline, photo inline, then the instruction text with the typed words.
    (turn,) = body["contents"]
    assert turn["role"] == "user"
    audio, photo, text = turn["parts"]
    assert _get(audio["inlineData"], "mimeType", "mime_type") == "audio/wav"
    assert base64.urlsafe_b64decode(audio["inlineData"]["data"]) == WAV
    assert _get(photo["inlineData"], "mimeType", "mime_type") == "image/jpeg"
    assert base64.urlsafe_b64decode(photo["inlineData"]["data"]) == JPEG
    assert "We are near Warren" in text["text"] and "Arabic" in text["text"]
    assert body["systemInstruction"]["parts"][0]["text"] == ai.SYSTEM_PROMPT

    gen = body["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert gen["responseJsonSchema"] == ai._RESPONSE_SCHEMA
    assert "responseSchema" not in gen  # never both schema fields
    assert gen["maxOutputTokens"] == 4096
    thinking = gen["thinkingConfig"]
    assert len(thinking) == 1  # thinkingBudget and thinkingLevel together are a 400
    if model.startswith("gemini-2.5"):
        assert gen["temperature"] == 0.2
        assert _get(thinking, "thinkingBudget", "thinking_budget") == 0
    else:
        # gemini-flash-latest is the newest Flash (3.8 today): no "minimal", keep temperature at its default.
        assert "temperature" not in gen
        assert _get(thinking, "thinkingLevel", "thinking_level") == "LOW"


def test_schema_uses_only_keywords_gemini_supports():
    allowed = {"type", "properties", "required", "description", "enum", "items"}

    def walk(node):
        assert set(node) <= allowed, set(node) - allowed
        types_ = node["type"] if isinstance(node["type"], list) else [node["type"]]
        assert set(types_) <= {"string", "integer", "number", "boolean", "object", "array", "null"}
        for child in node.get("properties", {}).values():
            walk(child)
        if node.get("type") == "object":
            assert set(node["required"]) == set(node["properties"])

    walk(ai._RESPONSE_SCHEMA)


def test_404_and_403_move_on_and_are_remembered(server):
    fake = server({
        "gemini-2.5-flash": [(404, NOT_FOUND)],
        "gemini-2.5-flash-lite": [(403, PERMISSION)],
        "gemini-flash-latest": [(200, ok_body(json.dumps(GOOD))), (200, ok_body(json.dumps(GOOD)))],
    })
    _, engine, _ = extract()
    assert engine == "gemini-flash-latest"
    assert fake.models == CHAIN
    # Second report: straight to the model that works, no wasted round trips.
    _, engine, _ = extract()
    assert engine == "gemini-flash-latest"
    assert fake.models == CHAIN + ["gemini-flash-latest"]
    assert ai.active_model() == "gemini-flash-latest"


def test_429_limit_zero_means_no_access(server):
    fake = server({"gemini-2.5-flash": [(429, NO_FREE_TIER)], "gemini-2.5-flash-lite": [(200, ok_body(json.dumps(GOOD)))]})
    _, engine, _ = extract()
    assert engine == "gemini-2.5-flash-lite"
    assert "gemini-2.5-flash" in ai._unavailable
    assert fake.models == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


def test_429_cools_down_for_the_retry_delay(server):
    server({"gemini-2.5-flash": [(429, QUOTA)], "gemini-2.5-flash-lite": [(200, ok_body(json.dumps(GOOD)))]})
    _, engine, _ = extract()
    assert engine == "gemini-2.5-flash-lite"
    assert "gemini-2.5-flash" not in ai._unavailable
    import time

    left = ai._cooldown["gemini-2.5-flash"] - time.monotonic()
    assert 30 < left <= 37
    assert ai.model_chain()[0] == "gemini-2.5-flash-lite"


@pytest.mark.parametrize("status,body", [(500, INTERNAL), (503, OVERLOADED)])
def test_server_errors_move_on_without_sdk_retries(server, status, body):
    fake = server({"gemini-2.5-flash": [(status, body)], "gemini-2.5-flash-lite": [(200, ok_body(json.dumps(GOOD)))]})
    _, engine, _ = extract()
    assert engine == "gemini-2.5-flash-lite"
    # The SDK's own retry is off by default (it would sleep 1-8 s inside our budget).
    assert fake.models == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    assert ai._unavailable == set()


def test_invalid_key_stops_at_once_and_learns_nothing(server):
    fake = server({m: [(400, KEY_INVALID)] for m in CHAIN})
    with pytest.raises(ai.AIError, match="^API key rejected"):
        extract()
    assert fake.models == ["gemini-2.5-flash"]
    assert ai._thinking_step == {} and ai._unavailable == set()


def test_minimal_thinking_rejected_then_low_works(server, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-3.6-flash")
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", [])
    fake = server({"gemini-3.6-flash": [(400, THINKING_MINIMAL), (200, ok_body(json.dumps(GOOD)))]})
    _, engine, _ = extract()
    assert engine == "gemini-3.6-flash"
    levels = [_get(b["generationConfig"]["thinkingConfig"], "thinkingLevel", "thinking_level") for _, b, _ in fake.requests]
    assert levels == ["MINIMAL", "LOW"]


def test_everything_failing_stays_inside_the_budget(server, monkeypatch):
    import time

    monkeypatch.setattr(config, "AI_TIMEOUT_S", 2.0)
    server({"gemini-2.5-flash": [(503, OVERLOADED)], "gemini-2.5-flash-lite": [(429, QUOTA)],
            "gemini-flash-latest": [(404, NOT_FOUND)]})
    started = time.monotonic()
    with pytest.raises(ai.AIError, match="^quota$"):
        extract()
    assert time.monotonic() - started < 2.0


def test_briefing_text_call_payload(server):
    fake = server({"gemini-2.5-flash": [(200, ok_body("  Worst is #2 at Schaefer Rd.  "))]})
    text, model = asyncio.run(ai.generate_text("counts...", system_instruction="sitrep", timeout_s=10))
    assert (text, model) == ("Worst is #2 at Schaefer Rd.", "gemini-2.5-flash")
    (_, body, _), = fake.requests
    assert body["contents"][0]["parts"] == [{"text": "counts..."}]
    gen = body["generationConfig"]
    assert "responseMimeType" not in gen and "responseJsonSchema" not in gen
    assert _get(gen["thinkingConfig"], "thinkingBudget", "thinking_budget") == 0
