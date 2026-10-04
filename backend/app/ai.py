"""Gemini step: voice note (+ optional photo or typed text) in, structured Extraction out.

One request per report: the audio goes inline with the instruction prompt, and Gemini answers
with JSON that follows _RESPONSE_SCHEMA. We then validate and normalize it into models.Extraction.

Models are tried in order (config.GEMINI_MODEL, then config.GEMINI_FALLBACK_MODELS). We move on
when a model is missing or not open to this key (404/403), out of quota (429) or overloaded (5xx).
Any other failure, or running past config.AI_TIMEOUT_S, raises AIError with a short reason that
service.py stores in `ai_error`; the report itself is always kept.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
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
_unavailable: set[str] = set()  # models that said 404/403 this session; skipped while others remain
_no_thinking: set[str] = set()  # models that rejected our thinking config; we stop sending it

# Gemini accepts at most ~20 MB per inline request (prompt + all files). Stay under it.
_MAX_INLINE_BYTES = 19 * 1024 * 1024


def ai_enabled() -> bool:
    """True when a Gemini API key is configured."""
    return bool(config.GEMINI_API_KEY)


def active_model() -> str | None:
    """Model name for the UI: the last model that answered, else the configured one. None when AI is off."""
    if not ai_enabled():
        return None
    return _last_model or config.GEMINI_MODEL


def _get_client() -> Any:
    """Create the google-genai client on first use (and again if the key changes)."""
    global _client, _client_key
    key = config.GEMINI_API_KEY
    if not key:
        raise AIError("AI not configured")
    if _client is None or _client_key != key:
        from google import genai  # imported lazily: keeps startup fast and tests independent of it

        _client = genai.Client(api_key=key)
        _client_key = key
    return _client


def model_chain() -> list[str]:
    """Configured model first, then the fallbacks, without duplicates.

    Models that already answered 404/403 go to the back instead of being dropped, so a
    temporary hiccup cannot leave us with an empty chain.
    """
    chain: list[str] = []
    for name in [config.GEMINI_MODEL, *config.GEMINI_FALLBACK_MODELS]:
        name = (name or "").strip()
        if name and name not in chain:
            chain.append(name)
    return [m for m in chain if m not in _unavailable] + [m for m in chain if m in _unavailable]


def _thinking_config(model: str) -> Any:
    """Thinking costs seconds we do not have, so turn it down as far as each family allows.

    - gemini-2.5-*: thinking_budget=0 switches it off (Flash and Flash-Lite; 2.5 Pro rejects it,
      which the caller handles by retrying without).
    - gemini-3+ Flash and the "-latest" aliases: thinking cannot be switched off, but
      thinking_level="minimal" is the documented low-latency setting.
    - Anything else: no thinking config.
    """
    from google.genai import types

    name = model.lower()
    if model in _no_thinking:
        return None
    if name.startswith("gemini-2.5"):
        return types.ThinkingConfig(thinking_budget=0)
    match = re.match(r"gemini-(\d+)", name)
    newer = bool(match and int(match.group(1)) >= 3)
    if "flash" in name and (newer or "latest" in name):
        return types.ThinkingConfig(thinking_level="minimal")
    return None


def _short(text: Any, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def _generate(
    contents: list[Any],
    *,
    system_instruction: str,
    json_schema: dict | None = None,
    max_output_tokens: int = 2048,
) -> tuple[str, str]:
    """Run one prompt through the model chain. Returns (response text, model that answered)."""
    global _last_model
    from google.genai import errors, types

    client = _get_client()
    reasons: list[str] = []
    for model in model_chain():
        thinking = _thinking_config(model)
        attempts = [thinking, None] if thinking is not None else [None]
        for thinking_cfg in attempts:
            cfg: dict[str, Any] = {"system_instruction": system_instruction, "max_output_tokens": max_output_tokens}
            if json_schema is not None:
                cfg["response_mime_type"] = "application/json"
                cfg["response_json_schema"] = json_schema
            if model.lower().startswith("gemini-2"):
                # Low temperature keeps transcripts verbatim. Gemini 3 docs ask to keep the default 1.0.
                cfg["temperature"] = 0.2
            if thinking_cfg is not None:
                cfg["thinking_config"] = thinking_cfg
            try:
                response = await client.aio.models.generate_content(
                    model=model, contents=contents, config=types.GenerateContentConfig(**cfg)
                )
            except errors.APIError as exc:
                code = getattr(exc, "code", None) or 0
                detail = _short(getattr(exc, "message", None) or exc)
                if code == 400 and thinking_cfg is not None:
                    log.info("gemini model=%s rejected thinking config (%s); retrying without", model, detail)
                    _no_thinking.add(model)
                    continue
                if code in (403, 404):
                    log.warning("gemini model=%s unavailable (%s %s); trying next", model, code, detail)
                    _unavailable.add(model)
                    reasons.append("model unavailable")
                    break
                if code == 429:
                    log.warning("gemini model=%s out of quota (%s); trying next", model, detail)
                    reasons.append("quota")
                    break
                if code >= 500:
                    log.warning("gemini model=%s server error %s (%s); trying next", model, code, detail)
                    reasons.append("model unavailable")
                    break
                raise AIError(f"error: {code} {detail}".strip()) from exc
            _unavailable.discard(model)
            text = _response_text(response)
            _last_model = model
            return text, model
    # Every model refused. Quota is the most useful thing to tell the operator about.
    raise AIError("quota" if "quota" in reasons else "model unavailable")


def _response_text(response: Any) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - odd responses (blocked, no candidates) are just "bad output"
        text = None
    if not text or not str(text).strip():
        raise AIError("bad output")
    return str(text)


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
- language: ISO 639-1 code of the main language spoken (for example "ar", "en", "es").
- transcript_english: faithful, complete English translation of transcript_original (identical if it is \
already English). Write street names the way they appear on English maps.
- ai_summary: one English line for responders, at most 15 words: who, where, how bad. Example: \
"Elderly woman in basement, knee-deep water rising, cannot climb stairs".
- confirmation_message: in the reporter's own language and a simple, warm register (Arabic: plain Levantine-\
friendly Arabic), calm, at most 25 words. Say the report was received and repeat the key facts you understood \
(water level, who is at risk, place). Never promise that help is coming and never give an arrival time. \
End by telling them to call 911 if a life is in danger.
- water_depth_cm: deepest water mentioned or visible, as an integer number of centimeters, or null. \
Conversions: ankle 10, mid-shin 30, knee 50, thigh 75, waist 100, chest 130; feet x 30.48; inches x 2.54; \
halfway up a car tire about 30.
- location_type: where the water that matters is: "basement"; "home" (ground floor or living areas of a \
house or apartment); "street" (road, underpass, yard, parking lot); "car" (someone is inside a vehicle); \
or "other".
- location_hint: any street, cross streets, landmark or neighborhood mentioned, in Latin letters as it would \
appear on a map (for example "Warren Ave and Schaefer Rd", "Fordson High School"), or null. Do not guess.
- water_in_living_space: true if water is in a finished or occupied basement, a bedroom, or a living area \
where people live or sleep.
- water_rising: true only if the person says the water is still rising or still coming in.
- hazards.electrical: water touching or near outlets, the electrical panel, appliances or wires; sparks; \
buzzing. hazards.sewage: sewage or sewer backup, black water. hazards.gas: gas smell or leak. \
hazards.structural: collapse, cracking walls or foundation, sagging ceiling.
- people_at_risk.elderly / children / disabled (wheelchair, cannot walk, bedridden): true only if such a \
person is at the flooded place. people_at_risk.trapped: true ONLY when someone cannot get out on their own \
(cannot climb the stairs, door blocked, water too deep to leave, stuck in a car). people_at_risk.medical: \
injury, breathing trouble, chest pain, unconscious, or powered medical equipment at risk (oxygen \
concentrator, home dialysis). people_at_risk.count: number of people at the location if stated, else null.
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
        "ai_summary": {"type": "string", "description": "One English line, at most 15 words"},
        "confirmation_message": {"type": "string", "description": "In the reporter's language, at most 25 words, ends with call 911 if life is in danger"},
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
        raw, model = await asyncio.wait_for(
            _generate(contents, system_instruction=SYSTEM_PROMPT, json_schema=_RESPONSE_SCHEMA),
            timeout=config.AI_TIMEOUT_S,
        )
        extraction = parse_extraction(raw, ui_language=ui_language)
    except TimeoutError:
        _log_outcome(model, started, "timeout")
        raise AIError("timeout") from None
    except AIError as exc:
        _log_outcome(model, started, str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 - network errors, SDK surprises: keep the reason short
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
            _generate([prompt], system_instruction=system_instruction, max_output_tokens=1024), timeout=timeout_s
        )
    except TimeoutError:
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
    log.log(level, "gemini %s model=%s latency_ms=%d outcome=%s", kind, model or _last_model or "-", latency_ms, outcome)
