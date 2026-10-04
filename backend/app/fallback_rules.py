"""No-LLM fallback: rough keyword extraction from typed text (English, Arabic, Spanish).

STUB written by the orchestrator. The ai-geo agent replaces the bodies; the signatures are the contract.
"""
from __future__ import annotations

from .models import Extraction

_RECEIVED = {
    "en": "Your report was received. A responder will review it.",
    "ar": "تم استلام بلاغك. سيراجعه أحد المستجيبين.",
    "es": "Recibimos tu reporte. Un rescatista lo revisará.",
}


def extract_from_text(text: str, ui_language: str = "en") -> Extraction:
    """Best-effort extraction without an LLM. Never raises."""
    cleaned = text.strip()
    return Extraction(
        language=ui_language,
        transcript_original=cleaned,
        transcript_english=cleaned,
        ai_summary=cleaned[:120],
        confirmation_message=_RECEIVED.get(ui_language, _RECEIVED["en"]),
    )


def failed_audio_fields(ui_language: str) -> dict:
    """Fields for a voice note the AI could not process: ai_summary (English) and confirmation_message (UI language)."""
    return {
        "ai_summary": "Voice note - needs human review (AI unavailable)",
        "confirmation_message": _RECEIVED.get(ui_language, _RECEIVED["en"]),
    }
