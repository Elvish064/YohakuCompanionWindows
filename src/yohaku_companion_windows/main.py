from __future__ import annotations

import argparse
import asyncio
import os
import platform
import struct
import sys
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .identity import APP_VERSION, current_identity


def main() -> int:
    arguments = _arguments()
    if arguments.self_test:
        return _self_test()
    if os.name != "nt":
        print("Yohaku Companion Windows 只能在 Windows 10 1809+ 或 Windows 11 上运行。")
        return 2
    if struct.calcsize("P") != 8:
        print("Yohaku Companion Windows 首版仅支持 x64。")
        return 2
    if _windows_build() < 17763:
        print("Yohaku Companion Windows 需要 Windows 10 1809 或更高版本。")
        return 2
    return _gui_main(arguments.background)


def _gui_main(background: bool) -> int:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon
    from qasync import QEventLoop

    from .capture import PresenceCapture
    from .credentials import WindowsCredentialStore, WindowsVRChatCredentialStore
    from .lifecycle import WindowsLifecycleMonitor
    from .logging_service import ProcessLogService
    from .media_capture import WinRTMediaProvider
    from .service import ApplicationService
    from .single_instance import SingleInstance
    from .startup import StartupManager
    from .storage import StateStore
    from .ui import SettingsWindow, TrayController
    from .vrchat import VRChatActivityState, VRChatIntegration
    from .win32_capture import Win32ApplicationProvider

    app = QApplication(sys.argv)
    app.setApplicationName("Yohaku Companion")
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("当前 Windows 会话不支持系统托盘。")
        return 3

    icon = QIcon(str(Path(__file__).with_name("assets") / "tray.svg"))
    holder: dict[str, SettingsWindow] = {}
    identity = current_identity()
    single_instance = SingleInstance(
        identity.single_instance_name,
        lambda: holder["window"].show_and_raise() if "window" in holder else None,
    )
    if not single_instance.acquire():
        return 0

    store = StateStore(identity.database_path)
    logs = ProcessLogService(identity.data_directory / "logs")
    logs.install()
    logging_settings = store.load_logging_settings()
    logs.set_master_enabled(logging_settings.master_enabled)
    logs.set_file_enabled(
        logging_settings.master_enabled and logging_settings.file_enabled
    )
    logs.set_vrchat_debug_enabled(
        logging_settings.master_enabled and logging_settings.vrchat_debug_enabled
    )
    credentials = WindowsCredentialStore(identity.credential_service)
    vrchat_credentials = WindowsVRChatCredentialStore(identity.credential_service)
    media = WinRTMediaProvider()
    vrchat_activity = VRChatActivityState()
    capture = PresenceCapture(
        store,
        Win32ApplicationProvider(),
        media,
        vrchat_activity=vrchat_activity,
    )
    vrchat = VRChatIntegration(vrchat_activity)
    service = ApplicationService(
        store,
        credentials,
        capture,
        media,
        vrchat_credentials,
        vrchat,
        logs,
    )
    monitor = WindowsLifecycleMonitor(service.handle_suspend, service.handle_resume)
    service.set_lifecycle_available(monitor.install())
    startup = StartupManager(identity)
    window = SettingsWindow(service, startup, icon)
    holder["window"] = window
    stopped = False

    async def quit_application() -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        window.begin_quit()
        try:
            await service.shutdown()
        finally:
            monitor.close()
            single_instance.close()
            tray.hide()
            window.close()
            app.quit()

    tray = TrayController(app, service, window, icon, quit_application)
    tray.show()
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(service.initialize())
        if not background or service.state.connection is None:
            window.show_and_raise()
        loop.run_forever()
    finally:
        if not stopped:
            stopped = True
            with suppress(Exception):
                loop.run_until_complete(service.shutdown())
            monitor.close()
            single_instance.close()
        loop.close()
    return 0


