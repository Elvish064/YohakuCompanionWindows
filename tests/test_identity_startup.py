from __future__ import annotations

import pytest

from yohaku_companion_windows.identity import AppIdentity
from yohaku_companion_windows.startup import StartupManager


def test_debug_and_release_namespaces_are_isolated() -> None:
    debug = AppIdentity("dev.innei.YohakuCompanion.windows.debug")
    release = AppIdentity("dev.innei.YohakuCompanion.windows")
    assert debug.data_directory != release.data_directory
    assert debug.credential_service != release.credential_service
    assert debug.single_instance_name != release.single_instance_name
    assert debug.startup_value_name != release.startup_value_name


def test_source_mode_cannot_write_startup_registration() -> None:
    manager = StartupManager(AppIdentity("dev.innei.YohakuCompanion.windows.debug"))
    assert not manager.available
    assert not manager.is_enabled()
    with pytest.raises(RuntimeError):
        manager.set_enabled(True)
