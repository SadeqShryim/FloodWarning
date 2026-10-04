"""Gemini step: voice note (+ optional photo or typed text) in, structured Extraction out.

STUB written by the orchestrator. The ai-geo agent replaces the bodies; the signatures are the contract.
"""
from __future__ import annotations

from . import config
from .models import Extraction


class AIError(Exception):
    """The AI step failed (disabled, timeout, quota, unusable output). Callers keep the report anyway."""


def ai_enabled() -> bool:
    """True when a Gemini API key is configured."""
    return bool(config.GEMINI_API_KEY)


def active_model() -> str | None:
    """Model name for the UI: the last model that answered, else the configured one. None when AI is off."""
    return config.GEMINI_MODEL if ai_enabled() else None


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
    raise AIError("AI step not implemented yet")
