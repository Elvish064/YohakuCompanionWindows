from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from yohaku_companion_windows.capture import PresenceCapture
from yohaku_companion_windows.domain import (
    LoggingSettings,
    PrivacyDefaults,
    RawApplicationIdentity,
    SensitiveAction,
    SensitiveField,
    SensitiveTextRule,
    SourceSettings,
    VRChatIntegrationSettings,
)
from yohaku_companion_windows.privacy import PrivacyEvaluator
from yohaku_companion_windows.protocol import ProtocolError
from yohaku_companion_windows.storage import StateStore
from yohaku_companion_windows.vrchat import (
    RPC_FRAME,
    VRCActivityUploader,
    VRChatActivityState,
    accepted_activity,
    decode_rpc_frame,
    encode_rpc_frame,
    sanitize_activity,
    validate_vrc_endpoint,
)


def test_rpc_frame_round_trip_and_size_validation() -> None:
    payload = {"cmd": "SET_ACTIVITY", "nonce": "中文"}
    assert decode_rpc_frame(encode_rpc_frame(RPC_FRAME, payload)) == (RPC_FRAME, payload)
    with pytest.raises(ValueError):
        decode_rpc_frame(b"\x01\x00\x00\x00\x05\x00\x00\x00{}")


def test_only_vrchat_and_vrcx_processes_are_accepted() -> None:
    payload = {
        "cmd": "SET_ACTIVITY",
        "args": {"pid": 42, "activity": {"details": "World"}},
    }
    assert accepted_activity(payload, lambda _pid: "vrchat.exe") == {"details": "World"}
    assert accepted_activity(payload, lambda _pid: "VRCX.EXE") == {"details": "World"}
    assert accepted_activity(payload, lambda _pid: "discord.exe") is NotImplemented
    assert accepted_activity(
        {"cmd": "SET_ACTIVITY", "args": {"activity": None}},
        lambda _pid: "vrchat.exe",
    ) is NotImplemented
    clear = {"cmd": "SET_ACTIVITY", "args": {"pid": 42, "activity": None}}
    assert accepted_activity(clear, lambda _pid: "vrchat.exe") is None


def test_activity_uses_whitelist_and_sensitive_filter() -> None:
    rule = SensitiveTextRule(
        str(uuid4()),
        "秘密",
        "secret",
        (SensitiveField.WINDOW_TITLE,),
        SensitiveAction.MASK_MATCH,
    )
    evaluator = PrivacyEvaluator(PrivacyDefaults(), (), (rule,))
    result = sanitize_activity(
        {
            "details": "secret world",
            "state": "friends",
            "timestamps": {"start": 10, "end": -1, "unknown": 4},
            "assets": {"large_image": "asset", "large_text": "raw"},
            "party": {"id": "private"},
            "secrets": {"join": "private"},
            "buttons": [{"url": "https://example.com"}],
            "type": 0,
        },
        evaluator,
    )
    assert result == {
        "details": "••• world",
        "state": "friends",
        "timestamps": {"start": 10},
        "assets": {"large_image": "asset"},
    }


def test_hide_context_suppresses_entire_activity() -> None:
    rule = SensitiveTextRule(
        str(uuid4()),
        "世界",
        "private",
        (SensitiveField.WINDOW_TITLE,),
        SensitiveAction.HIDE_CONTEXT,
    )
    evaluator = PrivacyEvaluator(PrivacyDefaults(), (), (rule,))
    assert sanitize_activity({"details": "private world"}, evaluator) is None


def test_vrc_endpoint_is_a_full_http_or_https_url() -> None:
    assert validate_vrc_endpoint("https://example.com/vrc/activity").endswith(
        "/vrc/activity"
    )
    assert validate_vrc_endpoint("http://192.168.1.2:2333/vrc") == (
        "http://192.168.1.2:2333/vrc"
    )
    with pytest.raises(ProtocolError):
        validate_vrc_endpoint("ftp://example.com/vrc")


def test_old_database_defaults_and_settings_round_trip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    assert store.load_vrchat_settings() == VRChatIntegrationSettings()
    assert store.load_logging_settings() == LoggingSettings()
    settings = VRChatIntegrationSettings(True, False, True, "https://example.com/vrc")
    store.save_vrchat_settings(settings)
    store.save_logging_settings(LoggingSettings(True, True))
    assert store.load_vrchat_settings() == settings
    assert store.load_logging_settings().file_enabled
    assert store.load_logging_settings().vrchat_debug_enabled
    raw_database = (tmp_path / "state.sqlite3").read_bytes()
    assert b"api-secret" not in raw_database
    store.close()


@pytest.mark.asyncio
async def test_world_title_uses_existing_title_privacy_gate(tmp_path: Path) -> None:
    class Applications:
        title_reads = 0

        def current_application(self) -> RawApplicationIdentity:
            return RawApplicationIdentity("win32:vrchat.exe", "VRChat", 1)

        def read_window_title(self, _window_handle: int) -> str:
            self.title_reads += 1
            return "ordinary title"

    class Media:
        available = False

        async def start(self, _on_change=None) -> bool:  # type: ignore[no-untyped-def]
            return False

        async def current_media(self):  # type: ignore[no-untyped-def]
            return None

        def reset_continuity(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    store = StateStore(tmp_path / "state.sqlite3")
    store.save_sources(SourceSettings(True, True, False))
    store.save_privacy_defaults(PrivacyDefaults(True, True, False))
    store.save_vrchat_settings(VRChatIntegrationSettings(True, True, False, ""))
    activity = VRChatActivityState()
    activity.update({"details": "World Name"})
    applications = Applications()
    capture = PresenceCapture(store, applications, Media(), activity)
    snapshot = await capture.capture()
    assert snapshot.application is not None
    assert snapshot.application.window_title == "World Name"
    assert applications.title_reads == 0

    store.save_sources(SourceSettings(True, False, False))
    snapshot = await capture.capture()
    assert snapshot.application is not None
    assert snapshot.application.window_title is None
    assert applications.title_reads == 0
    store.close()


@pytest.mark.asyncio
async def test_uploader_coalesces_burst_to_latest_activity() -> None:
    class Response:
        status_code = 204

    class Client:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        async def post(self, _url: str, **kwargs):  # type: ignore[no-untyped-def]
            self.payloads.append(kwargs["json"])
            return Response()

        async def aclose(self) -> None:
            return None

    evaluator = PrivacyEvaluator(PrivacyDefaults(), ())
    uploader = VRCActivityUploader(
        VRChatIntegrationSettings(True, False, True, "https://example.com/vrc"),
        "not-logged-secret",
        evaluator,
    )
    client = Client()
    uploader._client = client  # type: ignore[assignment]
    uploader.MINIMUM_UPLOAD_INTERVAL_SECONDS = 0
    uploader.submit({"details": "first"}, "1")
    uploader.submit({"details": "second"}, "2")
    uploader.submit({"details": "latest"}, "3")
    uploader._task = asyncio.create_task(uploader._run())
    await asyncio.sleep(0.05)
    await uploader.stop()
    assert len(client.payloads) == 1
    assert client.payloads[0]["activity"] == {"details": "latest"}
    assert client.payloads[0]["nonce"] == "3"
