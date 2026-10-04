"""Hotspot clustering and the sitrep (template and Gemini path with a fake). No network."""
from __future__ import annotations

import asyncio

import pytest

from app import ai, briefing, config
from app.util import haversine_m

# Warren Ave & Schaefer Rd is about (42.3456, -83.1729); a few hundred meters around it.
WARREN = (42.3456, -83.1729)


def report(id, level, lat, lng, *, status="new", ai_status="done", address=None, summary=None, score=None):
    base = {"CRITICAL": 85, "HIGH": 65, "MEDIUM": 45, "LOW": 20}
    return {
        "id": id, "urgency_level": level, "urgency_score": score or base.get(level), "lat": lat, "lng": lng,
        "status": status, "ai_status": ai_status, "address_text": address, "ai_summary": summary or f"report {id}",
        "created_at": f"2026-10-04T12:{id:02d}:00Z", "urgency_reasons": [],
        "location_hint": None,
    }


def sample():
    return [
        report(1, "HIGH", WARREN[0] + 0.001, WARREN[1], address="Warren Ave & Schaefer Rd"),
        report(2, "CRITICAL", WARREN[0], WARREN[1], address="Schaefer Rd, East Dearborn",
               summary="Elderly woman trapped in basement, water rising"),
        report(3, "MEDIUM", WARREN[0] - 0.002, WARREN[1] + 0.002, address="Warren Ave near Miller Rd"),
        report(4, "LOW", 42.2900, -83.1500, address="Dix Ave & Vernor Hwy"),           # alone, low: dropped
        report(5, "CRITICAL", 42.3220, -83.2400, address="Michigan Ave & Monroe St"),  # alone but critical: kept
        report(6, None, WARREN[0], WARREN[1] + 0.001, ai_status="pending"),            # pending: ignored
        report(7, "HIGH", WARREN[0], WARREN[1] - 0.001, status="dispatched"),          # not open: ignored
        report(8, "HIGH", None, None, address="unknown"),                               # no coordinates
        report(9, "MEDIUM", 42.3000, -83.2500, address="Outer Dr & Cherry Hill Rd"),
        report(10, "MEDIUM", 42.3010, -83.2510, address="Cherry Hill Rd, Dearborn"),
    ]


def test_hotspot_clustering():
    spots = briefing.compute_hotspots(sample())
    assert [s["report_ids"] for s in spots] == [[2, 1, 3], [5], [9, 10]]
    warren = spots[0]
    assert warren["level"] == "CRITICAL"
    # Warren Ave and Schaefer Rd both appear twice; the tie goes to the street the most urgent
    # report (#2, "Schaefer Rd, East Dearborn") mentions.
    assert warren["label"] == "Schaefer Rd area"
    for s in spots:
        assert s["radius_m"] >= 250
    # Radius covers the farthest member plus 150 m.
    members = [r for r in sample() if r["id"] in warren["report_ids"]]
    farthest = max(haversine_m(warren["lat"], warren["lng"], r["lat"], r["lng"]) for r in members)
    assert warren["radius_m"] == pytest.approx(max(250, farthest + 150), abs=0.2)
    assert spots[2]["label"] == "Cherry Hill Rd area"
    assert spots[1]["label"] == "Michigan Ave & Monroe St"  # lone report: its own place


def test_duplicate_labels_are_disambiguated():
    reports = [
        report(1, "CRITICAL", 42.3456, -83.1729, address="Warren Ave & Schaefer Rd"),
        report(2, "HIGH", 42.3460, -83.1720, address="Warren Ave & Schaefer Rd"),
        report(3, "HIGH", 42.3456, -83.2300, address="Warren Ave & Greenfield Rd"),
        report(4, "HIGH", 42.3460, -83.2290, address="Warren Ave near Greenfield Rd"),
    ]
    labels = [s["label"] for s in briefing.compute_hotspots(reports)]
    assert labels == ["Warren Ave area", "Warren Ave area (#3)"]


def test_hotspots_are_capped_at_five_and_sorted():
    reports = [report(i, "CRITICAL" if i % 2 else "HIGH", 42.27 + i * 0.012, -83.20, address=f"Street{i} Rd")
               for i in range(1, 15)]
    spots = briefing.compute_hotspots(reports)
    assert len(spots) == 5
    assert all(s["level"] == "CRITICAL" for s in spots)


def test_template_briefing_mentions_the_essentials():
    reports = sample()
    text = briefing.template_briefing(reports, briefing.compute_hotspots(reports))
    assert text.startswith("9 open reports: 2 critical, 2 high, 3 medium, 1 low; 1 already dispatched.")
    assert "#2" in text and "Schaefer Rd, East Dearborn" in text
    assert "Schaefer Rd area" in text
    assert "Send first: #2" in text
    assert text.endswith("1 is still being processed.")
    assert 3 <= text.count(". ") + 1 <= 6


def test_template_keeps_the_case_of_ai():
    # Regression: str.capitalize() on the closing sentence turned "AI" into "ai".
    reports = sample() + [report(11, "MEDIUM", 42.31, -83.21, ai_status="failed", address="Ford Rd & Chase Rd")]
    text = briefing.template_briefing(reports, briefing.compute_hotspots(reports))
    assert "1 needs human review because the AI could not process it and 1 is still being processed." in text


def test_template_with_nothing_open():
    text = briefing.template_briefing([report(1, "LOW", 42.3, -83.2, status="resolved")], [])
    assert text.startswith("No open reports")


def test_build_briefing_without_ai_uses_rules(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    out = asyncio.run(briefing.build_briefing(sample()))
    assert out["engine"] == "rules"
    assert out["hotspots"] and out["text"] and out["generated_at"].endswith("Z")


def test_build_briefing_falls_back_when_gemini_raises(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")

    async def failing(*args, **kwargs):
        raise ai.AIError("quota")

    monkeypatch.setattr(ai, "generate_text", failing)
    out = asyncio.run(briefing.build_briefing(sample()))
    assert out["engine"] == "rules"
    assert "open reports" in out["text"]


def test_build_briefing_uses_gemini_text(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    seen = {}

    async def fake_generate(prompt, *, system_instruction, timeout_s):
        seen.update(prompt=prompt, timeout_s=timeout_s)
        return "Worst is #2 at Schaefer Rd. Send a crew there first.", "gemini-2.5-flash"

    monkeypatch.setattr(ai, "generate_text", fake_generate)
    out = asyncio.run(briefing.build_briefing(sample()))
    assert out["engine"] == "gemini-2.5-flash"
    assert out["text"].startswith("Worst is #2")
    assert seen["timeout_s"] == 10.0
    assert "#2 CRITICAL at Schaefer Rd, East Dearborn" in seen["prompt"]
    assert "Schaefer Rd area: 3 reports" in seen["prompt"]


def test_build_briefing_never_raises_on_garbage(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    out = asyncio.run(briefing.build_briefing([{"id": 1, "status": "new", "lat": "x", "lng": 3, "ai_status": "done"}]))
    assert out["engine"] == "rules" and isinstance(out["text"], str)
