from __future__ import annotations

from pytest import MonkeyPatch

from yohaku_companion_windows.domain import RawApplicationIdentity
from yohaku_companion_windows.win32_capture import Win32ApplicationProvider


class FakeUser32:
    def GetForegroundWindow(self) -> int:
        return 100

    def GetWindow(self, window: object, command: int) -> int:
        assert command == Win32ApplicationProvider.GW_HWNDNEXT
        value = int(getattr(window, "value", window) or 0)
        return {100: 90, 90: 80}.get(value, 0)

    def IsWindowVisible(self, window: object) -> bool:
        value = int(getattr(window, "value", window) or 0)
        return value != 90


def test_preview_uses_visible_external_window_behind_own_settings(
    monkeypatch: MonkeyPatch,
) -> None:
    provider = object.__new__(Win32ApplicationProvider)
    provider._own_process_id = 10
    provider._user32 = FakeUser32()

    monkeypatch.setattr(
        Win32ApplicationProvider,
        "_window_process_id",
        lambda self, window: {100: 10, 90: 20, 80: 30}.get(window, 0),
    )
    expected = RawApplicationIdentity("win32:browser.exe", "Browser", 80)
    monkeypatch.setattr(
        Win32ApplicationProvider,
        "_application_for_window",
        lambda self, window: expected if window == 80 else None,
    )

    assert provider.current_application() == expected
