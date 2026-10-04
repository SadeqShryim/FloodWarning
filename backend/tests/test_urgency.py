"""Urgency rules (contract section 9): every trigger, precedence, bands, reasons, failed/pending handling."""
from __future__ import annotations

import itertools

import pytest

from app.urgency import BANDS, depth_label, score_report

VOCAB_FIXED = {
    "trapped", "medical emergency", "electrical + water", "elderly", "children", "disabled",
    "water rising", "sewage", "gas leak", "structural damage", "living space", "basement",
    "in car", "street flooding", "info only", "needs review", "voice note", "typed report",
}


def row(**overrides):
    """A complete, calm, done row: street report, no water, no flags."""
    base = {
        "ai_status": "done",
        "input_type": "voice",
        "water_depth_cm": None,
        "location_type": "other",
        "water_in_living_space": False,
        "water_rising": False,
        "hazards": {"electrical": False, "sewage": False, "gas": False, "structural": False},
        "people_at_risk": {"elderly": False, "children": False, "disabled": False, "medical": False,
                           "trapped": False, "count": None},
        "needs": {"evacuation": False, "pumping": False, "medical": False, "supplies": False},
    }
    for key in ("hazards", "people_at_risk", "needs"):
        if key in overrides:
            base[key] = {**base[key], **overrides.pop(key)}
    base.update(overrides)
    return base


def level(r):
    return score_report(r)["urgency_level"]


def assert_in_band(result):
    lo, hi = BANDS[result["urgency_level"]]
    assert lo <= result["urgency_score"] <= hi, result


def is_vocab(reason):
    if reason in VOCAB_FIXED:
        return True
    for suffix in (" cm water", " m water", " people"):
        if reason.endswith(suffix):
            number = reason[: -len(suffix)]
            try:
                float(number)
                return True
            except ValueError:
                return False
    return False


# ---------------------------------------------------------------- triggers, one at a time


@pytest.mark.parametrize(
    "overrides, first_reason",
    [
        ({"people_at_risk": {"trapped": True}}, "trapped"),  # C1
        ({"people_at_risk": {"medical": True}}, "medical emergency"),  # C2 (person)
        ({"needs": {"medical": True}}, "medical emergency"),  # C2 (need)
        ({"hazards": {"electrical": True}, "water_depth_cm": 5}, "electrical + water"),  # C3 via depth
        ({"hazards": {"electrical": True}, "location_type": "basement"}, "electrical + water"),  # C3 via basement
        ({"hazards": {"electrical": True}, "location_type": "car"}, "electrical + water"),  # C3 via car
        ({"hazards": {"electrical": True}, "water_in_living_space": True}, "electrical + water"),  # C3 via living
        ({"people_at_risk": {"elderly": True}, "water_rising": True, "location_type": "home"}, "elderly"),  # C4
        ({"people_at_risk": {"children": True}, "water_rising": True, "water_in_living_space": True}, "children"),
        ({"people_at_risk": {"disabled": True}, "water_rising": True, "location_type": "home"}, "disabled"),
    ],
)
def test_each_critical_trigger(overrides, first_reason):
    result = score_report(row(**overrides))
    assert result["urgency_level"] == "CRITICAL"
    assert result["urgency_reasons"][0] == first_reason
    assert_in_band(result)


def test_c4_chips_name_all_three_conditions():
    reasons = score_report(row(people_at_risk={"elderly": True}, water_rising=True, location_type="home"))["urgency_reasons"]
    assert reasons[:3] == ["elderly", "water rising", "living space"]


@pytest.mark.parametrize(
    "overrides, first_reason",
    [
        ({"water_depth_cm": 30, "location_type": "home"}, "30 cm water"),  # H1 at its boundary
        ({"water_depth_cm": 45, "water_in_living_space": True, "location_type": "basement"}, "45 cm water"),
        ({"hazards": {"sewage": True}}, "sewage"),  # H2
        ({"people_at_risk": {"elderly": True}}, "elderly"),  # H3
        ({"people_at_risk": {"children": True}}, "children"),
        ({"people_at_risk": {"disabled": True}}, "disabled"),
        ({"hazards": {"gas": True}}, "gas leak"),  # H4
        ({"hazards": {"structural": True}}, "structural damage"),
    ],
)
def test_each_high_trigger(overrides, first_reason):
    result = score_report(row(**overrides))
    assert result["urgency_level"] == "HIGH"
    assert result["urgency_reasons"][0] == first_reason
    assert_in_band(result)


@pytest.mark.parametrize(
    "overrides, first_reason",
    [
        ({"location_type": "basement"}, "basement"),  # M1, even with no depth given
        ({"water_in_living_space": True}, "living space"),  # M1
        ({"location_type": "home", "water_depth_cm": 10}, "living space"),  # M2 home
        ({"location_type": "car", "water_depth_cm": 20}, "in car"),  # M2 car
        ({"location_type": "car"}, "in car"),  # a car report counts as standing water
    ],
)
def test_each_medium_trigger(overrides, first_reason):
    result = score_report(row(**overrides))
    assert result["urgency_level"] == "MEDIUM"
    assert result["urgency_reasons"][0] == first_reason
    assert_in_band(result)


