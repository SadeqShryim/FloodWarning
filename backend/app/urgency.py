"""Deterministic, explainable urgency ranking on top of the AI fields.

The AI only *extracts* facts (depth, who is there, hazards). The ranking is plain rules, so a
responder can always see why a report sits where it does: the chips in `urgency_reasons` are
exactly the rules that fired.

Rules (first matching level wins; "vulnerable" = elderly, disabled or children;
"living space" = water in a living area or location type "home";
"standing water" = depth > 0, water in a living area, or a basement/car report)

    Level     Score   Fires when any of
    --------  ------  ---------------------------------------------------------------
    CRITICAL  80-100  C1 someone trapped
                      C2 medical emergency (people_at_risk.medical or needs.medical)
                      C3 electrical hazard + standing water
                      C4 vulnerable person + water rising + living space
    HIGH      60-79   H1 water >= 30 cm in a living space
                      H2 sewage
                      H3 vulnerable person present
                      H4 gas leak or structural damage
    MEDIUM    35-59   M1 basement report, or water in a living area
                      M2 home or car with standing water
    LOW       5-34    everything else (street flooding, information only)

    AI failed: "needs review" goes first and the level is never below MEDIUM (score >= 50);
    "info only" is never shown next to it (the input kind, "voice note" or "typed report", instead).
    Nothing extracted at all: MEDIUM 50, ["needs review", "voice note" | "typed report"].
    Pending (AI still running): no score, no level, no reasons.

Score inside a band = band floor + bonuses, clamped to the band:
    +8 per extra rule of the assigned level (beyond the first)
    +1 per 10 cm of water (max +20)
    +4 water rising
    +3 per vulnerable flag (elderly, children, disabled)
    +3 per hazard (electrical, sewage, gas, structural)
    +2 per person beyond the first (max +8)
Every bonus only grows with danger and every band sits above the one below, so adding danger
never lowers the score.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

BANDS: dict[str, tuple[int, int]] = {
    "CRITICAL": (80, 100),
    "HIGH": (60, 79),
    "MEDIUM": (35, 59),
    "LOW": (5, 34),
}
MAX_REASONS = 6
FAILED_MIN_SCORE = 50  # an unverified report must not sink below the "nothing extracted" case

VULNERABLE_KEYS = ("elderly", "children", "disabled")
HAZARD_KEYS = ("electrical", "sewage", "gas", "structural")


# ---------------------------------------------------------------- tolerant field access


def _section(report: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """A nested dict (hazards / people_at_risk / needs) that may be missing, None, JSON text or a model."""
    value = report.get(name)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):  # a pydantic model slipped through
        return value.model_dump()
    return {}


def _flag(section: Mapping[str, Any], key: str) -> bool:
    return bool(section.get(key))


def _int(value: Any) -> int:
    """Best-effort non-negative int; anything unreadable counts as 0."""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def depth_label(depth_cm: int) -> str:
    """The depth chip: "45 cm water" under a meter, "1.2 m water" from a meter up."""
    if depth_cm < 100:
        return f"{depth_cm} cm water"
    return f"{depth_cm / 100:.1f} m water"


# ---------------------------------------------------------------- scoring


def score_report(report: Mapping[str, Any]) -> dict:
    """Return {"urgency_score": int | None, "urgency_level": str | None, "urgency_reasons": list[str]}.

    Pending reports (ai_status == "pending") get None/None/[]. Pure: no I/O, never mutates the input.
    """
    if report.get("ai_status") == "pending":
        return {"urgency_score": None, "urgency_level": None, "urgency_reasons": []}

    failed = report.get("ai_status") == "failed"
    depth = _int(report.get("water_depth_cm"))
    lt = report.get("location_type") or None
    in_living_area = bool(report.get("water_in_living_space"))
    rising = bool(report.get("water_rising"))
    hz = _section(report, "hazards")
    p = _section(report, "people_at_risk")
    nd = _section(report, "needs")

    vulnerable_flags = [k for k in VULNERABLE_KEYS if _flag(p, k)]
    hazard_flags = [k for k in HAZARD_KEYS if _flag(hz, k)]
    count = _int(p.get("count"))
    trapped = _flag(p, "trapped")
    medical = _flag(p, "medical") or _flag(nd, "medical")

    vulnerable = bool(vulnerable_flags)
    living_space = in_living_area or lt == "home"
    standing_water = depth > 0 or in_living_area or lt in ("basement", "car")

    # A failed AI step with nothing usable: rank it in the middle so a human listens to it.
    nothing_extracted = (
        lt in (None, "other")
        and depth == 0
        and not (in_living_area or rising or hazard_flags or vulnerable_flags or trapped or medical)
        and not any(_flag(nd, k) for k in ("evacuation", "pumping", "supplies"))
        and count == 0
    )
    if failed and nothing_extracted:
        kind = "typed report" if report.get("input_type") == "text" else "voice note"
        return {"urgency_score": FAILED_MIN_SCORE, "urgency_level": "MEDIUM", "urgency_reasons": ["needs review", kind]}

    vulnerable_chips = list(vulnerable_flags)  # "elderly", "children", "disabled" are already the chip words
    depth_chip = [depth_label(depth)] if depth > 0 else []

    # Each rule: (fired?, the chips that explain it). Order = rule order in the docstring.
    critical = [
        (trapped, ["trapped"]),
        (medical, ["medical emergency"]),
        (_flag(hz, "electrical") and standing_water, ["electrical + water"]),
        (vulnerable and rising and living_space, [*vulnerable_chips, "water rising", "living space"]),
    ]
    high = [
        (depth >= 30 and living_space, [*depth_chip, "living space"]),
        (_flag(hz, "sewage"), ["sewage"]),
        (vulnerable, vulnerable_chips),
        (_flag(hz, "gas") or _flag(hz, "structural"),
         (["gas leak"] if _flag(hz, "gas") else []) + (["structural damage"] if _flag(hz, "structural") else [])),
    ]
    medium = [
        (lt == "basement" or in_living_area, (["basement"] if lt == "basement" else []) + (["living space"] if in_living_area else [])),
        (lt in ("home", "car") and standing_water, ["in car"] if lt == "car" else ["living space"]),
    ]

    level, fired = "LOW", []
    for name, rules in (("CRITICAL", critical), ("HIGH", high), ("MEDIUM", medium)):
        fired = [chips for hit, chips in rules if hit]
        if fired:
            level = name
            break
    if level == "LOW" and lt == "street":
        fired = [["street flooding"]]  # LOW has one "rule" worth leading with

    # Supporting facts, in the fixed vocabulary order, after the level's own triggers.
    supporting: list[str] = []
    if trapped:
        supporting.append("trapped")
    if medical:
        supporting.append("medical emergency")
    if _flag(hz, "electrical") and standing_water:
        supporting.append("electrical + water")
    supporting += vulnerable_chips
    if rising:
        supporting.append("water rising")
    supporting += depth_chip
    if _flag(hz, "sewage"):
        supporting.append("sewage")
    if _flag(hz, "gas"):
        supporting.append("gas leak")
    if _flag(hz, "structural"):
        supporting.append("structural damage")
    if living_space:
        supporting.append("living space")
    if lt == "basement":
        supporting.append("basement")
    if lt == "car":
        supporting.append("in car")
    if lt == "street":
        supporting.append("street flooding")
    if count >= 2:
        supporting.append(f"{count} people")

    reasons: list[str] = ["needs review"] if failed else []
    for chips in fired:
        reasons += chips
    reasons += supporting
    if level == "LOW" and not supporting:
        # "info only" would contradict "needs review" on an unverified report (shown as MEDIUM):
        # say what a responder should check instead, as in the nothing-extracted case.
        reasons.append(("typed report" if report.get("input_type") == "text" else "voice note") if failed else "info only")
    reasons = list(dict.fromkeys(reasons))[:MAX_REASONS]  # dedupe, keep first occurrence

    # An unverified report must not sink: failed reports are at least MEDIUM.
    if failed and level == "LOW":
        level = "MEDIUM"

    bonus = (
        8 * max(0, len(fired) - 1)
        + min(depth // 10, 20)
        + (4 if rising else 0)
        + 3 * len(vulnerable_flags)
        + 3 * len(hazard_flags)
        + min(2 * max(0, count - 1), 8)
    )
    floor, ceiling = BANDS[level]
    score = min(ceiling, floor + bonus)
    if failed:
        score = max(score, FAILED_MIN_SCORE) if level == "MEDIUM" else score

    return {"urgency_score": score, "urgency_level": level, "urgency_reasons": reasons}
