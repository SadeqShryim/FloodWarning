"""Generate the seed voice notes with Microsoft Edge's online text-to-speech (edge-tts).

Writes backend/seed_audio/seed_<nn>_<lang>.mp3 for every seed that names a `seed_audio` file in
app/seed_data.py. The spoken text is exactly the seed's transcript_original, so the dashboard's
transcript matches what judges hear. Needs internet; the mp3s are committed, so this only reruns
when the seed texts change.

Run from the repo root:  .venv\\Scripts\\python.exe backend\\scripts\\make_seed_audio.py [--force]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import edge_tts  # noqa: E402

from app import config  # noqa: E402
from app.seed_data import seed_audio_texts  # noqa: E402

# One voice per file, matched to the dialect of the transcript (Lebanese, Iraqi, Mexican Spanish...).
VOICES = {
    "seed_01_ar.mp3": "ar-LB-LaylaNeural",  # Lebanese: daughter calling about her mother
    "seed_02_en.mp3": "en-US-GuyNeural",  # driver trapped in his car
    "seed_03_es.mp3": "es-MX-DaliaNeural",  # daughter calling about her dad's oxygen
    "seed_06_ar.mp3": "ar-IQ-BasselNeural",  # Iraqi: sewage in the basement
    "seed_13_en.mp3": "en-US-JennyNeural",  # sump pump failed
    "seed_21_es.mp3": "es-MX-JorgeNeural",  # clogged storm drains
}
DEFAULT_VOICE = {"ar": "ar-LB-RamiNeural", "en": "en-US-JennyNeural", "es": "es-MX-DaliaNeural"}
MAX_BYTES = 150 * 1024


async def make_one(path: Path, text: str, voice: str) -> int:
    # Slightly faster than default: real people on the phone in a flood talk quickly.
    await edge_tts.Communicate(text, voice, rate="+5%").save(str(path))
    return path.stat().st_size


async def main(force: bool) -> int:
    out_dir = Path(config.SEED_AUDIO_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for name, (lang, text) in seed_audio_texts().items():
        path = out_dir / name
        if path.exists() and not force:
            print(f"skip {name} (exists; --force to regenerate)")
            continue
        voice = VOICES.get(name, DEFAULT_VOICE.get(lang, "en-US-JennyNeural"))
        try:
            size = await make_one(path, text, voice)
        except Exception as exc:  # network down, voice retired...: the demo still works without audio
            failures += 1
            print(f"FAILED {name} with {voice}: {exc!r}")
            continue
        flag = "" if size <= MAX_BYTES else "  (over 150 KB!)"
        print(f"wrote {name}  {voice}  {size / 1024:.0f} KB{flag}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="regenerate files that already exist")
    sys.exit(asyncio.run(main(parser.parse_args().force)))
