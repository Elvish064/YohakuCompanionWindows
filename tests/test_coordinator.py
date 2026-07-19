from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from tests.helpers import DEVICE_ID
from yohaku_companion_windows import coordinator as coordinator_module
from yohaku_companion_windows.coordinator import LiveDeskCoordinator
from yohaku_companion_windows.domain import (
    ClearReason,
    ConnectionMetadata,
    PresenceConfiguration,
    RuntimeState,
    SanitizedApplicationPresence,
    SanitizedPresenceSnapshot,
)


class Store:
    def load_connection(self) -> ConnectionMetadata:
        return ConnectionMetadata("https://example.com", DEVICE_ID, (), 0, True)


class Credentials:
    async def get_token(self) -> str:
        return "token"


class Capture:
    def __init__(self) -> None:
        self.count = 0

    async def capture(self, include_media: bool = True) -> SanitizedPresenceSnapshot:
        del include_media
        self.count += 1
        return SanitizedPresenceSnapshot(
            datetime.now(UTC),
            SanitizedApplicationPresence(f"App {self.count}"),
            None,
        )

    def reset_media_continuity(self) -> None:
        return None


class Client:
    async def fetch_capabilities(self) -> Capabilities:
        return Capabilities()

    async def close(self) -> None:
        return None


class Capabilities:
    server_time = datetime.now(UTC)


class Writer:
    def __init__(self) -> None:
        self.snapshots: list[SanitizedPresenceSnapshot] = []
        self.first_started = asyncio.Event()
        self.second_sent = asyncio.Event()
        self.release_first = asyncio.Event()

    async def replace(self, snapshot: SanitizedPresenceSnapshot) -> None:
        self.snapshots.append(snapshot)
        if len(self.snapshots) == 1:
            self.first_started.set()
            await self.release_first.wait()
        elif len(self.snapshots) == 2:
            self.second_sent.set()

    async def clear(self, reason: ClearReason, observed_at: datetime) -> None:
        del reason, observed_at


@pytest.mark.asyncio
async def test_refresh_events_coalesce_while_send_is_in_flight(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    configuration = PresenceConfiguration(32768, 60_000, 30, 120, 60, 60, True)
    client = Client()
    writer = Writer()
    monkeypatch.setattr(
        coordinator_module, "CompanionHTTPClient", lambda server: client
    )
    monkeypatch.setattr(
        coordinator_module, "negotiate_presence", lambda capabilities, version: configuration
    )
    monkeypatch.setattr(
        coordinator_module, "validate_clock_skew", lambda server_time, maximum: None
    )
    monkeypatch.setattr(
        coordinator_module,
        "PresenceWriter",
        lambda metadata, token, config, store, http_client: writer,
    )
    states: list[RuntimeState] = []
    published: list[SanitizedPresenceSnapshot] = []
    coordinator = LiveDeskCoordinator(
        Store(),
        Credentials(),
        Capture(),
        states.append,
        published.append,  # type: ignore[arg-type]
    )
    await coordinator.start()
    await asyncio.wait_for(writer.first_started.wait(), 1)
    await coordinator.request_refresh()
    await coordinator.request_refresh()
    await coordinator.request_refresh()
    writer.release_first.set()
    await asyncio.wait_for(writer.second_sent.wait(), 1)
    await asyncio.sleep(0.05)
    assert len(writer.snapshots) == 2
    assert published == writer.snapshots
    assert RuntimeState.ACTIVE in states
    await coordinator.stop(ClearReason.PAUSED, RuntimeState.DISABLED)
