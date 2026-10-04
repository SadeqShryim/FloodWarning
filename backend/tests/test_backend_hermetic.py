"""The suite must stay hermetic even after someone puts a real Gemini key in .env:
conftest.py hides the key from every test and blocks connections that leave the machine."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app import ai, config

FAKE_KEY = "AIza-not-a-real-key-for-the-hermetic-test"
BACKEND = Path(__file__).resolve().parents[1]


def test_key_is_hidden_inside_tests() -> None:
    assert config.GEMINI_API_KEY is None
    assert ai.ai_enabled() is False
    assert ai._client is None


@pytest.mark.real_gemini_key
def test_opting_in_sees_the_environment_key() -> None:
    if os.environ.get("GEMINI_API_KEY") != FAKE_KEY:
        pytest.skip("only meaningful inside test_a_key_in_the_environment_never_reaches_tests")
    assert config.GEMINI_API_KEY == FAKE_KEY


def test_network_is_blocked() -> None:
    with pytest.raises(OSError, match="network"):
        socket.getaddrinfo("generativelanguage.googleapis.com", 443)
    with socket.socket() as sock, pytest.raises(OSError, match="network"):
        sock.connect(("142.250.80.10", 443))


def test_a_key_in_the_environment_never_reaches_tests() -> None:
    """Run a few tests in a fresh interpreter whose environment carries a key, as .env would."""
    env = {**os.environ, "GEMINI_API_KEY": FAKE_KEY}
    here = Path(__file__).name
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"tests/{here}::test_key_is_hidden_inside_tests",
         f"tests/{here}::test_opting_in_sees_the_environment_key",
         "tests/test_api.py::test_health",
         "tests/test_briefing.py::test_build_briefing_without_ai_uses_rules"],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout
