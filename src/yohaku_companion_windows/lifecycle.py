from __future__ import annotations

import asyncio
import ctypes
import os
from collections.abc import Callable, Coroutine
from typing import Any

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QCoreApplication, Qt
from PySide6.QtWidgets import QWidget


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


class WindowsLifecycleMonitor(QAbstractNativeEventFilter):
    WM_WTSSESSION_CHANGE = 0x02B1
    WM_POWERBROADCAST = 0x0218
    WTS_SESSION_LOCK = 0x7
    WTS_SESSION_UNLOCK = 0x8
    PBT_APMSUSPEND = 0x4
    RESUME_EVENTS = frozenset((0x6, 0x7, 0x12))
    NOTIFY_FOR_THIS_SESSION = 0

    def __init__(
        self,
        on_suspend: Callable[[], Coroutine[Any, Any, None]],
        on_resume: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        super().__init__()
        self._on_suspend = on_suspend
        self._on_resume = on_resume
        self._window = QWidget()
        self._window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self._installed = False
        self._registered = False

    def install(self) -> bool:
        if os.name != "nt":
            return False
        application = QCoreApplication.instance()
        if application is None:
            return False
        hwnd = int(self._window.winId())
        try:
            wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
            register = wtsapi32.WTSRegisterSessionNotification
            register.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            register.restype = ctypes.c_bool
            self._registered = bool(register(hwnd, self.NOTIFY_FOR_THIS_SESSION))
        except Exception:
            self._registered = False
        if not self._registered:
            return False
        application.installNativeEventFilter(self)
        self._installed = True
        return True

    def close(self) -> None:
        if self._installed:
            application = QCoreApplication.instance()
            if application is not None:
                application.removeNativeEventFilter(self)
            self._installed = False
        if self._registered:
            try:
                unregister = ctypes.WinDLL("wtsapi32").WTSUnRegisterSessionNotification
                unregister.argtypes = [ctypes.c_void_p]
                unregister.restype = ctypes.c_bool
                unregister(ctypes.c_void_p(int(self._window.winId())))
            except Exception:
                pass
            self._registered = False
        self._window.close()

    def nativeEventFilter(
        self,
        event_type: QByteArray | bytes | bytearray | memoryview[int],
        message: int,
    ) -> tuple[bool, int]:
        del event_type
        try:
            msg = _MSG.from_address(int(message))
            if msg.message == self.WM_WTSSESSION_CHANGE:
                if msg.wParam == self.WTS_SESSION_LOCK:
                    self._schedule(self._on_suspend)
                elif msg.wParam == self.WTS_SESSION_UNLOCK:
                    self._schedule(self._on_resume)
            elif msg.message == self.WM_POWERBROADCAST:
                if msg.wParam == self.PBT_APMSUSPEND:
                    self._schedule(self._on_suspend)
                elif msg.wParam in self.RESUME_EVENTS:
                    self._schedule(self._on_resume)
        except Exception:
            pass
        return False, 0

    @staticmethod
    def _schedule(callback: Callable[[], Coroutine[Any, Any, None]]) -> None:
        asyncio.get_running_loop().create_task(callback())
