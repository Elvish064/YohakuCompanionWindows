from __future__ import annotations

import sys
from contextlib import suppress
from pathlib import Path

from .identity import AppIdentity


class StartupManager:
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(self, identity: AppIdentity) -> None:
        self._identity = identity

    @property
    def available(self) -> bool:
        return self._identity.is_release and getattr(sys, "frozen", False)

    def is_enabled(self) -> bool:
        if not self.available:
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, self._identity.startup_value_name)
            return value == self._command()
        except FileNotFoundError:
            return False

    def set_enabled(self, enabled: bool) -> None:
        if not self.available:
            if enabled:
                raise RuntimeError("开机启动仅适用于打包后的 Release EXE")
            return
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    self._identity.startup_value_name,
                    0,
                    winreg.REG_SZ,
                    self._command(),
                )
            else:
                with suppress(FileNotFoundError):
                    winreg.DeleteValue(key, self._identity.startup_value_name)

    @staticmethod
    def _command() -> str:
        executable = str(Path(sys.executable).resolve())
        return f'"{executable}" --background'