def test_low_street_flooding_and_info_only():
    street = score_report(row(location_type="street", water_depth_cm=25))
    assert street["urgency_level"] == "LOW"
    assert street["urgency_reasons"] == ["street flooding", "25 cm water"]
    info = score_report(row())
    assert info == {"urgency_score": 5, "urgency_level": "LOW", "urgency_reasons": ["info only"]}


def test_boundaries_just_below_triggers():
    assert level(row(water_depth_cm=29, location_type="home")) == "MEDIUM"  # H1 needs >= 30
    assert level(row(water_depth_cm=30, location_type="street")) == "LOW"  # deep, but not a living space
    # Electrical with no standing water is not C3, and no other rule fires.
    assert level(row(hazards={"electrical": True})) == "LOW"
    assert level(row(people_at_risk={"elderly": True}, water_rising=True)) == "HIGH"  # C4 needs a living space
    assert level(row(people_at_risk={"elderly": True}, location_type="home")) == "HIGH"  # C4 needs rising water


# ---------------------------------------------------------------- precedence and scores


def test_critical_beats_high():
    result = score_report(row(people_at_risk={"trapped": True}, hazards={"sewage": True, "gas": True},
                              water_depth_cm=80, location_type="home"))
    assert result["urgency_level"] == "CRITICAL"
    assert result["urgency_reasons"][0] == "trapped"
    assert "sewage" in result["urgency_reasons"]


def test_high_beats_medium():
    result = score_report(row(location_type="basement", hazards={"sewage": True}))
    assert result["urgency_level"] == "HIGH"
    assert result["urgency_reasons"][0] == "sewage"


def test_band_floors_and_ceilings():
    assert score_report(row(people_at_risk={"trapped": True}))["urgency_score"] == 80
    assert score_report(row(hazards={"gas": True}))["urgency_score"] == 63  # floor 60 + one hazard
    assert score_report(row(location_type="basement"))["urgency_score"] == 35
    assert score_report(row())["urgency_score"] == 5
    worst = row(
        people_at_risk={"trapped": True, "medical": True, "elderly": True, "children": True, "disabled": True, "count": 9},
        hazards={"electrical": True, "sewage": True, "gas": True, "structural": True},
        water_depth_cm=400, water_rising=True, water_in_living_space=True, location_type="basement",
    )
    assert score_report(worst)["urgency_score"] == 100
    big_low = score_report(row(location_type="street", water_depth_cm=500, water_rising=True,
                               people_at_risk={"count": 9}))
    assert big_low["urgency_level"] == "LOW" and big_low["urgency_score"] == 34


DANGER_STEPS = [
    {"water_depth_cm": 20},
    {"water_depth_cm": 60},
    {"water_depth_cm": 150},
    {"water_rising": True},
    {"water_in_living_space": True},
    {"hazards": {"electrical": True}},
    {"hazards": {"sewage": True}},
    {"hazards": {"gas": True}},
    {"hazards": {"structural": True}},
    {"people_at_risk": {"elderly": True}},
    {"people_at_risk": {"children": True}},
    {"people_at_risk": {"disabled": True}},
    {"people_at_risk": {"trapped": True}},
    {"people_at_risk": {"medical": True}},
    {"people_at_risk": {"count": 4}},
    {"needs": {"medical": True}},
]


def _merge(base, step):
    out = {**base}
    for key, value in step.items():
        if isinstance(value, dict):
            out[key] = {**out[key], **value}
        elif key == "water_depth_cm":
            out[key] = max(out.get(key) or 0, value)  # "adding danger" never makes water shallower
        else:
            out[key] = value
    return out


@pytest.mark.parametrize("ai_status", ["done", "failed"])
def test_monotonic_adding_danger_never_lowers_score(ai_status):
    # The location type is what the place *is*, not a danger to add, so each start fixes one.
    starts = [row(ai_status=ai_status, location_type=lt) for lt in (None, "other", "street", "car", "basement", "home")]
    starts.append(row(ai_status=ai_status, location_type="street", water_depth_cm=10))
    for start in starts:
        for a, b in itertools.product(DANGER_STEPS, repeat=2):
            before = _merge(start, a)
            after = _merge(before, b)
            s1, s2 = score_report(before), score_report(after)
            assert s2["urgency_score"] >= s1["urgency_score"], (before, after, s1, s2)


# ---------------------------------------------------------------- reasons


