"""OpenStreetMap geocoding biased to Dearborn: Nominatim (1 request/second, cached) plus Overpass.

Nominatim's usage policy asks for at most one request per second, an identifying User-Agent and
caching, so every Nominatim request goes through one asyncio lock plus a monotonic clock, and every
answer, including "not found", is cached in memory and in a small JSON file. Transient failures
(timeouts, 5xx) are not cached, so the next report can try again.

Intersections ("Warren and Schaefer") do not geocode in Nominatim directly. In order:
1. Nominatim street geometry: both streets inside the Dearborn box with polygon_geojson=1 and
   dedupe=0 (one result per OSM way), then the closest approach of the two. Two requests, ~2 s, and
   it does not depend on Overpass (live, Oct 2026: overpass-api.de and the kumi mirror both timed
   out at 8 s while this found the exact shared node of Warren and Schaefer).
2. Overpass: a node shared by both named streets, overpass-api.de and then the kumi mirror,
   3.5 s each.
3. Nominatim again inside the small box where the two streets' extents overlap (long streets have
   more than 40 ways, so the crossing segment may be missing from step 1).
4. The point of the first street closest to the second ("Warren Ave near Schaefer Rd"), else the
   first street on its own.
Street names match prefix- and suffix-insensitively: "Warren" finds "West Warren Avenue",
"Schaefer" finds "Schaefer Road" and "Schaefer Highway".
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any

import httpx

from . import config

log = logging.getLogger("floodline.geocode")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_MIRROR_URL = "https://overpass.kumi.systems/api/interpreter"
MIN_INTERVAL_S = 1.0  # Nominatim policy: at most one request per second
NOMINATIM_TIMEOUT_S = 5.0
OVERPASS_TIMEOUT_S = 3.5
FORWARD_BUDGET_S = 14.0  # a whole forward lookup, all fallbacks included
MEET_M = 40.0  # streets closer than this meet: an intersection ("A & B")
NEAR_M = 400.0  # closer than this: "A near B" at the closest point of A
SOFT_CACHE_S = 600.0  # degraded answers given while a service was failing: remembered this long, in memory only

# Tests swap this for an httpx.MockTransport so no request ever leaves the machine.
_transport: httpx.AsyncBaseTransport | None = None

_cache: dict[str, Any] = {}
_cache_path: Path | None = None  # file the in-memory cache was loaded from
_soft_cache: dict[str, tuple[float, Any]] = {}  # key -> (expires at, value); never written to disk
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
    _soft_cache.clear()


def _cache_get(key: str) -> tuple[bool, Any]:
    _ensure_cache_loaded()
    soft = _soft_cache.get(key)
    if soft is not None:
        if soft[0] > time.monotonic():
            return True, soft[1]
        _soft_cache.pop(key, None)
    if key not in _cache:
        return False, None
    value = _cache[key]
    return True, (None if value == _MISS else value)


def _cache_put(key: str, value: Any, *, soft: bool = False) -> None:
    """soft=True: an answer given while a service was failing; better ones may come, so do not persist it."""
    _ensure_cache_loaded()
    if soft:
        _soft_cache[key] = (time.monotonic() + SOFT_CACHE_S, value)
        return
    _soft_cache.pop(key, None)
    _cache[key] = _MISS if value is None else value
    _save_cache()


# --- HTTP ---------------------------------------------------------------------------------


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(id(loop))
    if lock is None:
        _locks.clear()  # drop locks from loops that are gone
        lock = _locks[id(loop)] = asyncio.Lock()
    return lock


async def _send(method: str, url: str, *, timeout: float, **kwargs: Any) -> Any:
    """One request; returns parsed JSON or raises _Transient."""
    global request_count
    headers = {"User-Agent": config.NOMINATIM_USER_AGENT, "Accept-Language": "en"}
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=timeout, headers=headers) as client:
            request_count += 1
            response = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise _Transient(f"{type(exc).__name__}: {exc}") from exc
    if response.status_code >= 400:
        # 429/5xx: busy. Other 4xx: our request is wrong. Neither is a real "not found", so no caching.
        raise _Transient(f"HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise _Transient("invalid JSON") from exc


async def _request(method: str, url: str, *, timeout: float, **kwargs: Any) -> Any:
    """One polite Nominatim request: waits for the global 1 request/second slot."""
    global _last_request_at
    async with _lock():
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await _send(method, url, timeout=timeout, **kwargs)
        finally:
            _last_request_at = time.monotonic()


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


# --- street names -------------------------------------------------------------------------

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


def _known_streets() -> list[str]:
    from .fallback_rules import _STREETS  # the Dearborn street list the keyword fallback uses

    names = {english.split()[0] for english, _ in _STREETS}
    return sorted(names | {"Paul", "Colson", "Kendal", "Ternes", "Horger", "Freda", "Proctor", "Banner"})


def _canonical_street(street: str) -> str:
    """Fix a misheard spelling of a known Dearborn street ("Shafer Rd" -> "Schaefer Rd")."""
    core = _street_core(street)
    if not re.fullmatch(r"[A-Za-z]{4,}", core):
        return street
    known = _known_streets()
    if core.lower() in {k.lower() for k in known}:
        return street
    match = difflib.get_close_matches(core.title(), known, n=1, cutoff=0.8)
    if not match:
        return street
    return re.sub(re.escape(core), match[0], street, count=1)


def _name_matches(name: str | None, core: str) -> bool:
    """OSM name contains the core as whole words: "West Warren Avenue" ~ "Warren"."""
    if not name:
        return False
    return re.search(r"(^|\s)" + re.escape(core.lower()) + r"(\s|$)", name.lower()) is not None


def _box() -> tuple[float, float, float, float]:
    west, south, east, north = config.DEARBORN_VIEWBOX
    return west, south, east, north


def _title(street: str) -> str:
    words = [w if w.isupper() and len(w) > 1 else w[:1].upper() + w[1:] for w in street.split()]
    return abbreviate(" ".join(words))


# --- geometry (local flat projection; fine at city scale) ----------------------------------

_M_PER_DEG_LAT = 111_132.0


def _proj(lat0: float):
    k = _M_PER_DEG_LAT * math.cos(math.radians(lat0))

    def to_xy(lon: float, lat: float) -> tuple[float, float]:
        return lon * k, lat * _M_PER_DEG_LAT

    def to_lonlat(x: float, y: float) -> tuple[float, float]:
        return x / k, y / _M_PER_DEG_LAT

    return to_xy, to_lonlat


def _lines(geojson: dict | None) -> list[list[tuple[float, float]]]:
    """GeoJSON geometry -> list of polylines of (lon, lat)."""
    if not isinstance(geojson, dict):
        return []
    kind, coords = geojson.get("type"), geojson.get("coordinates") or []
    try:
        if kind == "LineString":
            return [[(float(x), float(y)) for x, y, *_ in coords]]
        if kind in ("MultiLineString", "Polygon"):
            return [[(float(x), float(y)) for x, y, *_ in line] for line in coords]
        if kind == "MultiPolygon":
            return [[(float(x), float(y)) for x, y, *_ in ring] for poly in coords for ring in poly]
        if kind == "Point":
            return [[(float(coords[0]), float(coords[1]))]]
    except (TypeError, ValueError):
        return []
    return []


def _closest_on_segment(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return (ax + t * dx, ay + t * dy)


def _segment_crossing(a1, a2, b1, b2):
    """Point where segments a1-a2 and b1-b2 cross, or None."""
    d = (a2[0] - a1[0]) * (b2[1] - b1[1]) - (a2[1] - a1[1]) * (b2[0] - b1[0])
    if d == 0:
        return None
    t = ((b1[0] - a1[0]) * (b2[1] - b1[1]) - (b1[1] - a1[1]) * (b2[0] - b1[0])) / d
    u = ((b1[0] - a1[0]) * (a2[1] - a1[1]) - (b1[1] - a1[1]) * (a2[0] - a1[0])) / d
    if 0 <= t <= 1 and 0 <= u <= 1:
        return (a1[0] + t * (a2[0] - a1[0]), a1[1] + t * (a2[1] - a1[1]))
    return None


def closest_approach(lines_a, lines_b) -> tuple[float, tuple[float, float], tuple[float, float]] | None:
    """(gap in meters, meeting point (lat, lng), point on A closest to B (lat, lng)) for two sets of
    polylines of (lon, lat); None if either is empty."""
    lines_a = [line for line in lines_a if line]
    lines_b = [line for line in lines_b if line]
    if not lines_a or not lines_b:
        return None
    pts = [p for line in lines_a + lines_b for p in line]
    to_xy, to_lonlat = _proj(sum(p[1] for p in pts) / len(pts))
    segs_a = [(to_xy(*l[i]), to_xy(*l[min(i + 1, len(l) - 1)])) for l in lines_a for i in range(max(1, len(l) - 1))]
    segs_b = [(to_xy(*l[i]), to_xy(*l[min(i + 1, len(l) - 1)])) for l in lines_b for i in range(max(1, len(l) - 1))]
    best: tuple[float, tuple, tuple] | None = None
    for a1, a2 in segs_a:
        for b1, b2 in segs_b:
            cross = _segment_crossing(a1, a2, b1, b2)
            if cross is not None:
                candidates = [(0.0, cross, cross)]
            else:
                candidates = []
                for p in (b1, b2):
                    q = _closest_on_segment(p, a1, a2)
                    candidates.append((math.dist(p, q), ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2), q))
                for p in (a1, a2):
                    q = _closest_on_segment(p, b1, b2)
                    candidates.append((math.dist(p, q), ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2), p))
            for cand in candidates:
                if best is None or cand[0] < best[0]:
                    best = cand
    if best is None:
        return None
    gap, meet, on_a = best
    meet_lon, meet_lat = to_lonlat(*meet)
    a_lon, a_lat = to_lonlat(*on_a)
    return gap, (meet_lat, meet_lon), (a_lat, a_lon)


# --- forward ------------------------------------------------------------------------------


async def _overpass_intersection(a: str, b: str, url: str = OVERPASS_URL) -> dict | None:
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
        f"[out:json][timeout:{int(OVERPASS_TIMEOUT_S)}];\n"
        f"{name_filter(a)}->.a;\n"
        f"{name_filter(b)}->.b;\n"
        f"node(w.a)(w.b);\n"
        f"out 1;"
    )
    # Overpass is not Nominatim: no 1/s slot, but its own short timeout.
    data = await _send("POST", url, timeout=OVERPASS_TIMEOUT_S, data={"data": query})
    for element in (data or {}).get("elements", []):
        if element.get("type") == "node" and "lat" in element and "lon" in element:
            label = f"{_title(a)} & {_title(b)}"
            return {"lat": round(float(element["lat"]), 6), "lng": round(float(element["lon"]), 6), "label": label}
    return None


async def _street_ways(street: str, box: tuple[float, float, float, float] | None = None) -> list[dict]:
    """Every OSM way of a street inside the box (default: Dearborn), with its geometry."""
    west, south, east, north = box or _box()
    core = _street_core(street)
    params = {
        "q": f"{core}, Dearborn, MI",
        "format": "jsonv2",
        "limit": 40,
        "countrycodes": "us",
        "viewbox": f"{west:.5f},{north:.5f},{east:.5f},{south:.5f}",
        "bounded": 1,
        "dedupe": 0,  # one result per way: a long street is many ways
        "polygon_geojson": 1,
        "polygon_threshold": 0.00005,  # simplify geometry to ~5 m: smaller answers, same crossing
        "layer": "address",
    }
    data = await _request("GET", config.NOMINATIM_URL.rstrip("/") + "/search", timeout=NOMINATIM_TIMEOUT_S, params=params)
    if not isinstance(data, list):
        return []
    return [h for h in data if h.get("category") == "highway" and _name_matches(h.get("name"), core) and h.get("geojson")]


def _extent(ways: list[dict]) -> tuple[float, float, float, float] | None:
    pts = [p for w in ways for line in _lines(w.get("geojson")) for p in line]
    if not pts:
        return None
    lons, lats = [p[0] for p in pts], [p[1] for p in pts]
    return min(lons), min(lats), max(lons), max(lats)


def _overlap_box(e1, e2, pad_m: float = 250.0) -> tuple[float, float, float, float] | None:
    """Where the extents of two streets overlap (padded); None if they do not or it is not small."""
    if e1 is None or e2 is None:
        return None
    pad_lat = pad_m / _M_PER_DEG_LAT
    pad_lon = pad_m / (_M_PER_DEG_LAT * math.cos(math.radians((e1[1] + e1[3]) / 2)))
    west = max(e1[0], e2[0]) - pad_lon
    south = max(e1[1], e2[1]) - pad_lat
    east = min(e1[2], e2[2]) + pad_lon
    north = min(e1[3], e2[3]) + pad_lat
    if west >= east or south >= north:
        return None
    full = _box()
    if east - west >= 0.6 * (full[2] - full[0]) and north - south >= 0.6 * (full[3] - full[1]):
        return None  # no narrower than Dearborn itself: asking again would return the same ways
    return west, south, east, north


def _street_label(street: str, ways: list[dict]) -> str:
    """The reporter's street name, completed with the OSM suffix when they left it out ("Warren" -> "Warren Ave")."""
    label = _title(street)
    if _street_core(street) != street.strip() or not ways:
        return label  # they said "Warren Ave" (or "W Warren"): keep it
    last = (ways[0].get("name") or "").split()[-1:] or [""]
    suffix = _SUFFIXES.get(last[0])
    return f"{label} {suffix}" if suffix else label


