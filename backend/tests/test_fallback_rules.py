"""Keyword fallback in English, Arabic and Spanish, including the three fixture voice notes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.fallback_rules import detect_language, extract_depth_cm, extract_from_text, failed_audio_fields

MANIFEST = json.loads((Path(__file__).parent / "fixtures" / "voice_notes.json").read_text(encoding="utf-8"))


def _get(extraction, dotted: str):
    value = extraction
    for part in dotted.split("."):
        value = getattr(value, part)
    return value


@pytest.mark.parametrize("note", MANIFEST["notes"], ids=lambda n: n["file"])
def test_fixture_texts_give_the_expected_fields(note):
    ex = extract_from_text(note["text"], ui_language="en")
    assert ex.language == note["expected_language"]
    for key, expected in note["expected"].items():
        if key == "location_hint_contains":
            for word in expected:
                assert word in (ex.location_hint or ""), (word, ex.location_hint)
        else:
            assert _get(ex, key) == expected, key


def test_arabic_fixture_is_critical_material():
    note = MANIFEST["notes"][0]
    ex = extract_from_text(note["text"], ui_language="ar")
    assert ex.people_at_risk.trapped and ex.people_at_risk.elderly
    assert ex.water_rising and ex.water_in_living_space
    assert ex.hazards.electrical is False  # "تقاطع" (intersection) must not read as "قاطع" (breaker)
    assert ex.transcript_english.startswith("[not translated - AI offline] ")
    assert "Elderly" in ex.ai_summary and "basement" in ex.ai_summary
    assert "911" in ex.confirmation_message and "وصلنا بلاغك" in ex.confirmation_message


def test_english_fixture_has_electrical_hazard_and_english_transcript():
    note = MANIFEST["notes"][1]
    ex = extract_from_text(note["text"])
    assert ex.hazards.electrical
    assert ex.transcript_english == ex.transcript_original
    assert ex.confirmation_message.startswith("We received your report")


def test_spanish_negation_nobody_hurt():
    ex = extract_from_text("La calle está inundada, no hay nadie herido.")
    assert ex.language == "es"
    assert ex.people_at_risk.medical is False
    assert ex.confirmation_message.startswith("Recibimos su reporte")


@pytest.mark.parametrize("text, field", [
    ("We are trapped on the second floor", "people_at_risk.trapped"),
    ("my grandpa can't get out of the basement", "people_at_risk.trapped"),
    ("محاصرين بالبيت", "people_at_risk.trapped"),
    ("ما فينا نطلع من البيت", "people_at_risk.trapped"),
    ("estamos atrapados, no podemos salir", "people_at_risk.trapped"),
    ("Grandma is downstairs", "people_at_risk.elderly"),
    ("جدي كبير بالعمر", "people_at_risk.elderly"),
    ("mi abuela está sola", "people_at_risk.elderly"),
    ("there is a baby here", "people_at_risk.children"),
    ("الأطفال بالبيت", "people_at_risk.children"),
    ("tengo un bebé", "people_at_risk.children"),
    ("my son uses a wheelchair", "people_at_risk.disabled"),
    ("أبي على كرسي متحرك", "people_at_risk.disabled"),
    ("mi papá usa silla de ruedas", "people_at_risk.disabled"),
    ("my mom is on oxygen", "people_at_risk.medical"),
    ("he has chest pain", "people_at_risk.medical"),
    ("إمي على الأكسجين", "people_at_risk.medical"),
    ("mi esposo necesita diálisis", "people_at_risk.medical"),
    ("water is near the outlet and I see sparks", "hazards.electrical"),
    ("المي وصلت للكهربا", "hazards.electrical"),
    ("hay chispas y cables en el agua", "hazards.electrical"),
    ("sewer backed up into the basement", "hazards.sewage"),
    ("في مجاري بالقبو", "hazards.sewage"),
    ("salen aguas negras del drenaje", "hazards.sewage"),
    ("I smell gas", "hazards.gas"),
    ("في ريحة غاز", "hazards.gas"),
    ("hay olor a gas", "hazards.gas"),
    ("the wall has a big crack", "hazards.structural"),
    ("the water keeps rising", "water_rising"),
    ("المي عم يطلع", "water_rising"),
    ("el agua sube rápido", "water_rising"),
])
def test_concepts_in_three_languages(text, field):
    assert _get(extract_from_text(text), field) is True


@pytest.mark.parametrize("text", [
    "nobody is hurt, just a wet street",
    "ما في حدا مصاب",
    "nadie está herido",
])
def test_negated_medical(text):
    assert extract_from_text(text).people_at_risk.medical is False


@pytest.mark.parametrize("text, place", [
    ("water in the basement", "basement"),
    ("المي بالقبو", "basement"),
    ("el sótano está lleno de agua", "basement"),
    ("we are stuck in my car on the underpass", "car"),
    ("estamos en el carro", "car"),
    ("علقنا بالسيارة", "car"),
    ("the street is flooded", "street"),
    ("الشارع غرقان", "street"),
    ("la calle está inundada", "street"),
    ("water in the living room", "home"),
    ("something happened", "other"),
])
def test_location_type(text, place):
    assert extract_from_text(text).location_type == place


@pytest.mark.parametrize("text, cm", [
    ("2 feet of water", 61),
    ("about two feet", 61),
    ("30 cm", 30),
    ("6 inches", 15),
    ("knee-deep water", 50),
    ("up to my waist", 100),
    ("المي للركبة", 50),
    ("المي وصلت للخصر", 100),
    ("٣٠ سانتي", 30),
    ("hasta la rodilla", 50),
    ("el agua llega a la cintura", 100),
    ("medio metro de agua", 50),
    ("no water depth here", None),
])
def test_depth(text, cm):
    assert extract_depth_cm(text) == cm


def test_language_detection_and_ui_tie_break():
    assert detect_language("المي عم تطلع") == "ar"
    assert detect_language("el agua está en la casa") == "es"
    assert detect_language("the water is in the house") == "en"
    assert detect_language("Dix Vernor", "es") == "es"  # nothing to go on: the UI language decides
    assert detect_language("Dix Vernor", "ar") == "en"  # Latin text cannot be Arabic


def test_failed_audio_fields_in_three_languages():
    en, ar, es = (failed_audio_fields(lang) for lang in ("en", "ar", "es"))
    assert en["ai_summary"] == ar["ai_summary"] == es["ai_summary"]  # always English for responders
    assert "911" in en["confirmation_message"] and "911" in ar["confirmation_message"] and "911" in es["confirmation_message"]
    assert "صوتية" in ar["confirmation_message"]
    assert "nota de voz" in es["confirmation_message"]
    assert failed_audio_fields("fr") == en


def test_never_raises_on_odd_input():
    for text in ["", "   ", "!!!", "1" * 5000, "🙂🌊"]:
        ex = extract_from_text(text, "zz")
        assert ex.language in ("en", "ar", "es")
