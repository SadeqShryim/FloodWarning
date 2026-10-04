"""Tests for the launcher (run.py). No network: cloudflared is replaced by tiny Python scripts.

    .venv\\Scripts\\python.exe -m pytest scripts -q
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
import run  # noqa: E402

# The real interpreter, not the venv redirector: killing the redirector would leave the fake
# cloudflared running.
PYTHON = getattr(sys, "_base_executable", sys.executable)


def fake_cloudflared(url: str | None, lifetime_s: float = 60) -> list[str]:
    """A stand-in for `cloudflared tunnel --url ...`: prints the banner line, then stays up."""
    lines = [
        "print('2026-10-04T11:04:14Z INF Requesting new quick Tunnel on trycloudflare.com...', flush=True)",
    ]
    if url:
        lines.append(f"print('2026-10-04T11:04:15Z INF |  {url}  |', flush=True)")
    lines.append(f"import time; time.sleep({lifetime_s})")
    return [PYTHON, "-c", "\n".join(lines)]


@pytest.fixture
def no_network(monkeypatch):
    """Record what run.py tells the server instead of sending it; skip DNS and reachability checks."""
    posted: list[str | None] = []

    def fake_http_json(url, payload=None, timeout=3.0):
        assert url.endswith("/api/config/public-url")
        posted.append(payload["public_url"])
        return {}

    monkeypatch.setattr(run, "http_json", fake_http_json)
    monkeypatch.setattr(run, "wait_for_public_dns", lambda url, timeout=45: True)
    monkeypatch.setattr(run, "check_tunnel_reachable", lambda url: None)
    monkeypatch.setattr(run, "TUNNEL_URL_TIMEOUT_S", 15)
    return posted


def test_parse_tunnel_url_skips_the_api_endpoint():
    assert run.parse_tunnel_url(
        "2026-10-04T11:04:15Z INF |  https://humanity-lover-simpson-indiana.trycloudflare.com  |"
    ) == "https://humanity-lover-simpson-indiana.trycloudflare.com"
    assert run.parse_tunnel_url(
        'ERR Error unmarshaling QuickTunnel response error="..." url=https://api.trycloudflare.com/tunnel'
    ) is None


def test_client_disconnects_are_not_shown_as_tunnel_errors():
    # Real lines from a demo run: the dashboard dropping its stalled event stream through the tunnel.
    benign = [
        '2026-10-04T11:09:17Z ERR  error="stream 513 canceled by remote with error code 0" connIndex=0 '
        "event=1 ingressRule=0 originService=http://127.0.0.1:8115",
        '2026-10-04T11:09:17Z ERR Request failed error="stream 513 canceled by remote with error code 0" '
        "connIndex=0 dest=https://x.trycloudflare.com/api/events event=0 ip=198.41.192.57 type=http",
        '2026-10-04T11:09:20Z ERR  error="Incoming request ended abruptly: context canceled" connIndex=0',
    ]
    for line in benign:
        assert not run.is_tunnel_problem(line), line
    real = [
        '2026-10-04T11:10:00Z ERR Unable to reach the origin service. The service may be down error="dial tcp '
        '127.0.0.1:8115: connectex: No connection could be made"',
        "2026-10-04T11:10:00Z ERR Failed to serve tunnel connection error=\"connection with edge closed\"",
    ]
    for line in real:
        assert run.is_tunnel_problem(line), line
    assert not run.is_tunnel_problem("2026-10-04T11:10:00Z INF Registered tunnel connection")


def test_keeper_replaces_a_tunnel_that_dies(no_network):
    keeper = run.TunnelKeeper(fake_cloudflared("https://first-try.trycloudflare.com"), "http://127.0.0.1:1")
    try:
        assert keeper.open() == "https://first-try.trycloudflare.com"
        assert no_network == ["https://first-try.trycloudflare.com"]

        keeper.tick()  # still alive: nothing happens
        assert no_network == ["https://first-try.trycloudflare.com"]

        # cloudflared dies mid-demo; the next one gets a new address.
        keeper.tunnel.proc.kill()
        keeper.tunnel.proc.wait(5)
        keeper.cmd = fake_cloudflared("https://second-try.trycloudflare.com")
        keeper.tick()

        # The dead link came off the QR code first, then the new one went up.
        assert no_network == [
            "https://first-try.trycloudflare.com", None, "https://second-try.trycloudflare.com",
        ]
        assert keeper.url == "https://second-try.trycloudflare.com"
        assert keeper.tunnel is not None and keeper.tunnel.proc.poll() is None
    finally:
        keeper.stop()
    assert keeper.tunnel is None


def test_keeper_retries_later_when_cloudflared_gives_no_url(no_network):
    keeper = run.TunnelKeeper(fake_cloudflared(None, lifetime_s=0), "http://127.0.0.1:1")
    try:
        assert keeper.open() is None
        assert keeper.tunnel is None
        assert keeper.next_try is not None and keeper.next_try > time.monotonic() + 5
        assert no_network == []

        keeper.tick()  # too early: no new attempt yet
        assert keeper.tunnel is None and keeper.failures == 1

        # Time is up and the network is back.
        keeper.next_try = time.monotonic()
        keeper.cmd = fake_cloudflared("https://back-again.trycloudflare.com")
        keeper.tick()
        assert keeper.url == "https://back-again.trycloudflare.com"
        assert no_network == ["https://back-again.trycloudflare.com"]
        assert keeper.failures == 0 and keeper.next_try is None
    finally:
        keeper.stop()


def test_dns_wait_trusts_any_resolver_that_has_the_name(monkeypatch):
    # One Cloudflare resolver node kept a cached "no such host" for over a minute in a real run;
    # Google already had the name. The QR code must not wait on the stale one.
    asked = []

    def fake_doh(endpoint, host):
        asked.append(endpoint)
        if "cloudflare" in endpoint:
            return False
        return len([e for e in asked if "google" in e]) >= 2  # Google has it on its second ask

    monkeypatch.setattr(run, "doh_has_a_record", fake_doh)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    started = time.monotonic()
    assert run.wait_for_public_dns("https://new-name.trycloudflare.com", timeout=30) is True
    assert time.monotonic() - started < 5
    assert sum("google" in e for e in asked) == 2


def test_dns_wait_gives_up_quickly_when_doh_is_blocked(monkeypatch):
    def blocked(endpoint, host):
        raise OSError("blocked by the venue firewall")

    clock = [0.0]
    monkeypatch.setattr(run, "doh_has_a_record", blocked)
    monkeypatch.setattr(run.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(run.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    assert run.wait_for_public_dns("https://new-name.trycloudflare.com", timeout=45) is False
    assert 19 <= clock[0] <= 21  # the fixed 20 s fallback, not the whole 45 s


def test_polling_requests_are_kept_out_of_the_console():
    quiet = [
        'INFO:     127.0.0.1:56973 - "GET /api/reports HTTP/1.1" 200 OK',
        'INFO:     141.215.218.198:0 - "GET /api/config HTTP/1.1" 200 OK',
        'INFO:     127.0.0.1:52364 - "GET /api/health HTTP/1.1" 200 OK',
    ]
    for line in quiet:
        assert run.is_routine_request(line), line
    shown = [
        'INFO:     141.215.218.198:0 - "POST /api/reports HTTP/1.1" 200 OK',
        'INFO:     127.0.0.1:56973 - "GET /api/reports HTTP/1.1" 500 Internal Server Error',
        'INFO:     127.0.0.1:56973 - "GET /api/reports/26 HTTP/1.1" 200 OK',
        "2026-10-04 07:04:12,962 INFO app.main: FloodLine ready: 25 reports, AI off (keyword fallback)",
    ]
    for line in shown:
        assert not run.is_routine_request(line), line


def test_retry_pause_grows_and_is_capped(no_network, monkeypatch):
    keeper = run.TunnelKeeper([], "http://127.0.0.1:1")
    monkeypatch.setattr(run.time, "monotonic", lambda: 1000.0)
    delays = []
    for _ in range(5):
        keeper._schedule_retry()
        delays.append(keeper.next_try - 1000.0)
    assert delays == [10, 30, 60, 60, 60]
