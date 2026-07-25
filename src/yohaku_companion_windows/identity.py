from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_VERSION = "1.7.10"
PRODUCT_NAME = "Yohaku Companion"


@dataclass(frozen=True, slots=True)
class AppIdentity:
    identifier: str
    product_name: str = PRODUCT_NAME

    @property
    def data_directory(self) -> Path:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            local_app_data = str(Path.home() / "AppData" / "Local")
        return Path(local_app_data) / self.identifier

    @property
    def database_path(self) -> Path:
        return self.data_directory / "state.sqlite3"

    @property
    def credential_service(self) -> str:
        return self.identifier

    @property
    def single_instance_name(self) -> str:
        return self.identifier.replace(".", "-")

    @property
    def startup_value_name(self) -> str:
        return "YohakuCompanionWindows" if self.is_release else "YohakuCompanionWindowsDebug"

    @property
    def is_release(self) -> bool:
        return not self.identifier.endswith(".debug")


def current_identity() -> AppIdentity:
    is_frozen = bool(getattr(sys, "frozen", False))
    suffix = "" if is_frozen else ".debug"
    return AppIdentity(f"dev.innei.YohakuCompanion.windows{suffix}")
