"""Gemini step: voice note (+ optional photo or typed text) in, structured Extraction out.

One request per report: the audio goes inline with the instruction prompt, and Gemini answers
with JSON that follows _RESPONSE_SCHEMA. We then validate and normalize it into models.Extraction.

Models are tried in order (config.GEMINI_MODEL, then config.GEMINI_FALLBACK_MODELS) inside one
time budget (config.AI_TIMEOUT_S). What each failure means:

- 404 NOT_FOUND / 403 PERMISSION_DENIED, or 429 with "limit: 0" (no free-tier access at all):
  this key cannot use the model. Remembered for the life of the process; later calls skip it.
  (Google now limits the 2.5 models to projects that used them before, so a new key may get this.)
- 429 RESOURCE_EXHAUSTED: per-minute quota. Skipped until the retry delay Google sends has passed.
- 5xx, 408, network errors, or a model that does not answer in its share of the budget: try the next.
- 400 that names the thinking config: retry the same model with the next thinking setting.
- 400/401/403 about the API key: stop at once ("API key rejected"); every model would say the same.
- Other 400s: try the next model (a model-specific rejection must not cost the report).

No model gets the whole budget while another is still waiting: the first ones leave RESERVE_S
for the rest. Any failure raises AIError with a short reason that service.py stores in `ai_error`;
the report itself is always kept. Every attempt is logged with the model and its latency.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from . import config
from .fallback_rules import LANGS, detect_language
from .models import Extraction

log = logging.getLogger("floodline.ai")


class AIError(Exception):
    """The AI step failed (disabled, timeout, quota, unusable output). Callers keep the report anyway."""


# --- client and model chain ---------------------------------------------------------------

_client: Any = None
_client_key: str | None = None
_last_model: str | None = None  # the model that answered most recently (shown in the UI)
_unavailable: set[str] = set()  # models this key cannot use (404/403/limit 0); skipped while others remain
_cooldown: dict[str, float] = {}  # model -> time.monotonic() until which it is out of quota (429)
_thinking_step: dict[str, int] = {}  # model -> index into _thinking_options() that the model accepts
_state_key: str | None = None  # the API key the memory above was learned with
_warmup_task: asyncio.Task | None = None
_warmup_started = False

# Gemini accepts at most ~20 MB per inline request (prompt + all files). Stay under it.
_MAX_INLINE_BYTES = 19 * 1024 * 1024

# Budget split: while another model is still waiting, an attempt may use what is left minus RESERVE_S
# (at most a third of it), so one hanging model cannot starve the one that works. With the default
# 15 s budget the first model gets ~9.7 s and the next one at least 5 s. MIN_ATTEMPT_S: not worth
# starting a request with less time than this.
RESERVE_S = 5.0
MIN_ATTEMPT_S = 1.0
QUOTA_COOLDOWN_S = 20.0  # 429 without a retry delay: skip the model this long
MAX_COOLDOWN_S = 120.0


def ai_enabled() -> bool:
    """True when a Gemini API key is configured."""
    return bool(config.GEMINI_API_KEY)


def active_model() -> str | None:
    """Model name for the UI: the last model that answered, else the first one we would try. None when AI is off."""
    if not ai_enabled():
        return None
    if _last_model:
        return _last_model
    chain = model_chain()
    return chain[0] if chain else config.GEMINI_MODEL


def _forget_if_key_changed() -> None:
    """What we learned about models belongs to one key; a new key starts fresh."""
    global _state_key, _last_model
    key = config.GEMINI_API_KEY
    if key != _state_key:
        _unavailable.clear()
        _cooldown.clear()
        _thinking_step.clear()
        _last_model = None
        _state_key = key


def _get_client() -> Any:
    """Create the google-genai client on first use (and again if the key changes). Blocking: call via _client_for_call."""
    global _client, _client_key
    key = config.GEMINI_API_KEY
    if not key:
        raise AIError("AI not configured")
    if _client is None or _client_key != key:
        from google import genai  # imported lazily: keeps startup fast and tests independent of it

        _client = genai.Client(api_key=key)
        _client_key = key
    return _client


async def _client_for_call() -> Any:
    """The client, created off the event loop: importing google.genai and building the client takes
    1-3 s on the demo laptop and would otherwise freeze every open dashboard stream meanwhile."""
    if _client is not None and _client_key == config.GEMINI_API_KEY:
        return _get_client()
    return await asyncio.to_thread(_get_client)


def _configured_chain() -> list[str]:
    chain: list[str] = []
    for name in [config.GEMINI_MODEL, *config.GEMINI_FALLBACK_MODELS]:
        name = (name or "").strip()
        if name and name not in chain:
            chain.append(name)
    return chain


def model_chain() -> list[str]:
    """Models to try, in order: configured model first, then the fallbacks, without duplicates.

    Models this key cannot use (404/403) are left out, and so are models still cooling down after a
    429. If that would leave nothing, everything is tried anyway (cooling-down models first) so a
    hiccup can never lock the AI out for good.
    """
    _forget_if_key_changed()
    chain = _configured_chain()
    now = time.monotonic()
    ready = [m for m in chain if m not in _unavailable and _cooldown.get(m, 0) <= now]
    if ready:
        return ready
    cooling = [m for m in chain if m not in _unavailable]
    return cooling + [m for m in chain if m in _unavailable]


def _version(name: str) -> float | None:
    match = re.match(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(match.group(1)) if match else None


def _thinking_options(model: str) -> list[Any]:
    """Thinking settings to try for a model, fastest first; None means "send no thinking config".

    Thinking costs seconds we do not have (checked against ai.google.dev, Oct 2026):
    - gemini-2.5 Flash / Flash-Lite: thinking_budget=0 switches it off.
    - Gemini 3 Flash models up to 3.6 and every Flash-Lite accept thinking_level "minimal".
    - gemini-3.7-flash and gemini-3.8-flash reject "minimal" with a 400 ("minimal is not supported
      and returns an error"); "low" is their fastest setting. gemini-flash-latest points at the newest
      Flash, so it gets "low". Pro models: "low" (no minimal, and 2.5 Pro cannot turn thinking off).
    A model that rejects a setting moves on to the next one (remembered in _thinking_step).
    Sending thinking_budget and thinking_level together is a 400, so we only ever send one.
    """
    from google.genai import types

    name = model.lower()
    minimal = types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)
    low = types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
    version = _version(name)
    if "flash" not in name and "pro" not in name:
        return [None]
    if version is not None and version < 2.5:
        return [None]  # 2.0 and older have no thinking
    if "pro" in name:
        return [low, None]
    if version is not None and version < 3:
        return [types.ThinkingConfig(thinking_budget=0), low, None]
    if "lite" in name or (version is not None and version < 3.7):
        return [minimal, low, None]
    return [low, None]  # gemini-3.7+/3.8 flash, gemini-flash-latest and unknown newer Flash models


def _thinking_config(model: str) -> Any:
    """The thinking config the next request to `model` will carry (None: none)."""
    options = _thinking_options(model)
    return options[min(_thinking_step.get(model, 0), len(options) - 1)]


def _temperature(model: str) -> float | None:
    """Low temperature keeps 2.x transcripts verbatim. Gemini 3 docs: keep the default 1.0 (lower can loop)."""
    version = _version(model.lower())
    return 0.2 if version is not None and version < 3 else None


def _short(text: Any, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --- error classification -----------------------------------------------------------------

_STATUS_CODES = {
    "INVALID_ARGUMENT": 400, "FAILED_PRECONDITION": 400, "UNAUTHENTICATED": 401, "PERMISSION_DENIED": 403,
    "NOT_FOUND": 404, "RESOURCE_EXHAUSTED": 429, "INTERNAL": 500, "UNAVAILABLE": 503, "DEADLINE_EXCEEDED": 504,
}
# Problems with the key itself (every model would answer the same). Deliberately narrow: a 403
# "API key does not have permission for this resource" is about one model and must move on.
_KEY_PROBLEM = re.compile(
    r"API_KEY_INVALID|API key not valid|API key expired|API_KEY_EXPIRED|reported as leaked|SERVICE_DISABLED|"
    r"has not been used in project|CONSUMER_SUSPENDED",
    re.IGNORECASE,
)
_THINKING_PROBLEM = re.compile(r"think|budget", re.IGNORECASE)


def _error_code(exc: Any) -> int:
    code = getattr(exc, "code", None)
    try:
        return int(code)
    except (TypeError, ValueError):
        return _STATUS_CODES.get(str(getattr(exc, "status", "") or "").upper(), 0)


def _error_text(exc: Any) -> str:
    """Message plus details: the reason codes (API_KEY_INVALID, RetryInfo) live in the details."""
    parts = [str(getattr(exc, "message", "") or ""), str(getattr(exc, "status", "") or "")]
    try:
        parts.append(json.dumps(getattr(exc, "details", None), default=str))
    except (TypeError, ValueError):
        pass
    return " ".join(parts)


def _retry_delay_s(exc: Any) -> float:
    """RetryInfo.retryDelay ("37s") from a 429, else QUOTA_COOLDOWN_S."""
    match = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', _error_text(exc))
    delay = float(match.group(1)) if match else QUOTA_COOLDOWN_S
    return max(1.0, min(MAX_COOLDOWN_S, delay))


def _is_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, OSError)):
        return True
    module = type(exc).__module__ or ""
    return module.startswith(("aiohttp", "httpx", "httpcore", "ssl"))


# --- the call -----------------------------------------------------------------------------


def _build_config(model: str, system_instruction: str, json_schema: dict | None, max_output_tokens: int) -> Any:
    from google.genai import types

    cfg: dict[str, Any] = {"system_instruction": system_instruction, "max_output_tokens": max_output_tokens}
    if json_schema is not None:
        # responseJsonSchema (plain JSON Schema, type arrays for null) is supported by 2.5 and 3.x.
        cfg["response_mime_type"] = "application/json"
        cfg["response_json_schema"] = json_schema
    temperature = _temperature(model)
    if temperature is not None:
        cfg["temperature"] = temperature
    thinking = _thinking_config(model)
    if thinking is not None:
        cfg["thinking_config"] = thinking
    return types.GenerateContentConfig(**cfg)


async def _generate(
    contents: list[Any],
    *,
    system_instruction: str,
    json_schema: dict | None = None,
    max_output_tokens: int = 2048,
    budget_s: float | None = None,
    accept: Callable[[str], Any] | None = None,
    allow_empty: bool = False,
) -> tuple[Any, str]:
    """Run one prompt through the model chain within budget_s seconds.

    Returns (result, model) where result is accept(text) when `accept` is given, else the text.
    `accept` may raise AIError("bad output"); the next model then gets a chance if time is left.
    """
    global _last_model
    from google.genai import errors

    budget = config.AI_TIMEOUT_S if budget_s is None else budget_s
    deadline = time.monotonic() + budget  # set before the client exists: building it can take seconds
    client = await _client_for_call()
    reasons: list[str] = []
    chain = model_chain()
    i = 0
    while i < len(chain):
        model = chain[i]
        remaining = deadline - time.monotonic()
        if remaining < MIN_ATTEMPT_S:
            reasons.append("timeout")
            break
        # While another model is still waiting, leave it a share of the budget (RESERVE_S, at most
        # a third of what is left): a hanging model must not starve the one that works.
        reserve = min(RESERVE_S, remaining / 3) if i < len(chain) - 1 else 0.0
        cap = remaining - reserve if remaining - reserve >= MIN_ATTEMPT_S else remaining
        thinking = _thinking_config(model)
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model, contents=contents,
                    config=_build_config(model, system_instruction, json_schema, max_output_tokens),
                ),
                timeout=cap,
            )
        except errors.APIError as exc:
            code = _error_code(exc)
            detail = _short(getattr(exc, "message", None) or exc)
            text = _error_text(exc)
            ms = int((time.monotonic() - started) * 1000)
            if code == 401 or (code in (400, 403) and _KEY_PROBLEM.search(text)):
                log.error("gemini model=%s: API key rejected (%s %s, %d ms)", model, code, detail, ms)
                raise AIError(f"API key rejected: {_short(detail, 60)}") from exc
            if code == 400 and thinking is not None and _THINKING_PROBLEM.search(text):
                _thinking_step[model] = _thinking_step.get(model, 0) + 1
                log.warning("gemini model=%s rejected thinking config %s (%s); retrying with %s",
                            model, _describe_thinking(thinking), detail, _describe_thinking(_thinking_config(model)))
                continue  # same model, next thinking setting
            if code in (403, 404) or (code == 429 and re.search(r"limit:\s*0\b", text)):
                _unavailable.add(model)
                log.warning("gemini model=%s unavailable to this key (%s %s, %d ms); skipping it from now on",
                            model, code, detail, ms)
                reasons.append("model unavailable")
            elif code == 429:
                delay = _retry_delay_s(exc)
                _cooldown[model] = time.monotonic() + delay
                log.warning("gemini model=%s out of quota (%s, %d ms); skipping it for %.0f s", model, detail, ms, delay)
                reasons.append("quota")
            elif code >= 500 or code == 408:
                log.warning("gemini model=%s server error %s (%s, %d ms); trying next", model, code, detail, ms)
                reasons.append("model unavailable")
            else:
                log.warning("gemini model=%s refused the request (%s %s, %d ms); trying next", model, code, detail, ms)
                reasons.append(f"error: {code} {detail}".strip())
            i += 1
            continue
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("gemini model=%s did not answer within %.1f s; trying next", model, cap)
            reasons.append("timeout")
            i += 1
            continue
        except Exception as exc:  # noqa: BLE001 - network trouble: the next model may still get through
            if not _is_network_error(exc):
                raise
            log.warning("gemini model=%s network error %s: %s", model, type(exc).__name__, _short(exc))
            reasons.append(f"error: network ({type(exc).__name__})")
            i += 1
            continue

        ms = int((time.monotonic() - started) * 1000)
        _unavailable.discard(model)
        _cooldown.pop(model, None)
        try:
            text = _response_text(response, allow_empty=allow_empty)
            result = accept(text) if accept is not None else text
        except AIError as exc:
            log.warning("gemini model=%s answered in %d ms but the output was unusable (%s)", model, ms, exc)
            reasons.append(str(exc))
            i += 1
            continue
        _last_model = model
        log.info("gemini answered: model=%s latency_ms=%d thinking=%s", model, ms, _describe_thinking(thinking))
        return result, model
    raise AIError(_pick_reason(reasons))


def _pick_reason(reasons: list[str]) -> str:
    """The most useful single reason for the operator when every model failed."""
    for wanted in ("quota", "bad output", "timeout"):
        if wanted in reasons:
            return wanted
    errors_ = [r for r in reasons if r.startswith("error:")]
    if errors_:
        return errors_[0]
    return "model unavailable"


def _describe_thinking(thinking: Any) -> str:
    if thinking is None:
        return "default"
    if getattr(thinking, "thinking_level", None) is not None:
        return f"level={thinking.thinking_level.value.lower()}"
    return f"budget={thinking.thinking_budget}"


def _response_text(response: Any, *, allow_empty: bool = False) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - odd responses (blocked, no candidates) are just "bad output"
        text = None
    if not text or not str(text).strip():
        if allow_empty:
            return ""
        raise AIError("bad output")
    return str(text)


# --- warm-up ------------------------------------------------------------------------------


async def warm_up() -> str | None:
    """One tiny text-only call through the model chain, so the first real report is fast.

    It imports the SDK and builds the client off the event loop, opens the HTTPS connection, and
    finds out which models this key can use (a 404/403 model is skipped from then on), so the
    first judge's voice note goes straight to a model that answers. Returns the model or None.
    Never raises.
    """
    if not ai_enabled():
        return None
    started = time.perf_counter()
    try:
        _, model = await _generate(
            ["Reply with the single word OK."],
            system_instruction="Health check. Answer with one word.",
            max_output_tokens=32,
            budget_s=config.AI_TIMEOUT_S,
            allow_empty=True,  # a 200 is all we need; tiny limits can end before any text
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("gemini warm-up failed after %d ms: %s (reports will still try every model)",
                    int((time.perf_counter() - started) * 1000), exc)
        return None
    log.info("gemini warm-up: %s ready in %d ms (unavailable to this key: %s)", model,
             int((time.perf_counter() - started) * 1000), ", ".join(sorted(_unavailable)) or "none")
    return model


def schedule_warm_up() -> asyncio.Task | None:
    """Start warm_up() in the background, at most once per process. Call from the running event loop
    (the app's startup); it returns at once and never delays startup.

    Skipped when AI is off, under pytest (tests must stay hermetic even if a real key is in .env),
    or with FLOODLINE_AI_WARMUP=0.
    """
    global _warmup_task, _warmup_started
    if _warmup_started or not ai_enabled():
        return None
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("FLOODLINE_AI_WARMUP", "1").strip() == "0":
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    _warmup_started = True
    _warmup_task = loop.create_task(warm_up(), name="gemini-warm-up")
    return _warmup_task


async def stop_warm_up() -> None:
    """Cancel a warm-up still running at shutdown (avoids "Task was destroyed but it is pending")."""
    task = _warmup_task
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down anyway
        pass


# --- the prompt ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the intake assistant for FloodLine, a flood emergency reporting line for Dearborn, Michigan.
Residents report flooding with a short voice note or typed text from their phone. Dearborn has a large \
Arabic-speaking community (Lebanese, Yemeni and Iraqi dialects) as well as English and Spanish speakers, and \
people often mix languages in one sentence (for example Arabic with English words like "basement").

Your JSON goes straight to emergency responders, who rank reports by danger. Be faithful and conservative: \
report only what the person actually said, or what an attached photo clearly shows. Never invent people, \
hazards, depths or places. When something is not mentioned, use false or null.

Fields:
- transcript_original: verbatim transcript of the voice note in the language and script spoken (Arabic in \
Arabic script, keeping dialect words exactly as said; do not translate, correct or summarize). If there is no \
audio, copy the typed text. If there are both, transcribe the audio, then add the typed text on a new line. \
If the audio is silent or unintelligible, return an empty string.
- language: ISO 639-1 code of the language most of the words are in (for example "ar", "en", "es").
- transcript_english: faithful, complete English translation of transcript_original (identical if it is \
already English). Write street names the way they appear on English maps.
- ai_summary: one short English line for responders, at most 15 words: who, where, how bad. Example: \
"Elderly woman trapped in basement, knee-deep water rising".
- confirmation_message: a reply to the reporter, written in the SAME language and script as \
transcript_original (Arabic speech gets an Arabic reply in Arabic script, Spanish gets Spanish; English only \
if they spoke English; with no speech, use the phone's language). Simple, warm, calm, at most 25 words. Say \
the report was received and repeat the key facts you understood (water level, who is at risk, place). Never \
promise that help is coming and never give an arrival time. End by telling them to call 911 if a life is in \
danger.
- water_depth_cm: deepest water mentioned or visible, as an integer number of centimeters, or null. \
Conversions: ankle 10, mid-shin 30, knee 50, thigh 75, waist 100, chest 130; feet x 30.48; inches x 2.54; \
halfway up a car tire about 30.
- location_type: where the water that matters is: "basement"; "home" (ground floor or living areas of a \
house or apartment); "street" (road, underpass, yard, parking lot); "car" (someone is inside a vehicle); \
or "other".
- location_hint: any street, cross streets, landmark or neighborhood mentioned, in Latin letters as it would \
appear on a map (for example "Warren Ave and Schaefer Rd", "Fordson High School"), or null. Do not guess. \
Use the map spelling of Dearborn streets even when said in Arabic or with an accent: Warren, Schaefer, Miller, \
Wyoming, Ford Rd, Michigan Ave, Dix, Vernor, Salina, Chase, Greenfield, Oakwood, Southfield, Telegraph, \
Outer Dr, Cherry Hill, Rotunda, Monroe, Military, Brady, Hubbard, Tireman, Evergreen, Lonyo, Calhoun, \
Morley, Paul, Colson, Kendal, Mercury, Fairlane, Springwells, Wagner (Arabic "وارن وشيفر" is "Warren and \
Schaefer").
- water_in_living_space: true if water is in a finished basement, a basement where someone is or lives, a \
bedroom, or a living area where people live or sleep.
- water_rising: true only if the person says the water is still rising, keeps coming in or keeps going up.
- hazards.electrical: water touching or near outlets, the electrical panel, appliances or wires; sparks; \
buzzing. hazards.sewage: sewage or sewer backup, black water. hazards.gas: gas smell or leak. \
hazards.structural: collapse, cracking walls or foundation, sagging ceiling.
- people_at_risk.elderly / children / disabled (wheelchair, cannot walk, bedridden): true only if such a \
person is in the flooded building or vehicle (any floor). people_at_risk.trapped: true ONLY when someone \
cannot get out on their own (cannot climb the stairs, door blocked, water too deep to leave, stuck in a car). \
people_at_risk.medical: injury, breathing trouble, chest pain, unconscious, or powered medical equipment at \
risk (oxygen concentrator, home dialysis). people_at_risk.count: number of people at risk at the location \
if stated, else null.
- needs.evacuation: someone must be brought out. needs.pumping: water must be pumped out of a home. \
needs.medical: medical help needed now. needs.supplies: they ask for food, drinking water, sandbags, shelter.
If a photo is attached, use it as evidence for depth and hazards, but clear spoken facts win.
"""

_LANG_NAMES = {"en": "English", "ar": "Arabic", "es": "Spanish"}


def _bool_schema(*names: str) -> dict:
    return {"type": "object", "properties": {n: {"type": "boolean"} for n in names}, "required": list(names)}


# Same shape as models.Extraction, written as plain JSON Schema for `response_json_schema`
# (nullable fields as type arrays). Transcripts come first so the model writes them before it
# summarizes; Gemini keeps the property order of the schema.
_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "transcript_original": {"type": "string", "description": "Verbatim transcript in the original language and script"},
        "language": {"type": "string", "description": "ISO 639-1 code, e.g. ar, en, es"},
        "transcript_english": {"type": "string", "description": "Faithful English translation"},
        "ai_summary": {"type": "string", "description": "One English line for responders, at most 15 words"},
        "confirmation_message": {"type": "string", "description": "Reply in the same language and script as transcript_original, at most 25 words, ends with call 911 if life is in danger"},
        "water_depth_cm": {"type": ["integer", "null"], "description": "Deepest water in cm, or null"},
        "location_type": {"type": "string", "enum": ["basement", "home", "street", "car", "other"]},
        "location_hint": {"type": ["string", "null"], "description": "Street, cross streets or landmark in Latin letters, or null"},
        "water_in_living_space": {"type": "boolean"},
        "water_rising": {"type": "boolean"},
        "hazards": _bool_schema("electrical", "sewage", "gas", "structural"),
        "people_at_risk": {
            "type": "object",
            "properties": {
                **{n: {"type": "boolean"} for n in ("elderly", "children", "disabled", "medical", "trapped")},
                "count": {"type": ["integer", "null"]},
            },
            "required": ["elderly", "children", "disabled", "medical", "trapped", "count"],
        },
        "needs": _bool_schema("evacuation", "pumping", "medical", "supplies"),
    },
    "required": [
        "transcript_original", "language", "transcript_english", "ai_summary", "confirmation_message",
        "water_depth_cm", "location_type", "location_hint", "water_in_living_space", "water_rising",
        "hazards", "people_at_risk", "needs",
    ],
}


