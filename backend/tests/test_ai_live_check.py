"""scripts/live_ai_check.py run offline against a fake Gemini, so the one-command check works the
day the key arrives. Also walks the three fixture voice notes through parsing and the urgency rules
with the answers a correct model gives: the Arabic grandmother must come out CRITICAL."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import ai, config, urgency

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_ai_check.py"

# What a correct model returns for each fixture (written from the fixture texts in voice_notes.json).
ANSWERS = {
    "ar": {
        "transcript_original": "ألو، الله يخليكن ساعدونا. جدتي بالبيسمنت، والمي وصلت لركبها وعم تطلع كل شوي. هي كبيرة "
                               "بالعمر وما فيها تطلع الدرج لحالها. نحنا قريبين من وارن وشيفر.",
        "language": "ar",
        "transcript_english": "Hello, please help us. My grandmother is in the basement, the water has reached her "
                              "knees and keeps rising. She is elderly and cannot climb the stairs by herself. We are "
                              "near Warren and Schaefer.",
        "ai_summary": "Elderly woman trapped in basement, knee-deep water rising, near Warren and Schaefer",
        "confirmation_message": "وصلنا بلاغك: جدتك بالبيسمنت والمي لركبها وعم تطلع، قرب وارن وشيفر. إذا في خطر على "
                                "الحياة اتصل بـ 911.",
        "water_depth_cm": 50, "location_type": "basement", "location_hint": "Warren Ave and Schaefer Rd",
        "water_in_living_space": True, "water_rising": True,
        "hazards": {"electrical": False, "sewage": False, "gas": False, "structural": False},
        "people_at_risk": {"elderly": True, "children": False, "disabled": False, "medical": False,
                           "trapped": True, "count": 1},
        "needs": {"evacuation": True, "pumping": False, "medical": False, "supplies": False},
    },
    "en": {
        "transcript_original": "Hi, I need help. We have about two feet of water in our basement and it's touching "
                               "the electrical panel. There's a buzzing sound coming from it. My two kids are upstairs.",
        "language": "en",
        "transcript_english": "Hi, I need help. We have about two feet of water in our basement and it's touching "
                              "the electrical panel. There's a buzzing sound coming from it. My two kids are upstairs.",
        "ai_summary": "Two feet of basement water touching electrical panel, buzzing; two kids upstairs",
        "confirmation_message": "We got your report: about two feet of water touching your electrical panel, two "
                                "kids upstairs. If a life is in danger, call 911.",
        "water_depth_cm": 61, "location_type": "basement", "location_hint": None,
        "water_in_living_space": False, "water_rising": False,
        "hazards": {"electrical": True, "sewage": False, "gas": False, "structural": False},
        "people_at_risk": {"elderly": False, "children": True, "disabled": False, "medical": False,
                           "trapped": False, "count": 2},
        "needs": {"evacuation": False, "pumping": True, "medical": False, "supplies": False},
    },
    "es": {
        "transcript_original": "Hola, quiero reportar que la avenida Dix cerca de Vernor está inundada. El agua llega "
                               "a la mitad de las llantas de los carros. No hay nadie herido.",
        "language": "es",
        "transcript_english": "Hello, I want to report that Dix Avenue near Vernor is flooded. The water reaches "
                              "halfway up the cars' tires. Nobody is hurt.",
        "ai_summary": "Dix Ave near Vernor flooded, water halfway up car tires, no injuries",
        "confirmation_message": "Recibimos su reporte: la avenida Dix cerca de Vernor está inundada, el agua a media "
                                "llanta. Si hay una vida en peligro, llame al 911.",
        "water_depth_cm": 30, "location_type": "street", "location_hint": "Dix Ave and Vernor Hwy",
        "water_in_living_space": False, "water_rising": False,
        "hazards": {"electrical": False, "sewage": False, "gas": False, "structural": False},
        "people_at_risk": {"elderly": False, "children": False, "disabled": False, "medical": False,
                           "trapped": False, "count": None},
        "needs": {"evacuation": False, "pumping": False, "medical": False, "supplies": False},
    },
}
NOTES = json.loads((Path(__file__).parent / "fixtures" / "voice_notes.json").read_text(encoding="utf-8"))["notes"]


@pytest.mark.parametrize("note", NOTES, ids=[n["file"] for n in NOTES])
def test_fixture_scenarios_rank_as_expected(note):
    extraction = ai.parse_extraction(json.dumps(ANSWERS[note["expected_language"]], ensure_ascii=False),
                                     ui_language=note["expected_language"])
    row = {**extraction.model_dump(), "ai_status": "done"}
    assert urgency.score_report(row)["urgency_level"] == note["expected_level"]
    assert extraction.language == note["expected_language"]
    assert len(extraction.ai_summary.split()) <= 15


def test_prompt_covers_what_the_grandmother_case_needs():
    p = ai.SYSTEM_PROMPT
    assert "cannot climb the stairs" in p  # -> trapped
    assert "basement where someone is" in p  # -> water_in_living_space
    assert "keeps going up" in p  # "عم تطلع كل شوي" -> water_rising
    assert "SAME language and script as transcript_original" in p
    assert "Schaefer" in p and "وارن وشيفر" in p  # map spelling for the spoken place
    assert "911" in p and "arrival time" in p


def _load_script():
    spec = importlib.util.spec_from_file_location("live_ai_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeModels:
    def __init__(self):
        self.calls: list[str] = []

    async def generate_content(self, *, model, contents, config):  # noqa: A002
        self.calls.append(model)
        if config.response_json_schema is None:
            return SimpleNamespace(text="Worst is #1 at Warren Ave & Schaefer Rd: elderly woman trapped. Send a crew.")
        lang = "ar" if "Arabic" in contents[-1] else "es" if "Spanish" in contents[-1] else "en"
        return SimpleNamespace(text=json.dumps(ANSWERS[lang], ensure_ascii=False))


@pytest.fixture
def fake_gemini(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", ["gemini-flash-latest"])
    for name, value in (("_last_model", None), ("_unavailable", set()), ("_cooldown", {}), ("_thinking_step", {}),
                        ("_state_key", "test-key")):
        monkeypatch.setattr(ai, name, value)
    models = FakeModels()
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    monkeypatch.setattr(ai, "_get_client", lambda: client)
    monkeypatch.setattr(ai, "_client", None)
    return models


def run_script(monkeypatch, *argv):
    module = _load_script()
    monkeypatch.setattr(sys, "argv", ["live_ai_check.py", *argv])
    return asyncio.run(module.main())


def test_live_check_default_is_four_calls_and_passes(fake_gemini, monkeypatch, capsys):
    assert run_script(monkeypatch) == 0
    out = capsys.readouterr().out
    assert len(fake_gemini.calls) == 4  # 3 voice notes + the sitrep
    assert out.count("model:        gemini-2.5-flash") == 3
    assert "CRITICAL" in out and "FAIL" not in out
    assert "PASS  confirmation in the reporter's language (ar, expected ar)" in out
    assert "english:      Hello, please help us." in out
    assert "RESULT: all checks passed" in out


def test_live_check_respects_max_calls_and_model(fake_gemini, monkeypatch, capsys):
    assert run_script(monkeypatch, "--max-calls", "1", "--model", "gemini-flash-latest") == 0
    out = capsys.readouterr().out
    assert fake_gemini.calls == ["gemini-flash-latest"]
    assert "ar_grandma_basement.mp3" in out and "skipping en_panel_kids.mp3" in out


def test_live_check_flags_a_confirmation_in_the_wrong_language(fake_gemini, monkeypatch, capsys):
    wrong = dict(ANSWERS["ar"], confirmation_message="We received your report. If a life is in danger, call 911.")
    monkeypatch.setitem(ANSWERS, "ar", wrong)
    assert run_script(monkeypatch, "--max-calls", "1") == 1
    assert "FAIL  confirmation in the reporter's language (en, expected ar)" in capsys.readouterr().out


def test_live_check_without_key_sends_nothing(monkeypatch, capsys):
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    assert run_script(monkeypatch) == 2
    assert "no Gemini calls made" in capsys.readouterr().out
