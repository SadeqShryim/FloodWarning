"""Nominatim/Overpass client with httpx.MockTransport: cache, misses, rate limit, labels. No network."""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from app import config, geocode

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


def test_intersection_goes_to_overpass(server):
    def handler(req):
        assert req.url.host == "overpass-api.de"
        return httpx.Response(200, json={"elements": [{"type": "node", "id": 1, "lat": 42.34561, "lon": -83.17289}]})

    rec = server(handler)
    hit = run(geocode.geocode("warren Ave and Schaefer Rd"))
    assert hit == {"lat": 42.34561, "lng": -83.17289, "label": "Warren Ave & Schaefer Rd"}  # title-cased
    assert len(rec.requests) == 1
    body = parse_qs(rec.requests[0].content.decode())["data"][0]
    assert '"(^| )Warren( |$)"]' in body and '"(^| )Schaefer( |$)"]' in body
    assert "(42.265,-83.305,42.365,-83.12)" in body and "node(w.a)(w.b)" in body
    # Cached under the query.
    assert run(geocode.geocode("warren ave and schaefer rd")) == hit
    assert len(rec.requests) == 1


@pytest.mark.parametrize("query", ["Warren & Schaefer", "Dix y Vernor", "وارن و شيفر", "near the corner of Dix and Vernor"])
def test_intersection_spellings(query):
    pair = geocode.split_intersection(query)
    if query.startswith("وارن"):
        assert pair == ("وارن", "شيفر")
    else:
        assert pair is not None and all(pair)


def test_arabic_street_names_are_mapped_to_english(server):
    seen = []

    def handler(req):
        seen.append(parse_qs(req.content.decode())["data"][0])
        return httpx.Response(200, json={"elements": [{"type": "node", "lat": 42.3456, "lon": -83.1729}]})

    server(handler)
    hit = run(geocode.geocode("قريب من وارن وشيفر"))
    assert hit and hit["label"] == "Warren & Schaefer"
    assert "Warren" in seen[0] and "Schaefer" in seen[0]


def test_intersection_without_shared_node_falls_back_to_first_street(server):
    def handler(req):
        if req.url.host == "overpass-api.de":
            return httpx.Response(200, json={"elements": []})
        return httpx.Response(200, json=[{"lat": "42.30", "lon": "-83.15", "category": "highway", "name": "Dix Avenue",
                                          "address": {"road": "Dix Avenue", "neighbourhood": "Southend"}}])

    rec = server(handler)
    hit = run(geocode.geocode("Dix and Vernor"))
    assert hit == {"lat": 42.3, "lng": -83.15, "label": "Dix near Vernor"}
    assert [r.url.host for r in rec.requests] == ["overpass-api.de", "nominatim.test"]
    assert parse_qs(rec.requests[1].url.query.decode())["q"] == ["Dix, Dearborn, MI"]


def test_overpass_failure_falls_back_to_nominatim(server):
    def handler(req):
        if req.url.host == "overpass-api.de":
            return httpx.Response(504)
        return httpx.Response(200, json=SCHAEFER_HIT)

    server(handler)
    hit = run(geocode.geocode("Warren and Schaefer"))
    assert hit is not None and hit["label"] == "Warren near Schaefer"


def test_block_of_address_is_cleaned(server):
    rec = server(lambda req: httpx.Response(200, json=[]))
    run(geocode.geocode("7000 block of Chase Rd"))
    assert parse_qs(rec.requests[0].url.query.decode())["q"] == ["Chase Rd, Dearborn, MI"]


def test_empty_query(server):
    rec = server(lambda req: httpx.Response(200, json=[]))
    assert run(geocode.geocode("  ")) is None
    assert rec.requests == []
