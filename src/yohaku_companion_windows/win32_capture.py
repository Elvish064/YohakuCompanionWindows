from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

from .domain import RawApplicationIdentity, normalize_text


class ApplicationProvider(Protocol):
    def current_application(self) -> RawApplicationIdentity | None: ...

    def read_window_title(self, window_handle: int) -> str | None: ...


class Win32ApplicationProvider:
    """Reads foreground identity without reading a window title as a side effect."""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INSUFFICIENT_BUFFER = 122
    GW_HWNDNEXT = 2
    MAXIMUM_Z_ORDER_SCAN = 64

    def __init__(self, own_process_id: int | None = None) -> None:
        if os.name != "nt":
            raise OSError("Win32 foreground capture requires Windows")
        self._own_process_id = own_process_id or os.getpid()
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def current_application(self) -> RawApplicationIdentity | None:
        window = int(self._user32.GetForegroundWindow())
        if window == 0:
            return None
        if self._window_process_id(window) != self._own_process_id:
            return self._application_for_window(window)

        # The settings window becomes foreground when preview is requested.
        # Resolve the first visible external window behind it so the preview
        # represents what the user was doing before opening Companion.
        candidate = window
        for _ in range(self.MAXIMUM_Z_ORDER_SCAN):
            candidate = int(
                self._user32.GetWindow(
                    wintypes.HWND(candidate), self.GW_HWNDNEXT
                )
                or 0
            )
            if candidate == 0:
                return None
            if not self._user32.IsWindowVisible(wintypes.HWND(candidate)):
                continue
            application = self._application_for_window(candidate)
            if application is not None:
                return application
        return None

    def _application_for_window(self, window: int) -> RawApplicationIdentity | None:
        process_id = self._window_process_id(window)
        if process_id == 0 or process_id == self._own_process_id:
            return None

        process = self._kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            process_id,
        )
        if not process:
            return None
        try:
            executable_path = self._query_process_image(process)
            app_user_model_id = self._query_app_user_model_id(process)
        finally:
            self._kernel32.CloseHandle(process)

        if app_user_model_id:
            identifier = f"aumid:{app_user_model_id.casefold()}"
        elif executable_path:
            identifier = f"win32:{Path(executable_path).name.casefold()}"
        else:
            return None
        display_name = self._friendly_name(executable_path, app_user_model_id)
        return RawApplicationIdentity(identifier, display_name, window)

    def _window_process_id(self, window: int) -> int:
        process_id = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(
            wintypes.HWND(window), ctypes.byref(process_id)
        )
        return int(process_id.value)

    def read_window_title(self, window_handle: int) -> str | None:
        # This is deliberately the only GetWindowTextW call in the project. The
        # caller must first pass both global and per-application title policy.
        length = int(self._user32.GetWindowTextLengthW(wintypes.HWND(window_handle)))
        if length <= 0:
            return None
        buffer = ctypes.create_unicode_buffer(min(length + 1, 4096))
        copied = self._user32.GetWindowTextW(
            wintypes.HWND(window_handle), buffer, len(buffer)
        )
        return normalize_text(buffer.value[:copied], 500) if copied > 0 else None

    def _configure_signatures(self) -> None:
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self._user32.GetWindow.restype = wintypes.HWND
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]

    def _query_process_image(self, process: int) -> str | None:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not self._kernel32.QueryFullProcessImageNameW(
            process, 0, buffer, ctypes.byref(size)
        ):
            return None
        return buffer.value[: size.value]

    def _query_app_user_model_id(self, process: int) -> str | None:
        length = wintypes.UINT(0)
        function = getattr(self._kernel32, "GetApplicationUserModelId", None)
        if function is None:
            return None
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        function.restype = wintypes.LONG
        result = function(process, ctypes.byref(length), None)
        if result != self.ERROR_INSUFFICIENT_BUFFER or length.value == 0:
            return None
        buffer = ctypes.create_unicode_buffer(length.value)
        if function(process, ctypes.byref(length), buffer) != 0:
            return None
        return normalize_text(buffer.value, 260)

    @staticmethod
    def _friendly_name(path: str | None, app_user_model_id: str | None) -> str:
        if path:
            description = _file_description(path)
            if description:
                return description
            stem = normalize_text(Path(path).stem, 120)
            if stem:
                return stem
        if app_user_model_id:
            tail = app_user_model_id.rsplit("!", 1)[-1].rsplit(".", 1)[-1]
            return normalize_text(tail, 120) or "Windows 应用"
        return "Windows 应用"


def _file_description(path: str) -> str | None:
    """Reads the executable's localized FileDescription without persisting its path."""
    try:
        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        version.VerQueryValueW.restype = wintypes.BOOL
        size = version.GetFileVersionInfoSizeW(path, None)
        if size <= 0:
            return None
        buffer = (ctypes.c_byte * size)()
        if not version.GetFileVersionInfoW(path, 0, size, ctypes.byref(buffer)):
            return None
        translation_pointer = ctypes.c_void_p()
        translation_length = wintypes.UINT()
        if not version.VerQueryValueW(
            ctypes.byref(buffer),
            r"\VarFileInfo\Translation",
            ctypes.byref(translation_pointer),
            ctypes.byref(translation_length),
        ):
            return None
        if translation_length.value < 4:
            return None
        translation = ctypes.cast(
            translation_pointer, ctypes.POINTER(ctypes.c_ushort)
        )
        query = (
            rf"\StringFileInfo\{translation[0]:04x}{translation[1]:04x}"
            r"\FileDescription"
        )
        value_pointer = ctypes.c_void_p()
        value_length = wintypes.UINT()
        if not version.VerQueryValueW(
            ctypes.byref(buffer),
            query,
            ctypes.byref(value_pointer),
            ctypes.byref(value_length),
        ):
            return None
        address = value_pointer.value
        if address is None:
            return None
        value = ctypes.wstring_at(address, max(0, value_length.value - 1))
        return normalize_text(value, 120)
    except Exception:
        return None
