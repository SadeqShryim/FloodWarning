"""Small helpers shared across modules."""
from __future__ import annotations

import math
from datetime import datetime, timezone


def to_iso(dt: datetime) -> str:
    """UTC ISO 8601 with a Z suffix and second precision, e.g. 2026-10-04T15:04:05Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    return to_iso(datetime.now(timezone.utc))


def ms_to_iso(epoch_ms: int) -> str:
    """UTC ISO 8601 with milliseconds and a Z suffix, e.g. 2026-10-04T15:04:05.123Z."""
    seconds, millis = divmod(int(epoch_ms), 1000)
    return to_iso(datetime.fromtimestamp(seconds, timezone.utc))[:-1] + f".{millis:03d}Z"


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distance in meters between two lat/lng points."""
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))
