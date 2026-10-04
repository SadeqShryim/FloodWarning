"""AI situation briefing for the incident commander, plus hotspot clusters drawn on the map.

Hotspots are deterministic (same reports in, same circles out) so the map never flickers between
runs. Gemini only writes the prose; when it is off or fails, a template says the same things.
"""
from __future__ import annotations

import logging
import re
from collections import Counter

from . import ai
from .util import haversine_m, utc_now_iso

log = logging.getLogger("floodline.briefing")

LEVEL_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
CLUSTER_RADIUS_M = 700.0
MAX_HOTSPOTS = 5
BRIEFING_TIMEOUT_S = 10.0

_STREET_SUFFIX = re.compile(
    r"\b(?:Ave|Avenue|Rd|Road|St|Street|Blvd|Boulevard|Dr|Drive|Hwy|Highway|Fwy|Freeway|Ln|Lane|Ct|Pkwy|Way|"
    r"Service Dr)\b\.?",
    re.IGNORECASE,
)


def _rank(report: dict) -> int:
    return LEVEL_RANK.get(report.get("urgency_level") or "", 0)


def _urgency_key(report: dict) -> tuple:
    """Most urgent first: level, score, then oldest first (waiting longest), then id."""
    return (-_rank(report), -(report.get("urgency_score") or 0), report.get("created_at") or "", report.get("id") or 0)


def _streets(address: str | None) -> list[str]:
    """Street names in an address_text such as "Warren Ave & Schaefer Rd" or "7000 block of Chase Rd"."""
    if not address:
        return []
    text = re.sub(r"\b\d+\s+block\s+of\s+", "", address, flags=re.IGNORECASE)
    parts = re.split(r"\s*(?:&|,|/|\band\b|\bnear\b|\bat\b)\s*", text, flags=re.IGNORECASE)
    return [" ".join(p.split()) for p in parts if p and _STREET_SUFFIX.search(p)]


def _open_located(reports: list[dict]) -> list[dict]:
    return [
        r for r in reports
        if r.get("status") == "new"
        and r.get("ai_status") != "pending"
        and r.get("lat") is not None
        and r.get("lng") is not None
    ]


def compute_hotspots(reports: list[dict]) -> list[dict]:
    """Clusters of open reports -> list of Hotspot dicts (see models.Hotspot). Deterministic, no network."""
    candidates = sorted(_open_located(reports), key=_urgency_key)
    assigned: set[int] = set()
    clusters: list[list[dict]] = []
    # Greedy: the most urgent unassigned report seeds a cluster; everything within 700 m of it joins.
    for i, seed in enumerate(candidates):
        if i in assigned:
            continue
        members = [seed]
        assigned.add(i)
        for j in range(i + 1, len(candidates)):
            if j in assigned:
                continue
            other = candidates[j]
            if haversine_m(seed["lat"], seed["lng"], other["lat"], other["lng"]) <= CLUSTER_RADIUS_M:
                members.append(other)
                assigned.add(j)
        clusters.append(members)

    hotspots = []
    for members in clusters:
        top_level = max((m.get("urgency_level") for m in members), key=lambda lv: LEVEL_RANK.get(lv or "", 0))
        if len(members) < 2 and top_level != "CRITICAL":
            continue
        lat = sum(m["lat"] for m in members) / len(members)
        lng = sum(m["lng"] for m in members) / len(members)
        farthest = max(haversine_m(lat, lng, m["lat"], m["lng"]) for m in members)
        hotspots.append({
            "label": _cluster_label(members),
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "radius_m": round(max(250.0, farthest + 150.0), 1),
            "level": top_level or "MEDIUM",
            "report_ids": [m["id"] for m in members if m.get("id") is not None],
        })
    hotspots.sort(key=lambda h: (-LEVEL_RANK.get(h["level"], 0), -len(h["report_ids"])))
    hotspots = hotspots[:MAX_HOTSPOTS]
    # Two clusters on the same long street (Warren Ave runs across town) need different labels.
    used: set[str] = set()
    for spot in hotspots:
        if spot["label"] in used:
            spot["label"] = f"{spot['label']} (#{spot['report_ids'][0]})"
        used.add(spot["label"])
    return hotspots


def _cluster_label(members: list[dict]) -> str:
    if len(members) == 1 and members[0].get("address_text"):
        return members[0]["address_text"]  # a lone critical report: its own place says the most
    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for idx, m in enumerate(members):
        for street in _streets(m.get("address_text")):
            counts[street] += 1
            first_seen.setdefault(street, idx)
    if counts:
        # Most common street; ties go to the one the most urgent report mentions.
        best = min(counts, key=lambda s: (-counts[s], first_seen[s]))
        return f"{best} area"
    seed = members[0]
    place = seed.get("address_text") or seed.get("location_hint")
    return f"Near {place}" if place else f"Near report #{seed.get('id')}"


# --- prose --------------------------------------------------------------------------------


def _counts(reports: list[dict]) -> dict:
    open_reports = [r for r in reports if r.get("status") == "new"]
    by_level = Counter(r.get("urgency_level") for r in open_reports if r.get("urgency_level"))
    return {
        "total": len(reports),
        "open": len(open_reports),
        "levels": {lv: by_level.get(lv, 0) for lv in LEVEL_RANK},
        "dispatched": sum(1 for r in reports if r.get("status") == "dispatched"),
        "resolved": sum(1 for r in reports if r.get("status") == "resolved"),
        "pending": sum(1 for r in open_reports if r.get("ai_status") == "pending"),
        "needs_review": sum(1 for r in open_reports if r.get("ai_status") == "failed"),
    }