def test_reason_vocabulary_order_dedupe_and_limit():
    result = score_report(row(
        people_at_risk={"trapped": True, "medical": True, "elderly": True, "children": True, "count": 3},
        hazards={"electrical": True, "sewage": True},
        water_depth_cm=45, water_rising=True, location_type="home",
    ))
    reasons = result["urgency_reasons"]
    assert len(reasons) == 6
    assert len(set(reasons)) == len(reasons)
    assert all(is_vocab(r) for r in reasons), reasons
    assert all(r == r.lower() for r in reasons)
    # The CRITICAL triggers come first, in rule order.
    assert reasons[:3] == ["trapped", "medical emergency", "electrical + water"]


def test_people_count_chip_only_from_two():
    assert "1 people" not in score_report(row(location_type="basement", people_at_risk={"count": 1}))["urgency_reasons"]
    assert "3 people" in score_report(row(location_type="basement", people_at_risk={"count": 3}))["urgency_reasons"]


@pytest.mark.parametrize("depth, label", [(45, "45 cm water"), (5, "5 cm water"), (99, "99 cm water"),
                                          (100, "1.0 m water"), (120, "1.2 m water"), (155, "1.6 m water")])
def test_depth_formatting(depth, label):
    assert depth_label(depth) == label
    assert label in score_report(row(location_type="street", water_depth_cm=depth))["urgency_reasons"]


# ---------------------------------------------------------------- pending / failed / robustness


def test_pending_has_no_score():
    pending = row(ai_status="pending", people_at_risk={"trapped": True})
    assert score_report(pending) == {"urgency_score": None, "urgency_level": None, "urgency_reasons": []}


def test_failed_with_nothing_extracted_voice_and_text():
    bare = {"ai_status": "failed", "input_type": "voice"}
    assert score_report(bare) == {"urgency_score": 50, "urgency_level": "MEDIUM",
                                  "urgency_reasons": ["needs review", "voice note"]}
    typed = row(ai_status="failed", input_type="text")  # fallback extraction found nothing ("other", no flags)
    assert score_report(typed) == {"urgency_score": 50, "urgency_level": "MEDIUM",
                                   "urgency_reasons": ["needs review", "typed report"]}


def test_failed_never_says_info_only():
    """A failed report with a hazard the rules do not rank (electrical, but no water found) is shown
    as MEDIUM "needs review"; an "info only" chip next to that would read as a contradiction."""
    sparks = row(ai_status="failed", input_type="text", hazards={"electrical": True})
    result = score_report(sparks)
    assert result["urgency_level"] == "MEDIUM"
    assert result["urgency_reasons"] == ["needs review", "typed report"]
    voice = score_report(row(ai_status="failed", input_type="voice", hazards={"electrical": True}))
    assert voice["urgency_reasons"] == ["needs review", "voice note"]
    done = score_report(row(ai_status="done", hazards={"electrical": True}))
    assert done["urgency_reasons"] == ["info only"]  # a verified LOW report keeps it


def test_failed_puts_needs_review_first_and_never_below_medium():
    low = score_report(row(ai_status="failed", location_type="street", water_depth_cm=10))
    assert low["urgency_level"] == "MEDIUM"
    assert low["urgency_score"] >= 50
    assert low["urgency_reasons"][:2] == ["needs review", "street flooding"]

    critical = score_report(row(ai_status="failed", people_at_risk={"trapped": True}))
    assert critical["urgency_level"] == "CRITICAL"
    assert critical["urgency_reasons"][:2] == ["needs review", "trapped"]

    crowded = score_report(row(ai_status="failed", people_at_risk={"trapped": True, "medical": True, "elderly": True,
                                                                    "children": True, "disabled": True, "count": 5},
                               water_rising=True, water_depth_cm=80, location_type="home"))
    assert crowded["urgency_reasons"][0] == "needs review"
    assert len(crowded["urgency_reasons"]) == 6


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"ai_status": "done"},
        {"hazards": None, "people_at_risk": None, "needs": None, "water_depth_cm": None, "location_type": None},
        {"water_depth_cm": "abc", "people_at_risk": {"count": "two"}},
        {"water_depth_cm": "40", "location_type": "home"},
        {"hazards": '{"sewage": true}'},  # JSON text straight from SQLite
        {"people_at_risk": {"elderly": None, "count": None}},
    ],
)
def test_robust_to_missing_and_none(report):
    result = score_report(report)
    assert result["urgency_level"] in BANDS
    assert_in_band(result)
    assert all(is_vocab(r) for r in result["urgency_reasons"])


def test_string_inputs_are_understood():
    assert level({"water_depth_cm": "40", "location_type": "home"}) == "HIGH"
    assert level({"hazards": '{"sewage": true}'}) == "HIGH"


def test_pure_does_not_mutate_input():
    r = row(people_at_risk={"elderly": True}, water_depth_cm=40, location_type="home")
    snapshot = repr(r)
    score_report(r)
    assert repr(r) == snapshot
