from __future__ import annotations

import asyncio
import time
from datetime import datetime

import pytest

from yohaku_companion_windows.coordinator import LiveDeskCoordinator
from yohaku_companion_windows.domain import ClearReason, RuntimeState


class SlowWriter:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def clear(self, reason: ClearReason, observed_at: datetime) -> None:
        del reason, observed_at
        self.started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class Capture:
    def reset_media_continuity(self) -> None:
        return None


@pytest.mark.asyncio
async def test_clear_is_bounded_to_500_milliseconds() -> None:
    coordinator = LiveDeskCoordinator(
        None, None, Capture(), lambda state: None  # type: ignore[arg-type]
    )
    writer = SlowWriter()
    coordinator._writer = writer  # type: ignore[assignment]
    started = time.monotonic()
    await coordinator.stop(ClearReason.SHUTDOWN, RuntimeState.DISABLED)
    elapsed = time.monotonic() - started
    assert elapsed < 0.8
    assert writer.cancelled


class OrderedWriter:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def clear(self, reason: ClearReason, observed_at: datetime) -> None:
        del reason, observed_at
        self.events.append("clear-start")
        await asyncio.sleep(0.05)
        self.events.append("clear-end")


@pytest.mark.asyncio
async def test_resume_joins_old_clear_before_start() -> None:
    events: list[str] = []
    coordinator = LiveDeskCoordinator(
        None, None, Capture(), lambda state: None  # type: ignore[arg-type]
    )
    coordinator._writer = OrderedWriter(events)  # type: ignore[assignment]

    async def start_locked() -> None:
        events.append("restart")

    coordinator._start_locked = start_locked  # type: ignore[method-assign]
    suspend = asyncio.create_task(coordinator.suspend())
    await asyncio.sleep(0)
    resume = asyncio.create_task(coordinator.resume())
    await asyncio.gather(suspend, resume)
    assert events == ["clear-start", "clear-end", "restart"]
