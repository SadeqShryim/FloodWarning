"""AI situation briefing for the incident commander, plus hotspot clusters drawn on the map.

STUB written by the orchestrator. The ai-geo agent replaces the bodies; the signatures are the contract.
"""
from __future__ import annotations

from .util import utc_now_iso


def compute_hotspots(reports: list[dict]) -> list[dict]:
    """Clusters of open reports -> list of Hotspot dicts (see models.Hotspot). Deterministic, no network."""
    return []


async def build_briefing(reports: list[dict]) -> dict:
    """Briefing dict (see models.Briefing). Gemini writes the prose when enabled; rules otherwise. Never raises."""
    open_reports = [r for r in reports if r.get("status") == "new"]
    return {
        "text": f"{len(open_reports)} open reports.",
        "hotspots": compute_hotspots(reports),
        "generated_at": utc_now_iso(),
        "engine": "rules",
    }
