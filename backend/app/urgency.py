"""Deterministic, explainable urgency ranking on top of the AI fields.

STUB written by the orchestrator. The rules-data agent replaces the body; the signature is the contract.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def score_report(report: Mapping[str, Any]) -> dict:
    """Return {"urgency_score": int | None, "urgency_level": str | None, "urgency_reasons": list[str]}.

    Pending reports (ai_status == "pending") get None/None/[].
    """
    if report.get("ai_status") == "pending":
        return {"urgency_score": None, "urgency_level": None, "urgency_reasons": []}
    return {"urgency_score": 50, "urgency_level": "MEDIUM", "urgency_reasons": []}