# --- media types --------------------------------------------------------------------------

_AUDIO_ALIASES = {
    "audio/wav": "audio/wav", "audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/vnd.wave": "audio/wav",
    "audio/x-pn-wav": "audio/wav",
    "audio/mpeg": "audio/mp3", "audio/mp3": "audio/mp3", "audio/mpeg3": "audio/mp3", "audio/x-mp3": "audio/mp3",
    "audio/x-mpeg": "audio/mp3",
    "audio/ogg": "audio/ogg", "application/ogg": "audio/ogg", "audio/opus": "audio/ogg",
    "audio/webm": "audio/webm", "video/webm": "audio/webm",
    "audio/mp4": "audio/mp4", "audio/m4a": "audio/mp4", "audio/x-m4a": "audio/mp4", "video/mp4": "audio/mp4",
    "audio/aac": "audio/aac", "audio/x-aac": "audio/aac", "audio/aacp": "audio/aac",
    "audio/flac": "audio/flac", "audio/x-flac": "audio/flac",
    "audio/aiff": "audio/aiff", "audio/x-aiff": "audio/aiff",
}


def normalize_audio_mime(mime: str | None, data: bytes | None = None) -> str:
    """Map browser/OS spellings to what Gemini expects; sniff the bytes when the type is missing or odd."""
    base = (mime or "").split(";")[0].strip().lower()
    if base in _AUDIO_ALIASES:
        return _AUDIO_ALIASES[base]
    head = (data or b"")[:12]
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mp3"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"
    if head[4:8] == b"ftyp":
        return "audio/mp4"
    if head[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return "audio/aac"
    return base if base.startswith("audio/") else "audio/wav"


def _normalize_image_mime(mime: str | None) -> str:
    base = (mime or "").split(";")[0].strip().lower()
    return {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}.get(base, base or "image/jpeg")


# --- parsing and normalization ------------------------------------------------------------

_LOCATION_SYNONYMS = {
    "house": "home", "apartment": "home", "living room": "home", "bedroom": "home", "residence": "home",
    "road": "street", "underpass": "street", "yard": "street", "parking lot": "street", "intersection": "street",
    "vehicle": "car", "truck": "car", "cellar": "basement",
}
_NULLISH = {"", "null", "none", "unknown", "n/a", "na", "-"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_extraction(raw: str, *, ui_language: str = "en") -> Extraction:
    """Gemini's JSON text -> validated Extraction. Raises AIError("bad output") when unusable."""
    text = raw.strip()
    # Some models wrap JSON in a Markdown fence even when asked not to.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AIError("bad output") from exc
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise AIError("bad output")
    try:
        return Extraction.model_validate(normalize_fields(data, ui_language=ui_language))
    except ValidationError as exc:
        raise AIError("bad output") from exc


def normalize_fields(data: dict, *, ui_language: str = "en") -> dict:
    """Trim, coerce and clamp the model's fields so small slips do not throw the whole answer away."""
    from .fallback_rules import extract_from_text  # local: only needed for a missing confirmation

    out: dict[str, Any] = {}
    original = _as_text(data.get("transcript_original"))
    out["transcript_original"] = original

    lang_raw = _as_text(data.get("language")).lower()
    match = re.match(r"^([a-z]{2})(?:[-_][a-z0-9]+)?$", lang_raw)
    if match:
        out["language"] = match.group(1)
    else:
        # "Arabic", "ara" or nothing at all: fall back to looking at the script.
        names = {"arabic": "ar", "english": "en", "spanish": "es", "ara": "ar", "eng": "en", "spa": "es"}
        out["language"] = names.get(lang_raw) or detect_language(original, ui_language)

    english = _as_text(data.get("transcript_english"))
    if not english and out["language"] == "en":
        english = original
    out["transcript_english"] = english

    summary = _as_text(data.get("ai_summary"))
    if not summary:
        summary = english[:100] or original[:100]
    out["ai_summary"] = summary[:200]

    confirmation = _as_text(data.get("confirmation_message"))
    if not confirmation:
        lang = out["language"] if out["language"] in LANGS else (ui_language if ui_language in LANGS else "en")
        confirmation = extract_from_text(original, lang).confirmation_message
    out["confirmation_message"] = confirmation[:400]

    depth = _as_int(data.get("water_depth_cm"))
    out["water_depth_cm"] = None if depth is None else max(0, min(500, depth))

    loc = _as_text(data.get("location_type")).lower()
    loc = _LOCATION_SYNONYMS.get(loc, loc)
    out["location_type"] = loc if loc in ("basement", "home", "street", "car", "other") else "other"

    hint = _as_text(data.get("location_hint"))
    out["location_hint"] = None if hint.lower() in _NULLISH else hint[:200]

    out["water_in_living_space"] = _as_bool(data.get("water_in_living_space"))
    out["water_rising"] = _as_bool(data.get("water_rising"))

    def flags(key: str, names: tuple[str, ...]) -> dict:
        src = data.get(key) if isinstance(data.get(key), dict) else {}
        return {n: _as_bool(src.get(n)) for n in names}

    out["hazards"] = flags("hazards", ("electrical", "sewage", "gas", "structural"))
    people = flags("people_at_risk", ("elderly", "children", "disabled", "medical", "trapped"))
    src_people = data.get("people_at_risk") if isinstance(data.get("people_at_risk"), dict) else {}
    count = _as_int(src_people.get("count"))
    people["count"] = count if count is not None and 1 <= count <= 1000 else None
    out["people_at_risk"] = people
    out["needs"] = flags("needs", ("evacuation", "pumping", "medical", "supplies"))
    return out


# --- public entry points ------------------------------------------------------------------


def _build_contents(
    audio: bytes | None, audio_mime: str | None, text: str | None, photo: bytes | None, photo_mime: str | None,
    ui_language: str,
) -> list[Any]:
    from google.genai import types

    parts: list[Any] = []
    size = 0
    if audio:
        parts.append(types.Part.from_bytes(data=audio, mime_type=normalize_audio_mime(audio_mime, audio)))
        size += len(audio)
    if photo:
        if size + len(photo) <= _MAX_INLINE_BYTES:
            parts.append(types.Part.from_bytes(data=photo, mime_type=_normalize_image_mime(photo_mime)))
        else:
            log.warning("photo dropped from the Gemini request: audio + photo exceed the inline limit")

    lang_name = _LANG_NAMES.get(ui_language, "English")
    lines = [f"The reporter's phone is set to {lang_name}. That is only a hint: the language actually spoken decides."]
    if audio:
        lines.append("The voice note is attached.")
    if photo:
        lines.append("A photo from the reporter is attached.")
    if text and text.strip():
        lines.append("Typed text from the reporter:\n" + text.strip())
    lines.append("Return the JSON for this report.")
    parts.append("\n".join(lines))
    return parts


async def extract_report(
    *,
    audio: bytes | None,
    audio_mime: str | None,
    text: str | None,
    photo: bytes | None,
    photo_mime: str | None,
    ui_language: str,
) -> tuple[Extraction, str, int]:
    """Understand one report. Returns (extraction, engine, latency_ms); engine is the model that answered.

    Raises AIError on any failure, including AI disabled and taking longer than config.AI_TIMEOUT_S.
    """
    if not ai_enabled():
        raise AIError("AI not configured")
    if not audio and not (text and text.strip()):
        raise AIError("empty report")

    started = time.perf_counter()
    model = None
    try:
        contents = _build_contents(audio, audio_mime, text, photo, photo_mime, ui_language)
        # The chain keeps its own deadline a little inside the outer one, so it can report which
        # model ran out of time; the outer wait_for is only the hard stop.
        extraction, model = await asyncio.wait_for(
            _generate(
                contents, system_instruction=SYSTEM_PROMPT, json_schema=_RESPONSE_SCHEMA, max_output_tokens=4096,
                budget_s=max(MIN_ATTEMPT_S, config.AI_TIMEOUT_S - 0.3),
                accept=lambda raw: parse_extraction(raw, ui_language=ui_language),
            ),
            timeout=config.AI_TIMEOUT_S,
        )
    except (TimeoutError, asyncio.TimeoutError):
        _log_outcome(model, started, "timeout")
        raise AIError("timeout") from None
    except AIError as exc:
        _log_outcome(model, started, str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 - SDK surprises: keep the reason short
        _log_outcome(model, started, f"error: {type(exc).__name__}")
        raise AIError(f"error: {_short(type(exc).__name__ + ' ' + str(exc), 60)}") from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    _log_outcome(model, started, "ok")
    return extraction, model, latency_ms


async def generate_text(prompt: str, *, system_instruction: str, timeout_s: float = 10.0) -> tuple[str, str]:
    """Plain-text Gemini call through the same model chain (used by the briefing). Raises AIError."""
    if not ai_enabled():
        raise AIError("AI not configured")
    started = time.perf_counter()
    try:
        text, model = await asyncio.wait_for(
            _generate([prompt], system_instruction=system_instruction, max_output_tokens=2048,
                      budget_s=max(MIN_ATTEMPT_S, timeout_s - 0.3)),
            timeout=timeout_s,
        )
    except (TimeoutError, asyncio.TimeoutError):
        _log_outcome(None, started, "timeout", kind="text")
        raise AIError("timeout") from None
    except AIError as exc:
        _log_outcome(None, started, str(exc), kind="text")
        raise
    except Exception as exc:  # noqa: BLE001
        _log_outcome(None, started, f"error: {type(exc).__name__}", kind="text")
        raise AIError(f"error: {type(exc).__name__}") from exc
    _log_outcome(model, started, "ok", kind="text")
    return text.strip(), model


def _log_outcome(model: str | None, started: float, outcome: str, kind: str = "extract") -> None:
    latency_ms = int((time.perf_counter() - started) * 1000)
    level = logging.INFO if outcome == "ok" else logging.WARNING
    log.log(level, "gemini %s model=%s latency_ms=%d outcome=%s", kind, model or "-", latency_ms, outcome)