def _top_open(reports: list[dict], n: int = 6) -> list[dict]:
    open_ranked = [r for r in reports if r.get("status") == "new" and r.get("urgency_level")]
    return sorted(open_ranked, key=_urgency_key)[:n]


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _place(report: dict) -> str:
    return report.get("address_text") or report.get("location_hint") or "location unknown"


def _summary(report: dict) -> str:
    text = (report.get("ai_summary") or "unclear report").strip().rstrip(".")
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def template_briefing(reports: list[dict], hotspots: list[dict]) -> str:
    """Deterministic 3-5 sentence sitrep with the same content the AI gets."""
    c = _counts(reports)
    lv = c["levels"]
    if c["open"] == 0:
        return (
            f"No open reports right now ({c['dispatched']} dispatched, {c['resolved']} resolved). "
            "Keep the intake line open and watch for new reports."
        )
    sentences = [
        f"{c['open']} open reports: {lv['CRITICAL']} critical, {lv['HIGH']} high, {lv['MEDIUM']} medium, "
        f"{lv['LOW']} low; {c['dispatched']} already dispatched."
    ]
    top = _top_open(reports)
    if top:
        worst = top[0]
        sentences.append(
            f"Most urgent: #{worst.get('id')} at {_place(worst)} ({worst.get('urgency_level')}): {_summary(worst)}."
        )
    if hotspots:
        spots = "; ".join(f"{h['label']} ({_n(len(h['report_ids']), 'report')}, {h['level'].lower()})" for h in hotspots[:3])
        sentences.append(f"Reports cluster around {spots}.")
    critical = [r for r in top if r.get("urgency_level") == "CRITICAL"][:3]
    send_first = critical or top[:2]
    if send_first:
        ids = ", ".join(f"#{r.get('id')} ({_place(r)})" for r in send_first)
        sentences.append(f"Send first: {ids}.")
    extra = []
    if c["needs_review"]:
        n = c["needs_review"]
        extra.append(f"{n} {'needs' if n == 1 else 'need'} human review because the AI could not process "
                     f"{'it' if n == 1 else 'them'}")
    if c["pending"]:
        n = c["pending"]
        extra.append(f"{n} {'is' if n == 1 else 'are'} still being processed")
    if extra:
        sentences.append(" and ".join(extra).capitalize() + ".")
    return " ".join(sentences[:5])


_BRIEFING_SYSTEM = (
    "You write situation reports for the incident commander of a flood response in Dearborn, Michigan. "
    "Write 3 to 5 short sentences of plain text (no Markdown, no lists): what is worst right now, where reports "
    "cluster, and which reports to send crews to first (cite report numbers like #12 and their places). "
    "Use only the facts given; do not invent numbers, places or arrival times."
)


def _briefing_prompt(reports: list[dict], hotspots: list[dict]) -> str:
    c = _counts(reports)
    lines = [
        f"Open reports: {c['open']} (critical {c['levels']['CRITICAL']}, high {c['levels']['HIGH']}, "
        f"medium {c['levels']['MEDIUM']}, low {c['levels']['LOW']}). Dispatched: {c['dispatched']}. "
        f"Resolved: {c['resolved']}. Still processing: {c['pending']}. Needing human review: {c['needs_review']}.",
        "Hotspots:",
    ]
    for h in hotspots:
        lines.append(f"- {h['label']}: {len(h['report_ids'])} reports, worst level {h['level']}, ids {h['report_ids']}")
    if not hotspots:
        lines.append("- none")
    lines.append("Most urgent open reports:")
    for r in _top_open(reports):
        reasons = ", ".join(r.get("urgency_reasons") or [])
        lines.append(f"- #{r.get('id')} {r.get('urgency_level')} at {_place(r)}: {r.get('ai_summary') or 'no summary'}"
                     + (f" [{reasons}]" if reasons else ""))
    return "\n".join(lines)


async def build_briefing(reports: list[dict]) -> dict:
    """Briefing dict (see models.Briefing). Gemini writes the prose when enabled; rules otherwise. Never raises."""
    try:
        hotspots = compute_hotspots(reports)
    except Exception as exc:  # noqa: BLE001 - a bad row must not kill the sitrep button
        log.warning("hotspot clustering failed: %s", exc)
        hotspots = []

    text, engine = None, "rules"
    if ai.ai_enabled():
        try:
            text, engine = await ai.generate_text(
                _briefing_prompt(reports, hotspots), system_instruction=_BRIEFING_SYSTEM, timeout_s=BRIEFING_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001 - AIError or anything else: fall back to the template
            log.warning("AI briefing failed, using template: %s", exc)
            text, engine = None, "rules"
    if not text:
        try:
            text = template_briefing(reports, hotspots)
        except Exception as exc:  # noqa: BLE001
            log.warning("briefing template failed: %s", exc)
            text = f"{sum(1 for r in reports if r.get('status') == 'new')} open reports."
        engine = "rules"
    return {"text": text, "hotspots": hotspots, "generated_at": utc_now_iso(), "engine": engine}