def _self_test() -> int:
    """No-UI smoke test used against the PyInstaller Release directory."""
    try:
        if os.name != "nt" or _windows_build() < 17763:
            raise RuntimeError("Windows 10 1809+ is required")
        if struct.calcsize("P") != 8:
            raise RuntimeError("x64 is required")
        import httpx  # noqa: F401
        import keyring  # noqa: F401
        import pywintypes  # noqa: F401
        import qasync  # noqa: F401
        import win32file  # noqa: F401
        import win32pipe  # noqa: F401
        from anyio._backends._asyncio import AsyncIOBackend  # noqa: F401
        from PySide6 import QtCore  # noqa: F401
        from winrt.windows.media.control import (  # noqa: F401
            GlobalSystemMediaTransportControlsSessionManager,
        )

        from .domain import PresenceConfiguration, SanitizedPresenceSnapshot
        from .protocol import encode_json, make_presence_request
        from .storage import StateStore
        from .vrchat import decode_rpc_frame, encode_rpc_frame

        configuration = PresenceConfiguration(32_768, 60, 30, 120, 60, 60, True)
        snapshot = SanitizedPresenceSnapshot(datetime.now(UTC), None, None)
        payload = encode_json(
            make_presence_request(
                snapshot,
                "018F3E8B-8F6C-7A4B-9D10-123456789ABC",
                0,
                configuration,
            )
        )
        if b'"application":null' not in payload or b'"media":null' not in payload:
            raise RuntimeError("protocol null fields are missing")
        if decode_rpc_frame(encode_rpc_frame(3, {"nonce": "probe"})) != (
            3,
            {"nonce": "probe"},
        ):
            raise RuntimeError("Discord RPC frame codec failed")
        asyncio.run(_verify_httpx_loopback())
        with tempfile.TemporaryDirectory(prefix="yohaku-self-test-") as directory:
            state = StateStore(Path(directory) / "state.sqlite3")
            state.close()
        backend = _windows_keyring_backend()
        if float(backend.priority) <= 0:
            raise RuntimeError("Windows Credential Locker backend is unavailable")
        _verify_windows_keyring(backend)
    except Exception as error:
        report_path = os.environ.get("YOHAKU_SELF_TEST_REPORT")
        if report_path:
            with suppress(Exception):
                Path(report_path).write_text(
                    f"{type(error).__name__}: {error}",
                    encoding="utf-8",
                )
        print(f"SELF-TEST FAILED: {error}")
        return 1
    print(f"SELF-TEST OK: Yohaku Companion Windows {APP_VERSION}")
    return 0


def _windows_keyring_backend() -> Any:
    from keyring.backends.Windows import WinVaultKeyring

    return WinVaultKeyring()


async def _verify_httpx_loopback() -> None:
    """Exercise HTTPX/AnyIO's dynamic asyncio backend in the frozen app."""
    import httpx

    async def respond(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 204 No Content\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        sockets = server.sockets
        if not sockets:
            raise RuntimeError("loopback test server failed to bind")
        port = int(sockets[0].getsockname()[1])
        async with httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(f"http://127.0.0.1:{port}/self-test")
        if response.status_code != 204:
            raise RuntimeError("HTTPX loopback request failed")
    finally:
        server.close()
        await server.wait_closed()


def _verify_windows_keyring(backend: Any) -> None:
    """Round-trip a disposable secret so bundled backend imports are exercised."""
    service = f"dev.innei.YohakuCompanion.windows.self-test.{uuid4()}"
    account = "credential-probe"
    marker = f"probe-{uuid4()}"
    stored = False
    try:
        backend.set_password(service, account, marker)
        stored = True
        if backend.get_password(service, account) != marker:
            raise RuntimeError("Windows Credential Locker round-trip failed")
    finally:
        if stored:
            backend.delete_password(service, account)


def _windows_build() -> int:
    try:
        return int(platform.version().split(".")[-1])
    except (ValueError, IndexError):
        return 0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="YohakuCompanion")
    parser.add_argument("--background", action="store_true", help="在系统托盘后台启动")
    parser.add_argument("--self-test", action="store_true", help="执行无界面打包冒烟测试")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
