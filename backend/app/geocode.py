"""OpenStreetMap Nominatim geocoding, biased to Dearborn, rate-limited to 1 request/second, cached.

Nominatim's usage policy asks for at most one request per second, an identifying User-Agent and
caching, so every request (Nominatim and Overpass) goes through one asyncio lock plus a monotonic
clock, and every answer, including "not found", is cached in memory and in a small JSON file.
Transient failures (timeouts, 5xx) are not cached, so the next report can try again.

Intersections ("Warren and Schaefer") rarely geocode well in Nominatim, so for those we first ask
Overpass for a node shared by both named streets inside the Dearborn box.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from . import config

log = logging.getLogger("floodline.geocode")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MIN_INTERVAL_S = 1.0  # Nominatim policy: at most one request per second
NOMINATIM_TIMEOUT_S = 5.0
OVERPASS_TIMEOUT_S = 6.0

# Tests swap this for an httpx.MockTransport so no request ever leaves the machine.
_transport: httpx.AsyncBaseTransport | None = None

_cache: dict[str, Any] = {}
_cache_path: Path | None = None  # file the in-memory cache was loaded from
_locks: dict[int, asyncio.Lock] = {}  # one lock per event loop (tests run several loops)
_last_request_at = float("-inf")
request_count = 0  # how many HTTP requests we actually sent (used by the live check script)

_MISS = {"miss": True}  # cached marker for "the service answered, nothing found"


class _Transient(Exception):
    """Timeout, network error or 5xx: worth retrying later, so it is not cached."""


# --- cache --------------------------------------------------------------------------------


def _ensure_cache_loaded() -> None:
    """Load the JSON cache file once per path (the path comes from config, read at call time)."""
    global _cache, _cache_path
    path = Path(config.GEOCODE_CACHE_PATH)
    if _cache_path == path:
        return
    _cache = {}
    _cache_path = path
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _cache = data
    except Exception as exc:  # noqa: BLE001 - a corrupt cache file must not break geocoding
        log.warning("ignoring unreadable geocode cache %s: %s", path, exc)


def _save_cache() -> None:
    if _cache_path is None:
        return
    try:
        _cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(_cache_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not write geocode cache: %s", exc)


def clear_memory_cache() -> None:
    """Forget the in-memory cache (the file stays). Mainly for tests."""
    global _cache, _cache_path
    _cache = {}
    _cache_path = None


def _cache_get(key: str) -> tuple[bool, Any]:
    _ensure_cache_loaded()
    if key not in _cache:
        return False, None
    value = _cache[key]
    return True, (None if value == _MISS else value)


def _cache_put(key: str, value: Any) -> None:
    _ensure_cache_loaded()
    _cache[key] = _MISS if value is None else value
    _save_cache()


# --- rate-limited HTTP --------------------------------------------------------------------


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(id(loop))
    if lock is None:
        _locks.clear()  # drop locks from loops that are gone
        lock = _locks[id(loop)] = asyncio.Lock()
    return lock


async def _request(method: str, url: str, *, timeout: float, **kwargs: Any) -> Any:
    """One polite request: waits for the global 1 req/s slot, returns parsed JSON or raises _Transient."""
    global _last_request_at, request_count
    async with _lock():
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        headers = {"User-Agent": config.NOMINATIM_USER_AGENT, "Accept-Language": "en"}
        try:
            async with httpx.AsyncClient(transport=_transport, timeout=timeout, headers=headers) as client:
                request_count += 1
                response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise _Transient(f"{type(exc).__name__}: {exc}") from exc
        finally:
            _last_request_at = time.monotonic()
    if response.status_code == 429 or response.status_code >= 500:
        raise _Transient(f"HTTP {response.status_code}")
    if response.status_code >= 400:
        # 4xx other than 429 means our request is wrong; treat as "nothing found" but do not cache.
        raise _Transient(f"HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise _Transient("invalid JSON") from exc


# --- labels -------------------------------------------------------------------------------

_SUFFIXES = {
    "Avenue": "Ave", "Road": "Rd", "Street": "St", "Boulevard": "Blvd", "Drive": "Dr", "Highway": "Hwy",
    "Lane": "Ln", "Court": "Ct", "Freeway": "Fwy", "Parkway": "Pkwy", "Place": "Pl", "Terrace": "Ter",
    "Circle": "Cir", "Trail": "Trl",
}


def abbreviate(name: str) -> str:
    """'Schaefer Road' -> 'Schaefer Rd' (street signs style, shorter for map labels)."""
    words = name.split()
    return " ".join(_SUFFIXES.get(w, w) for w in words)


def format_label(address: dict | None, fallback: str | None = None) -> str | None:
    """Road plus neighbourhood/suburb (or city), e.g. "Schaefer Rd, East Dearborn"."""
    address = address or {}
    road = address.get("road") or address.get("pedestrian") or address.get("footway") or address.get("highway")
    area = (
        address.get("neighbourhood") or address.get("suburb") or address.get("quarter")
        or address.get("city_district") or address.get("city") or address.get("town") or address.get("village")
    )
    parts = [abbreviate(road) if road else None, area]
    label = ", ".join(p for p in parts if p)
    if label:
        return label
    if fallback:
        return ", ".join(fallback.split(", ")[:2])
    return None


# --- forward ------------------------------------------------------------------------------

_INTERSECTION_SPLIT = re.compile(r"\s+(?:and|at|y|و)\s+|\s*&\s*|\s*/\s*", re.IGNORECASE)
_LEADING = re.compile(
    r"^(?:the\s+)?(?:corner of|intersection of|near|by|at|on|cerca de|esquina de|en|por|قريب من|قرب|عند|بشارع|شارع)\s+",
    re.IGNORECASE,
)
_STREET_WORDS = re.compile(
    r"\b(?:ave(?:nue)?|rd|road|st|street|blvd|boulevard|dr|drive|hwy|highway|fwy|freeway|ln|lane|ct|court|"
    r"pkwy|parkway|n|s|e|w|north|south|east|west|calle|avenida)\b\.?",
    re.IGNORECASE,
)


def _clean_query(query: str) -> str:
    q = " ".join((query or "").split()).strip(" ,.;")
    q = re.sub(r"^\d+\s+block\s+of\s+", "", q, flags=re.IGNORECASE)  # "7000 block of Chase Rd"
    previous = None
    while previous != q:
        previous = q
        q = _LEADING.sub("", q).strip()
    return q


def split_intersection(query: str) -> tuple[str, str] | None:
    """'Warren Ave and Schaefer Rd' -> ('Warren Ave', 'Schaefer Rd'); None if it is not two streets."""
    q = _clean_query(query)
    parts = [p.strip(" ,.") for p in _INTERSECTION_SPLIT.split(q, maxsplit=1)]
    if len(parts) != 2 or not all(parts):
        return None
    if any(len(p) > 40 or re.search(r"\d{3,}", p) for p in parts):
        return None  # looks like an address or a sentence, not two street names
    return parts[0], parts[1]


def _street_core(name: str) -> str:
    """'W Warren Ave' -> 'Warren' (what to look for in OSM names)."""
    core = _STREET_WORDS.sub(" ", name)
    return " ".join(core.split()) or name.strip()


def _box() -> tuple[float, float, float, float]:
    west, south, east, north = config.DEARBORN_VIEWBOX
    return west, south, east, north


async def _overpass_intersection(a: str, b: str) -> dict | None:
    """Node shared by a way named like `a` and a way named like `b` inside the Dearborn box."""
    west, south, east, north = _box()
    bbox = f"{south},{west},{north},{east}"

    def name_filter(street: str) -> str:
        # Letters, digits and spaces only; anything else becomes a regex "any character".
        core = re.sub(r"[^\w ]", ".", _street_core(street))
        # OSM names are title case. Case-insensitive (",i") regexes are much slower on Overpass
        # (live: ",i" timed out at 6 s, case-sensitive answered in ~3 s), so fix the case here.
        core = " ".join(w[:1].upper() + w[1:] for w in core.split())
        return f'way["highway"]["name"~"(^| ){core}( |$)"]({bbox})'

    query = (
        f"[out:json][timeout:5];\n"
        f"{name_filter(a)}->.a;\n"
        f"{name_filter(b)}->.b;\n"
        f"node(w.a)(w.b);\n"
        f"out 1;"
    )
    data = await _request("POST", OVERPASS_URL, timeout=OVERPASS_TIMEOUT_S, data={"data": query})
    for element in (data or {}).get("elements", []):
        if element.get("type") == "node" and "lat" in element and "lon" in element:
            label = f"{_title(a)} & {_title(b)}"
            return {"lat": round(float(element["lat"]), 6), "lng": round(float(element["lon"]), 6), "label": label}
    return None


def _title(street: str) -> str:
    words = [w if w.isupper() and len(w) > 1 else w[:1].upper() + w[1:] for w in street.split()]
    return abbreviate(" ".join(words))


async def _nominatim_search(q: str) -> dict | None:
    west, south, east, north = _box()
    params = {
        "q": q,
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "us",
        "viewbox": f"{west},{north},{east},{south}",
        "bounded": 1,
        "addressdetails": 1,
    }
    data = await _request("GET", config.NOMINATIM_URL.rstrip("/") + "/search", timeout=NOMINATIM_TIMEOUT_S, params=params)
    if not isinstance(data, list) or not data:
        return None
    hit = data[0]
    label = format_label(hit.get("address"), hit.get("display_name"))
    name = hit.get("name")
    # For landmarks ("Fordson High School") the name says more than the road it sits on.
    if name and hit.get("category") not in ("highway", "place", "boundary"):
        label = f"{name}, {label}" if label and name not in label else (label or name)
    return {"lat": round(float(hit["lat"]), 6), "lng": round(float(hit["lon"]), 6), "label": label or q}


async def _forward(q: str) -> dict | None:
    pair = split_intersection(q)
    if pair:
        try:
            hit = await _overpass_intersection(*pair)
            if hit:
                return hit
        except _Transient as exc:
            log.info("overpass failed for %r (%s); falling back to Nominatim", q, exc)
        # No shared node: the first street still puts the pin in the right neighborhood.
        q_first = pair[0]
        hit = await _nominatim_search(f"{q_first}, Dearborn, MI")
        if hit:
            hit["label"] = f"{_title(pair[0])} near {_title(pair[1])}"
        return hit
    hit = await _nominatim_search(f"{q}, Dearborn, MI")
    if hit is None:
        hit = await _nominatim_search(q)
    return hit


async def geocode(query: str) -> dict | None:
    """Typed or spoken place in or near Dearborn -> {"lat": float, "lng": float, "label": str}, or None. Never raises."""
    try:
        q = _clean_query(query)
        if re.search(r"[؀-ۿ]", q):
            # OSM names here are in English: map Arabic spellings of Dearborn streets ("وارن") first.
            from .fallback_rules import extract_location_hint

            q = extract_location_hint(q) or q
        if len(q) < 2:
            return None
        key = "fwd:" + q.lower()
        hit_cached, value = _cache_get(key)
        if hit_cached:
            return dict(value) if value else None
        try:
            result = await _forward(q)
        except _Transient as exc:
            log.warning("geocode %r failed (not cached): %s", q, exc)
            return None
        _cache_put(key, result)
        return dict(result) if result else None
    except Exception as exc:  # noqa: BLE001 - geocoding is a nice-to-have; never break a report
        log.warning("geocode %r crashed: %s", query, exc)
        return None


async def reverse_geocode(lat: float, lng: float) -> str | None:
    """Short label for a GPS fix, e.g. "Schaefer Rd, East Dearborn", or None. Never raises."""
    try:
        lat, lng = float(lat), float(lng)
        key = f"rev:{lat:.4f},{lng:.4f}"
        hit_cached, value = _cache_get(key)
        if hit_cached:
            return value
        params = {"lat": f"{lat:.6f}", "lon": f"{lng:.6f}", "format": "jsonv2", "zoom": 17, "addressdetails": 1}
        try:
            data = await _request(
                "GET", config.NOMINATIM_URL.rstrip("/") + "/reverse", timeout=NOMINATIM_TIMEOUT_S, params=params
            )
        except _Transient as exc:
            log.warning("reverse geocode %s,%s failed (not cached): %s", lat, lng, exc)
            return None
        label = None
        if isinstance(data, dict) and not data.get("error"):
            label = format_label(data.get("address"), data.get("display_name"))
        _cache_put(key, label)
        return label
    except Exception as exc:  # noqa: BLE001
        log.warning("reverse geocode crashed: %s", exc)
        return None
