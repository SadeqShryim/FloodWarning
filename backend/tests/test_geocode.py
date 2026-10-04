"""Nominatim/Overpass client with httpx.MockTransport: cache, misses, rate limit, labels. No network."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app import config, geocode
from app.util import haversine_m

FIXTURES = Path(__file__).parent / "fixtures"

NOMINATIM = "https://nominatim.test"

SCHAEFER_HIT = [{
    "lat": "42.3437", "lon": "-83.1663", "name": "Fordson High School", "category": "amenity",
    "display_name": "Fordson High School, Ford Road, East Dearborn, Dearborn, Wayne County, Michigan, USA",
    "address": {"road": "Ford Road", "neighbourhood": "East Dearborn", "city": "Dearborn"},
}]


class Recorder:
    """Mock HTTP server: `handler(request) -> httpx.Response`, remembering requests and their times."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.times: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.times.append(time.monotonic())
        return self.handler(request)


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GEOCODE_CACHE_PATH", tmp_path / "geocode_cache.json")
    monkeypatch.setattr(config, "NOMINATIM_URL", NOMINATIM)
    monkeypatch.setattr(geocode, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(geocode, "_last_request_at", float("-inf"))
    geocode.clear_memory_cache()

    def install(handler):
        rec = Recorder(handler)
        monkeypatch.setattr(geocode, "_transport", httpx.MockTransport(rec))
        return rec

    yield install
    geocode.clear_memory_cache()


def run(coro):
    return asyncio.run(coro)


def test_forward_uses_dearborn_box_and_user_agent_and_caches(server):
    rec = server(lambda req: httpx.Response(200, json=SCHAEFER_HIT))
    first = run(geocode.geocode("Fordson High School"))
    assert first == {"lat": 42.3437, "lng": -83.1663, "label": "Fordson High School, Ford Rd, East Dearborn"}
    req = rec.requests[0]
    params = parse_qs(req.url.query.decode())
    assert req.url.path == "/search"
    assert params["q"] == ["Fordson High School, Dearborn, MI"]
    assert params["viewbox"] == ["-83.305,42.365,-83.12,42.265"]
    assert params["bounded"] == ["1"] and params["countrycodes"] == ["us"] and params["format"] == ["jsonv2"]
    assert req.headers["User-Agent"] == config.NOMINATIM_USER_AGENT

    # Cache hit: no second request, also after a "restart" (memory cleared, file kept).
    assert run(geocode.geocode("  fordson high school ")) == first
    geocode.clear_memory_cache()
    assert run(geocode.geocode("Fordson High School")) == first
    assert len(rec.requests) == 1
    saved = json.loads(config.GEOCODE_CACHE_PATH.read_text(encoding="utf-8"))
    assert "fwd:fordson high school" in saved


def test_miss_tries_plain_query_then_is_cached(server):
    rec = server(lambda req: httpx.Response(200, json=[]))
    assert run(geocode.geocode("Nowhere Place")) is None
    queries = [parse_qs(r.url.query.decode())["q"][0] for r in rec.requests]
    assert queries == ["Nowhere Place, Dearborn, MI", "Nowhere Place"]
    assert run(geocode.geocode("Nowhere Place")) is None
    assert len(rec.requests) == 2  # the miss was cached


def test_transient_errors_are_not_cached(server):
    status = {"code": 503}
    rec = server(lambda req: httpx.Response(status["code"], json=SCHAEFER_HIT if status["code"] == 200 else {}))
    assert run(geocode.geocode("Fordson High School")) is None
    status["code"] = 200
    assert run(geocode.geocode("Fordson High School")) is not None
    assert len(rec.requests) == 2


def test_network_exception_returns_none(server):
    def boom(req):
        raise httpx.ConnectTimeout("slow", request=req)

    server(boom)
    assert run(geocode.geocode("Fordson High School")) is None
    assert run(geocode.reverse_geocode(42.3, -83.2)) is None


def test_rate_limit_spaces_requests(server, monkeypatch):
    monkeypatch.setattr(geocode, "MIN_INTERVAL_S", 0.25)
    rec = server(lambda req: httpx.Response(200, json={"address": {"road": "Chase Road", "city": "Dearborn"}}))

    async def three():
        # Concurrent callers still queue behind the one global slot.
        return await asyncio.gather(
            geocode.reverse_geocode(42.30, -83.20), geocode.reverse_geocode(42.31, -83.21),
            geocode.reverse_geocode(42.32, -83.22),
        )

    labels = run(three())
    assert labels == ["Chase Rd, Dearborn"] * 3
    gaps = [b - a for a, b in zip(rec.times, rec.times[1:])]
    assert len(gaps) == 2 and all(g >= 0.24 for g in gaps), gaps


def test_reverse_label_formatting_and_rounded_cache_key(server):
    rec = server(lambda req: httpx.Response(200, json={
        "display_name": "Schaefer Road, East Dearborn, Dearborn, Michigan",
        "address": {"road": "Schaefer Road", "neighbourhood": "East Dearborn", "suburb": "Ignored", "city": "Dearborn"},
    }))
    assert run(geocode.reverse_geocode(42.343712, -83.166301)) == "Schaefer Rd, East Dearborn"
    params = parse_qs(rec.requests[0].url.query.decode())
    assert rec.requests[0].url.path == "/reverse" and params["zoom"] == ["17"] and params["format"] == ["jsonv2"]
    # Same spot to 4 decimals -> cache.
    assert run(geocode.reverse_geocode(42.34374, -83.16628)) == "Schaefer Rd, East Dearborn"
    assert len(rec.requests) == 1


def test_format_label_fallbacks():
    assert geocode.format_label({"road": "Michigan Avenue", "suburb": "West Dearborn"}) == "Michigan Ave, West Dearborn"
    assert geocode.format_label({"road": "Oakwood Boulevard", "city": "Dearborn"}) == "Oakwood Blvd, Dearborn"
    assert geocode.format_label({}, "Ford Woods Park, Dearborn, Michigan") == "Ford Woods Park, Dearborn"
    assert geocode.format_label(None) is None


def test_reverse_error_payload_is_a_cached_miss(server):
    rec = server(lambda req: httpx.Response(200, json={"error": "Unable to geocode"}))
    assert run(geocode.reverse_geocode(10.0, 10.0)) is None
    assert run(geocode.reverse_geocode(10.0, 10.0)) is None
    assert len(rec.requests) == 1


@pytest.mark.parametrize("query", ["Warren & Schaefer", "Dix y Vernor", "وارن و شيفر", "near the corner of Dix and Vernor"])
def test_intersection_spellings(query):
    pair = geocode.split_intersection(query)
    if query.startswith("وارن"):
        assert pair == ("وارن", "شيفر")
    else:
        assert pair is not None and all(pair)


def test_block_of_address_is_cleaned(server):
    rec = server(lambda req: httpx.Response(200, json=[]))
    run(geocode.geocode("7000 block of Chase Rd"))
    assert parse_qs(rec.requests[0].url.query.decode())["q"] == ["Chase Rd, Dearborn, MI"]


def test_empty_query(server):
    rec = server(lambda req: httpx.Response(200, json=[]))
    assert run(geocode.geocode("  ")) is None
    assert rec.requests == []


class OSM:
    """Fake Nominatim + Overpass. Street lookups (dedupe=0, polygon_geojson) answer from real Nominatim
    answers recorded live in Oct 2026 (tests/fixtures/nominatim_*.json), filtered by the request's
    viewbox and limit like the real service. Overpass behavior per host: "timeout", "empty", "down"
    (HTTP 504) or a node dict."""

    def __init__(self, overpass=None, mirror=None, drop_near=None, single=None, hide_always=False):
        self.overpass = {"overpass-api.de": overpass or "timeout", "overpass.kumi.systems": mirror or "timeout"}
        self.drop_near = drop_near  # (lat, lng, meters): hide ways near this point from Dearborn-wide lookups
        self.single = single if single is not None else []
        self.hide_always = hide_always  # also hide them from narrowed lookups
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        host = req.url.host
        if host in self.overpass:
            mode = self.overpass[host]
            if mode == "timeout":
                raise httpx.ReadTimeout("The read operation timed out", request=req)
            if mode == "down":
                return httpx.Response(504)
            elements = [] if mode == "empty" else [{"type": "node", "id": 1, **mode}]
            return httpx.Response(200, json={"elements": elements})
        params = parse_qs(req.url.query.decode())
        if params.get("polygon_geojson") != ["1"]:
            return httpx.Response(200, json=self.single)
        core = params["q"][0].split(",")[0].strip().lower()
        ways = STREETS.get(core, [])
        west, north, east, south = (float(v) for v in params["viewbox"][0].split(","))
        narrowed = east - west < 0.1
        out = []
        for way in ways:
            s, n, w, e = (float(v) for v in way["boundingbox"])
            if e < west or w > east or n < south or s > north:
                continue
            if self.drop_near and (not narrowed or self.hide_always):
                lat, lng, meters = self.drop_near
                if haversine_m(lat, lng, (s + n) / 2, (w + e) / 2) < meters:
                    continue
            out.append(way)
        return httpx.Response(200, json=out[: int(params["limit"][0])])

    @property
    def hosts(self):
        return [r.url.host for r in self.requests]


STREETS = {
    name: json.loads((FIXTURES / f"nominatim_{name}.json").read_text(encoding="utf-8"))
    for name in ("warren", "schaefer")
}
WARREN_SCHAEFER = (42.343985, -83.176874)  # the node West Warren Avenue and Schaefer Road share in OSM


def test_intersection_from_nominatim_geometry_without_overpass(server):
    # Live, Oct 2026: both Overpass servers timed out at 8 s; Nominatim answered in ~1 s.
    osm = OSM(overpass="timeout", mirror="timeout")
    server(osm)
    hit = run(geocode.geocode("Warren and Schaefer"))
    assert hit == {"lat": WARREN_SCHAEFER[0], "lng": WARREN_SCHAEFER[1], "label": "Warren Ave & Schaefer Rd"}
    assert osm.hosts == ["nominatim.test", "nominatim.test"]  # Overpass not even needed
    params = [parse_qs(r.url.query.decode()) for r in osm.requests]
    assert [p["q"][0] for p in params] == ["Warren, Dearborn, MI", "Schaefer, Dearborn, MI"]
    assert all(p["dedupe"] == ["0"] and p["bounded"] == ["1"] and p["limit"] == ["40"] for p in params)
    # Exact: cached for good.
    saved = json.loads(config.GEOCODE_CACHE_PATH.read_text(encoding="utf-8"))
    assert saved["fwd:warren and schaefer"]["label"] == "Warren Ave & Schaefer Rd"


@pytest.mark.parametrize("query", ["W Warren Ave & Schaefer Hwy", "warren avenue and schaefer road",
                                   "Warren y Shafer", "قريب من وارن وشيفر"])
def test_intersection_names_are_prefix_suffix_and_spelling_tolerant(server, query):
    server(OSM())
    hit = run(geocode.geocode(query))
    assert hit is not None and (hit["lat"], hit["lng"]) == WARREN_SCHAEFER
    assert "&" in hit["label"]


def test_overpass_mirror_when_the_crossing_is_beyond_nominatims_40_ways(server):
    osm = OSM(overpass="timeout", mirror={"lat": 42.343985, "lon": -83.176874}, drop_near=(*WARREN_SCHAEFER, 600))
    server(osm)
    hit = run(geocode.geocode("Warren and Schaefer"))
    assert hit == {"lat": WARREN_SCHAEFER[0], "lng": WARREN_SCHAEFER[1], "label": "Warren & Schaefer"}
    assert osm.hosts == ["nominatim.test", "nominatim.test", "overpass-api.de", "overpass.kumi.systems"]
    body = parse_qs(osm.requests[3].content.decode())["data"][0]
    assert '"(^| )Warren( |$)"]' in body and '"(^| )Schaefer( |$)"]' in body
    assert "(42.265,-83.305,42.365,-83.12)" in body and "node(w.a)(w.b)" in body and "[timeout:3]" in body


def test_overpass_answering_no_node_skips_the_mirror(server):
    osm = OSM(overpass="empty", drop_near=(*WARREN_SCHAEFER, 600))
    server(osm)
    run(geocode.geocode("Warren and Schaefer"))
    assert "overpass.kumi.systems" not in osm.hosts


def test_both_overpass_down_then_nominatim_inside_the_overlap_box(server):
    osm = OSM(overpass="down", mirror="timeout", drop_near=(*WARREN_SCHAEFER, 600))
    server(osm)
    hit = run(geocode.geocode("Warren and Schaefer"))
    assert hit == {"lat": WARREN_SCHAEFER[0], "lng": WARREN_SCHAEFER[1], "label": "Warren Ave & Schaefer Rd"}
    assert osm.hosts == ["nominatim.test", "nominatim.test", "overpass-api.de", "overpass.kumi.systems",
                         "nominatim.test", "nominatim.test"]
    west, north, east, south = (float(v) for v in parse_qs(osm.requests[4].url.query.decode())["viewbox"][0].split(","))
    assert west < WARREN_SCHAEFER[1] < east and south < WARREN_SCHAEFER[0] < north
    assert east - west < 0.03 and north - south < 0.02  # a few hundred meters, not all of Dearborn


def test_degraded_answer_is_not_cached_for_good(server):
    # Overpass down, and the crossing ways missing even in the narrow box: "near" from the closest approach.
    osm = OSM(overpass="down", mirror="down", drop_near=(*WARREN_SCHAEFER, 600), hide_always=True)
    server(osm)
    hit = run(geocode.geocode("Warren and Schaefer"))
    assert hit is not None and hit["label"] == "Warren Ave near Schaefer Rd"
    assert haversine_m(hit["lat"], hit["lng"], *WARREN_SCHAEFER) < 900
    # Remembered in memory for a while (no new requests)...
    count = len(osm.requests)
    assert run(geocode.geocode("Warren and Schaefer")) == hit
    assert len(osm.requests) == count
    # ...but not written to the cache file, so a restart (or SOFT_CACHE_S later) tries again.
    path = config.GEOCODE_CACHE_PATH
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    assert "fwd:warren and schaefer" not in saved


def test_intersection_without_geometry_falls_back_to_first_street(server):
    single = [{"lat": "42.30", "lon": "-83.15", "category": "highway", "name": "Dix Avenue",
               "address": {"road": "Dix Avenue", "neighbourhood": "Southend"}}]
    osm = OSM(overpass="empty", single=single)  # no fixture for Dix/Vernor: Nominatim finds no ways
    server(osm)
    hit = run(geocode.geocode("Dix and Vernor"))
    assert hit == {"lat": 42.3, "lng": -83.15, "label": "Dix near Vernor"}
    assert osm.hosts == ["nominatim.test", "overpass-api.de", "nominatim.test"]
    assert parse_qs(osm.requests[-1].url.query.decode())["q"] == ["Dix, Dearborn, MI"]


def test_nominatim_is_spaced_but_overpass_is_not(server, monkeypatch):
    monkeypatch.setattr(geocode, "MIN_INTERVAL_S", 0.2)
    osm = OSM(overpass="down", mirror="down", drop_near=(*WARREN_SCHAEFER, 600))
    times = []
    original = osm.__call__

    def timed(req):
        times.append((req.url.host, time.monotonic()))
        return original(req)

    server(timed)
    run(geocode.geocode("Warren and Schaefer"))
    nominatim = [t for h, t in times if h == "nominatim.test"]
    assert len(nominatim) == 4
    assert all(b - a >= 0.19 for a, b in zip(nominatim, nominatim[1:]))


def test_whole_lookup_has_a_time_budget(server, monkeypatch):
    monkeypatch.setattr(geocode, "FORWARD_BUDGET_S", 0.3)

    async def slow_forward(q):
        await asyncio.sleep(5)

    monkeypatch.setattr(geocode, "_forward", slow_forward)
    server(lambda req: httpx.Response(200, json=[]))
    started = time.monotonic()
    assert run(geocode.geocode("Warren and Schaefer")) is None
    assert time.monotonic() - started < 1.0
    assert not config.GEOCODE_CACHE_PATH.exists()  # a timeout is not a miss


def test_closest_approach_math():
    # Two crossing lines: an X centered on (42.30, -83.20).
    a = [[(-83.201, 42.30), (-83.199, 42.30)]]
    b = [[(-83.20, 42.299), (-83.20, 42.301)]]
    gap, meet, _ = geocode.closest_approach(a, b)
    assert gap == pytest.approx(0, abs=0.01) and meet == pytest.approx((42.30, -83.20), abs=1e-6)
    # Parallel streets ~111 m apart.
    c = [[(-83.201, 42.301), (-83.199, 42.301)]]
    gap, _, on_a = geocode.closest_approach(a, c)
    assert gap == pytest.approx(111, abs=1.5) and on_a[0] == pytest.approx(42.30)
    assert geocode.closest_approach(a, []) is None and geocode.closest_approach([[]], b) is None


def test_canonical_street_spelling():
    assert geocode._canonical_street("Shafer Rd") == "Schaefer Rd"
    assert geocode._canonical_street("Waren") == "Warren"
    assert geocode._canonical_street("Main St") == "Main St"  # unknown streets are left alone
    assert geocode._canonical_street("Dix") == "Dix"