def _geometry(ways: list[dict]) -> list[list[tuple[float, float]]]:
    return [line for w in ways for line in _lines(w.get("geojson")) if line]


def _hit_from_approach(approach, label_a: str, label_b: str) -> dict | None:
    gap, meet, on_a = approach
    if gap <= MEET_M:
        return {"lat": round(meet[0], 6), "lng": round(meet[1], 6), "label": f"{label_a} & {label_b}"}
    if gap <= NEAR_M:
        return {"lat": round(on_a[0], 6), "lng": round(on_a[1], 6), "label": f"{label_a} near {label_b}"}
    return None


async def _intersection(a: str, b: str) -> tuple[dict | None, bool]:
    """Pin for two crossing streets. Returns (hit, degraded): degraded=True when a service failed on
    the way and the answer may improve later (so it is not cached for good)."""
    failed = False
    ways_a: list[dict] = []
    ways_b: list[dict] = []

    def best(extra_a: list[dict], extra_b: list[dict]) -> dict | None:
        approach = closest_approach(_geometry(ways_a + extra_a), _geometry(ways_b + extra_b))
        if approach is None:
            return None
        return _hit_from_approach(approach, _street_label(a, ways_a + extra_a), _street_label(b, ways_b + extra_b))

    # 1. Both streets' ways from Nominatim, closest approach.
    try:
        ways_a = await _street_ways(a)
        ways_b = await _street_ways(b) if ways_a else []
    except _Transient as exc:
        log.info("nominatim street lookup failed for %r / %r (%s)", a, b, exc)
        failed = True
    near = best([], [])
    if near and " & " in near["label"]:
        return near, False

    # 2. Overpass, then its mirror.
    for url in (OVERPASS_URL, OVERPASS_MIRROR_URL):
        try:
            hit = await _overpass_intersection(a, b, url)
            if hit:
                return hit, False
            break  # the server answered: there is no shared node, the mirror would say the same
        except _Transient as exc:
            log.info("overpass %s failed for %s & %s (%s)", url.split("/")[2], a, b, exc)
            failed = True

    # 3. Nominatim again, only where the two streets' extents overlap.
    box = _overlap_box(_extent(ways_a), _extent(ways_b))
    if box is not None:
        try:
            more_a = await _street_ways(a, box)
            more_b = await _street_ways(b, box)
            hit = best(more_a, more_b)
            if hit:
                near = hit  # more ways can only bring the streets closer
            if hit and " & " in hit["label"]:
                return hit, False
        except _Transient as exc:
            log.info("nominatim narrowed lookup failed for %r / %r (%s)", a, b, exc)
            failed = True

    # 4. The first street near the second, else the first street on its own.
    if near:
        return near, failed
    label = f"{_street_label(a, ways_a)} near {_street_label(b, ways_b)}"
    if ways_a:
        return {"lat": round(float(ways_a[0]["lat"]), 6), "lng": round(float(ways_a[0]["lon"]), 6), "label": label}, failed
    hit = await _nominatim_search(f"{a}, Dearborn, MI")
    if hit:
        hit["label"] = label
    return hit, failed


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


async def _forward(q: str) -> tuple[dict | None, bool]:
    pair = split_intersection(q)
    if pair:
        return await _intersection(_canonical_street(pair[0]), _canonical_street(pair[1]))
    hit = await _nominatim_search(f"{q}, Dearborn, MI")
    if hit is None:
        hit = await _nominatim_search(q)
    return hit, False


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
        started = time.monotonic()
        try:
            result, degraded = await asyncio.wait_for(_forward(q), timeout=FORWARD_BUDGET_S)
        except (_Transient, TimeoutError, asyncio.TimeoutError) as exc:
            log.warning("geocode %r failed (not cached): %s", q, exc or "timeout")
            return None
        log.info("geocode %r -> %s (%d ms%s)", q, result and result.get("label"),
                 int((time.monotonic() - started) * 1000), ", degraded" if degraded else "")
        _cache_put(key, result, soft=degraded)
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
