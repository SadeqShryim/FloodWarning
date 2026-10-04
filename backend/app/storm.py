"""Storm mode: injects a realistic new report every few seconds so the ranking reshuffles live.

STUB written by the orchestrator. The rules-data agent replaces the body; the interface is the contract.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

AddReport = Callable[[dict], Awaitable[dict]]  # insert a row dict, publish it, return the stored row (with id)
ApplyUpdate = Callable[[int, dict], Awaitable[dict]]  # update fields, recompute urgency, publish, return the row
OnState = Callable[[bool, int], Awaitable[None]]  # (running, injected) after every change


class StormController:
    def __init__(self, add_report: AddReport, apply_update: ApplyUpdate, on_state: OnState) -> None:
        self._add_report = add_report
        self._apply_update = apply_update
        self._on_state = on_state
        self._running = False
        self._injected = 0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def injected(self) -> int:
        return self._injected

    def start(self, *, max_reports: int = 24, min_interval_s: float = 2.0, max_interval_s: float = 4.5) -> None:
        """Start injecting (no-op if already running). Must be called from inside the event loop."""

    async def stop(self) -> None:
        """Stop injecting (no-op if not running)."""
