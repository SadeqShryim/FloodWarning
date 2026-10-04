"""OpenStreetMap Nominatim geocoding, biased to Dearborn, rate-limited to 1 request/second, cached.

STUB written by the orchestrator. The ai-geo agent replaces the bodies; the signatures are the contract.
"""
from __future__ import annotations


async def geocode(query: str) -> dict | None:
    """Typed or spoken place in or near Dearborn -> {"lat": float, "lng": float, "label": str}, or None. Never raises."""
    return None


async def reverse_geocode(lat: float, lng: float) -> str | None:
    """Short label for a GPS fix, e.g. "Schaefer Rd near Warren Ave, East Dearborn", or None. Never raises."""
    return None
