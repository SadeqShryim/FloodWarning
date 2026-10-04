"""Regenerate the sample voice notes in this folder (dev tool; needs network for edge-tts).

    ..\\.venv\\Scripts\\python.exe tests\\fixtures\\make_fixtures.py      (from backend/)

Writes three mp3 files with edge-tts, one 16 kHz mono 16-bit WAV with Windows SAPI (the format the
phone recorder sends), and voice_notes.json describing what each note says and what we expect.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

NOTES = [
    {
        "file": "ar_grandma_basement.mp3",
        "voice": "ar-LB-LaylaNeural",
        "text": (
            "ألو، الله يخليكن ساعدونا. جدتي بالبيسمنت، والمي وصلت لركبها وعم تطلع كل شوي. "
            "هي كبيرة بالعمر وما فيها تطلع الدرج لحالها. نحنا قريبين من وارن وشيفر."
        ),
        "english": (
            "Hello, please help us. My grandmother is in the basement, the water has reached her knees and keeps "
            "rising. She is elderly and cannot climb the stairs by herself. We are near Warren and Schaefer."
        ),
        "expected_language": "ar",
        "expected": {
            "location_type": "basement",
            "water_depth_cm": 50,
            "water_rising": True,
            "people_at_risk.elderly": True,
            "people_at_risk.trapped": True,
            "location_hint_contains": ["Warren", "Schaefer"],
        },
        "expected_level": "CRITICAL",
    },
    {
        "file": "en_panel_kids.mp3",
        "voice": "en-US-JennyNeural",
        "text": (
            "Hi, I need help. We have about two feet of water in our basement and it's touching the electrical "
            "panel. There's a buzzing sound coming from it. My two kids are upstairs."
        ),
        "english": None,
        "expected_language": "en",
        "expected": {
            "location_type": "basement",
            "water_depth_cm": 61,
            "hazards.electrical": True,
            "people_at_risk.children": True,
            "people_at_risk.count": 2,
        },
        "expected_level": "CRITICAL",
    },
    {
        "file": "es_street.mp3",
        "voice": "es-MX-DaliaNeural",
        "text": (
            "Hola, quiero reportar que la avenida Dix cerca de Vernor está inundada. El agua llega a la mitad de "
            "las llantas de los carros. No hay nadie herido."
        ),
        "english": (
            "Hello, I want to report that Dix Avenue near Vernor is flooded. The water reaches halfway up the cars' "
            "tires. Nobody is hurt."
        ),
        "expected_language": "es",
        "expected": {
            "location_type": "street",
            "water_depth_cm": 30,
            "people_at_risk.medical": False,
            "location_hint_contains": ["Dix", "Vernor"],
        },
        "expected_level": "LOW",
    },
]

WAV = {
    "file": "en_panel_kids.wav",
    "voice": "Windows SAPI (System.Speech default voice), 16 kHz mono 16-bit PCM",
    "same_as": "en_panel_kids.mp3",
}

_SAPI = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.Rate = 1
$s.SetOutputToWaveFile($env:FL_WAV_PATH, $fmt)
$s.Speak($env:FL_WAV_TEXT)
$s.Dispose()
"""


async def _tts(note: dict) -> None:
    import edge_tts

    out = HERE / note["file"]
    await edge_tts.Communicate(note["text"], note["voice"]).save(str(out))
    print(f"{out.name}: {out.stat().st_size // 1024} KB")


def _sapi_wav(text: str) -> None:
    import os

    out = HERE / WAV["file"]
    env = dict(os.environ, FL_WAV_PATH=str(out), FL_WAV_TEXT=text)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _SAPI], check=True, env=env)
    print(f"{out.name}: {out.stat().st_size // 1024} KB")


def main() -> int:
    for note in NOTES:
        asyncio.run(_tts(note))
    _sapi_wav(NOTES[1]["text"])
    manifest = {"notes": NOTES, "wav": {**WAV, "text": NOTES[1]["text"]}}
    (HERE / "voice_notes.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("voice_notes.json written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
