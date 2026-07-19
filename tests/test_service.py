from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import DEVICE_ID
from yohaku_companion_windows.capture import PresenceCapture
from yohaku_companion_windows.domain import (
    ClearReason,
    ConnectionMetadata,
    PrivacyDefaults,
    RawApplicationIdentity,
    RuntimeState,
    SourceSettings,
)
from yohaku_companion_windows.service import ApplicationService, ServiceError
from yohaku_companion_windows.storage import StateStore


class Credentials:
    def __init__(self, token: str | None = "token") -> None:
        self.token = token

    async def ensure_available(self) -> None:
        return None

    async def get_token(self) -> str | None:
        return self.token

    async def set_token(self, value: str) -> None:
        self.token = value

    async def delete_token(self) -> None:
        self.token = None


class Applications:
    def current_application(self) -> RawApplicationIdentity:
        return RawApplicationIdentity("win32:editor.exe", "Editor", 1)

    def read_window_title(self, window_handle: int) -> str:
        return "Document"


class Media:
    available = False

    async def start(self, on_change=None) -> bool:  # type: ignore[no-untyped-def]
        return False

    async def current_media(self):  # type: ignore[no-untyped-def]
        return None

    async def stop(self) -> None:
        return None


class Coordinator:
    def __init__(self) -> None:
        self.started = 0
        self.stops: list[tuple[ClearReason, RuntimeState]] = []

    async def start(self) -> None:
        self.started += 1

    async def stop(self, reason: ClearReason, state: RuntimeState) -> None:
        self.stops.append((reason, state))

    async def suspend(self) -> None:
        return None

    async def resume(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def make_service(tmp_path: Path, token: str | None = "token") -> ApplicationService:
    store = StateStore(tmp_path / "state.sqlite3")
    store.save_connection(
        ConnectionMetadata("https://example.com", DEVICE_ID, (), 0, False)
    )
    media = Media()
    service = ApplicationService(
        store,
        Credentials(token),
        PresenceCapture(store, Applications(), media),
        media,
    )
    service.coordinator = Coordinator()  # type: ignore[assignment]
    return service


@pytest.mark.asyncio
async def test_enable_requires_current_preview_and_lock_monitor(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(ServiceError):
        await service.enable_live_desk()
    service.set_lifecycle_available(True)
    with pytest.raises(ServiceError):
        await service.enable_live_desk()
    await service.refresh_preview()
    await service.enable_live_desk()
    assert service.store.load_connection().live_desk_enabled  # type: ignore[union-attr]
    assert service.coordinator.started == 1  # type: ignore[attr-defined]
    await service.shutdown()


@pytest.mark.asyncio
async def test_privacy_change_clears_and_invalidates_preview(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.set_lifecycle_available(True)
    await service.refresh_preview()
    await service.enable_live_desk()
    await service.save_privacy(SourceSettings(), PrivacyDefaults(), ())
    assert not service.state.preview_current
    assert not service.store.load_connection().live_desk_enabled  # type: ignore[union-attr]
    assert (
        service.coordinator.stops[-1][0]  # type: ignore[attr-defined]
        is ClearReason.PRIVACY_CHANGED
    )
    await service.shutdown()


@pytest.mark.asyncio
async def test_missing_credential_fails_closed(tmp_path: Path) -> None:
    service = make_service(tmp_path, token=None)
    service.set_lifecycle_available(True)
    await service.refresh_preview()
    with pytest.raises(ServiceError):
        await service.enable_live_desk()
    await service.shutdown()
