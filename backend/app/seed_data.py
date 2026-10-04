"""The 25 demo reports spread across Dearborn.

STUB written by the orchestrator. The rules-data agent replaces the body; the signature is the contract.
"""
from __future__ import annotations

from datetime import datetime


def seed_rows(now: datetime) -> list[dict]:
    """Row dicts ready for db.insert_report (urgency is computed on insert). May carry a "seed_audio" filename."""
    return []
