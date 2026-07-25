from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import struct
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .domain import SensitiveField, VRChatIntegrationSettings, normalize_text
from .http_client import verify_transport_address
from .privacy import PrivacyEvaluator
from .protocol import MAXIMUM_SAFE_INTEGER, ProtocolError, validate_base_url, wire_timestamp

RPC_HANDSHAKE = 0
RPC_FRAME = 1
RPC_CLOSE = 2
RPC_PING = 3
RPC_PONG = 4
RPC_MAX_PAYLOAD = 1024 * 1024
RPC_PIPE_BUFFER = 64 * 1024
_ACCEPTED_EXECUTABLES = {"vrchat.exe", "vrcx.exe"}

capture_log = logging.getLogger("yohaku.VRChat 捕获")
upload_log = logging.getLogger("yohaku.VRC 上传")


def encode_rpc_frame(opcode: int, payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > RPC_MAX_PAYLOAD:
        raise ValueError("RPC payload too large")
    return struct.pack("<II", opcode, len(raw)) + raw


def decode_rpc_frame(frame: bytes) -> tuple[int, dict[str, Any]]:
    if len(frame) < 8:
        raise ValueError("incomplete RPC frame")
    opcode, length = struct.unpack("<II", frame[:8])
    if length > RPC_MAX_PAYLOAD or len(frame) != length + 8:
        raise ValueError("invalid RPC frame length")
    payload = json.loads(frame[8:].decode("utf-8")) if length else {}
    if not isinstance(payload, dict):
        raise ValueError("RPC payload root must be an object")
    return opcode, payload


class VRChatActivityState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._activity: dict[str, Any] | None = None

    def update(self, activity: dict[str, Any] | None) -> None:
        with self._lock:
            self._activity = None if activity is None else deepcopy(activity)

    def clear(self) -> None:
        self.update(None)

    def activity(self) -> dict[str, Any] | None:
        with self._lock:
            return None if self._activity is None else deepcopy(self._activity)

    def world_name(self) -> str | None:
        activity = self.activity()
        details = None if activity is None else activity.get("details")
        return normalize_text(details if isinstance(details, str) else None, 500)


def process_executable_name(process_id: int) -> str | None:
    if os.name != "nt" or not 0 < process_id <= 0xFFFFFFFF:
        return None
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, process_id)
    if not process:
        return None
    try:
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
            process, 0, buffer, ctypes.byref(size)
        ):
            return None
        return Path(buffer.value).name.casefold()
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


def accepted_activity(
    payload: dict[str, Any],
    executable_lookup: Callable[[int], str | None] = process_executable_name,
) -> dict[str, Any] | None | object:
    """Return activity, None for a clear, or NotImplemented for a rejected sender."""
    if payload.get("cmd") != "SET_ACTIVITY":
        return NotImplemented
    args = payload.get("args")
    if not isinstance(args, dict) or type(args.get("pid")) is not int:
        return NotImplemented
    executable = executable_lookup(args["pid"])
    if executable is None or executable.casefold() not in _ACCEPTED_EXECUTABLES:
        return NotImplemented
    activity = args.get("activity")
    if activity is None:
        return None
    return deepcopy(activity) if isinstance(activity, dict) else NotImplemented


def sanitize_activity(
    activity: dict[str, Any] | None,
    evaluator: PrivacyEvaluator,
) -> dict[str, Any] | None:
    if activity is None:
        return None
    evaluator.reset_diagnostics()
    result: dict[str, Any] = {}
    for key in ("details", "state"):
        raw = activity.get(key)
        filtered = evaluator.filter_text(
            raw if isinstance(raw, str) else None,
            SensitiveField.WINDOW_TITLE,
            500,
        )
        if filtered.hide_context:
            return None
        if filtered.value is not None:
            result[key] = filtered.value
    timestamps = activity.get("timestamps")
    if isinstance(timestamps, dict):
        clean_timestamps = {
            key: value
            for key in ("start", "end")
            if type(value := timestamps.get(key)) is int
            and 0 <= value <= MAXIMUM_SAFE_INTEGER
        }
        if clean_timestamps:
            result["timestamps"] = clean_timestamps
    assets = activity.get("assets")
    if isinstance(assets, dict):
        clean_assets: dict[str, str] = {}
        for key in ("large_image", "small_image"):
            value = assets.get(key)
            normalized = normalize_text(value if isinstance(value, str) else None, 512)
            if normalized is not None:
                clean_assets[key] = normalized
        if clean_assets:
            result["assets"] = clean_assets
    return result or None


def validate_vrc_endpoint(value: str) -> str:
    endpoint = validate_base_url(value)
    parsed = urlsplit(endpoint)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ProtocolError("VRC API 端点必须使用 HTTP 或 HTTPS")
    return endpoint


class VRCActivityUploader:
    MINIMUM_UPLOAD_INTERVAL_SECONDS = 1.0

    def __init__(
        self,
        settings: VRChatIntegrationSettings,
        api_key: str,
        evaluator: PrivacyEvaluator,
    ) -> None:
        self._settings = settings
        self._api_key = api_key
        self._evaluator = evaluator
        self._queue: asyncio.Queue[tuple[dict[str, Any] | None, Any]] = asyncio.Queue(20)
        self._task: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self._last_upload_at = 0.0

    async def start(self) -> None:
        endpoint = validate_vrc_endpoint(self._settings.endpoint_url)
        await verify_transport_address(endpoint)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._task = asyncio.create_task(self._run(), name="vrc-activity-upload")
        upload_log.info("VRC 状态上传已启动")

    def submit(self, activity: dict[str, Any] | None, nonce: Any) -> None:
        item = (deepcopy(activity), _safe_nonce(nonce))
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            upload_log.warning("上传队列已满，已丢弃最旧状态")
        self._queue.put_nowait(item)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _run(self) -> None:
        while True:
            activity, nonce = await self._queue.get()
            while True:
                try:
                    latest_activity, latest_nonce = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._queue.task_done()
                activity, nonce = latest_activity, latest_nonce
            try:
                remaining = self.MINIMUM_UPLOAD_INTERVAL_SECONDS - (
                    time.monotonic() - self._last_upload_at
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
                clean = sanitize_activity(activity, self._evaluator)
                if activity is not None and clean is None:
                    upload_log.info("Activity 已被隐私规则隐藏，未上传")
                    continue
                await verify_transport_address(self._settings.endpoint_url)
                assert self._client is not None
                self._last_upload_at = time.monotonic()
                response = await self._client.post(
                    self._settings.endpoint_url,
                    headers={"X-API-Key": self._api_key},
                    json={
                        "capture_at": wire_timestamp(datetime.now(UTC)),
                        "activity": clean,
                        "nonce": nonce,
                    },
                )
                if response.status_code in {200, 201, 202, 204, 409}:
                    upload_log.debug("VRC 状态上报完成，HTTP %d", response.status_code)
                else:
                    upload_log.warning("VRC 状态上报被拒绝，HTTP %d", response.status_code)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                upload_log.warning("VRC 状态上报失败：%s", type(error).__name__)
            finally:
                self._queue.task_done()


class DiscordRPCCapture:
    def __init__(
        self,
        on_activity: Callable[[dict[str, Any] | None, Any], None],
        executable_lookup: Callable[[int], str | None] = process_executable_name,
    ) -> None:
        self._on_activity = on_activity
        self._executable_lookup = executable_lookup
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._activity_log_lock = threading.Lock()
        self._activity_log_count = 0
        self._activity_log_at = 0.0

    def start(self) -> None:
        if self._threads:
            return
        try:
            import pywintypes
            import win32file
            import win32pipe
        except ImportError as error:
            raise RuntimeError("VRChat 集成需要 pywin32==312") from error
        self._stop.clear()
        for index in range(10):
            thread = threading.Thread(
                target=self._listen,
                args=(index, win32pipe, win32file, pywintypes),
                name=f"yohaku-discord-ipc-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        capture_log.info("正在监听 discord-ipc-0..9")

    def stop(self) -> None:
        self._stop.set()
        self._wake_listeners()
        for thread in self._threads:
            thread.join(timeout=0.25)
        self._threads.clear()
        capture_log.info("Discord RPC 捕获已停止")

    def _listen(self, index: int, win32pipe: Any, win32file: Any, pywintypes: Any) -> None:
        pipe_name = rf"\\.\pipe\discord-ipc-{index}"
        last_unavailable_log = 0.0
        while not self._stop.is_set():
            handle = None
            try:
                handle = win32pipe.CreateNamedPipe(
                    pipe_name,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_BYTE
                    | win32pipe.PIPE_READMODE_BYTE
                    | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    RPC_PIPE_BUFFER,
                    RPC_PIPE_BUFFER,
                    0,
                    None,
                )
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except pywintypes.error as error:
                    if error.winerror != 535:
                        raise
                if not self._stop.is_set():
                    self._handle_client(handle, win32file, pywintypes)
            except Exception as error:
                if not self._stop.is_set():
                    now = time.monotonic()
                    if now - last_unavailable_log >= 30:
                        capture_log.debug(
                            "discord-ipc-%d 暂不可用：%s",
                            index,
                            type(error).__name__,
                        )
                        last_unavailable_log = now
                    self._stop.wait(1)
            finally:
                if handle is not None:
                    with suppress(Exception):
                        win32file.CloseHandle(handle)

    def _handle_client(self, handle: Any, win32file: Any, pywintypes: Any) -> None:
        accepted_connection = False
        try:
            while not self._stop.is_set():
                opcode, payload = self._read_frame(handle, win32file, pywintypes)
                if opcode == RPC_HANDSHAKE:
                    self._write_frame(handle, RPC_FRAME, _ready_payload(), win32file)
                elif opcode == RPC_PING:
                    self._write_frame(handle, RPC_PONG, payload, win32file)
                elif opcode == RPC_CLOSE:
                    return
                elif opcode == RPC_FRAME:
                    activity: Any = accepted_activity(payload, self._executable_lookup)
                    if activity is not NotImplemented:
                        accepted_connection = True
                        self._on_activity(activity, payload.get("nonce"))
                        self._log_activity(activity is None)
                    self._ack(handle, payload, win32file)
        finally:
            if accepted_connection and not self._stop.is_set():
                self._on_activity(None, None)

    def _log_activity(self, cleared: bool) -> None:
        if not capture_log.isEnabledFor(logging.DEBUG):
            return
        with self._activity_log_lock:
            self._activity_log_count += 1
            now = time.monotonic()
            if not cleared and now - self._activity_log_at < 5:
                return
            count = self._activity_log_count
            self._activity_log_count = 0
            self._activity_log_at = now
        capture_log.debug(
            "已接收 %d 次目标进程 Activity 更新%s",
            count,
            "；最新状态为清除" if cleared else "",
        )

    def _read_frame(
        self, handle: Any, win32file: Any, pywintypes: Any
    ) -> tuple[int, dict[str, Any]]:
        header = self._read_exact(handle, 8, win32file, pywintypes)
        _, length = struct.unpack("<II", header)
        if length > RPC_MAX_PAYLOAD:
            raise ValueError("RPC payload too large")
        return decode_rpc_frame(
            header + self._read_exact(handle, length, win32file, pywintypes)
        )

    @staticmethod
    def _read_exact(handle: Any, size: int, win32file: Any, pywintypes: Any) -> bytes:
        chunks: list[bytes] = []
        while size > 0:
            try:
                _, data = win32file.ReadFile(handle, size)
            except pywintypes.error as error:
                raise ConnectionError("RPC pipe read failed") from error
            if not data:
                raise ConnectionError("RPC client disconnected")
            chunks.append(data)
            size -= len(data)
        return b"".join(chunks)

    @staticmethod
    def _write_frame(
        handle: Any, opcode: int, payload: dict[str, Any], win32file: Any
    ) -> None:
        win32file.WriteFile(handle, encode_rpc_frame(opcode, payload))

    def _ack(self, handle: Any, request: dict[str, Any], win32file: Any) -> None:
        if request.get("nonce") is not None:
            self._write_frame(
                handle,
                RPC_FRAME,
                {
                    "cmd": request.get("cmd"),
                    "nonce": request["nonce"],
                    "data": {},
                },
                win32file,
            )

    @staticmethod
    def _wake_listeners() -> None:
        try:
            import win32file
        except ImportError:
            return
        for index in range(10):
            try:
                handle = win32file.CreateFile(
                    rf"\\.\pipe\discord-ipc-{index}",
                    0xC0000000,
                    0,
                    None,
                    3,
                    0,
                    None,
                )
                win32file.CloseHandle(handle)
            except Exception:
                pass


class VRChatIntegration:
    def __init__(self, activity_state: VRChatActivityState) -> None:
        self.activity_state = activity_state
        self._capture: DiscordRPCCapture | None = None
        self._uploader: VRCActivityUploader | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_change: Callable[[], None] | None = None

    @property
    def running(self) -> bool:
        return self._capture is not None

    async def start(
        self,
        settings: VRChatIntegrationSettings,
        api_key: str | None,
        evaluator: PrivacyEvaluator,
        on_change: Callable[[], None],
    ) -> None:
        await self.stop()
        self._loop = asyncio.get_running_loop()
        self._on_change = on_change
        if settings.upload_activity:
            if not api_key:
                raise RuntimeError("VRC API 密匙未配置")
            uploader = VRCActivityUploader(settings, api_key, evaluator)
            await uploader.start()
            self._uploader = uploader

        def receive(activity: dict[str, Any] | None, nonce: Any) -> None:
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._accept, activity, nonce)

        capture = DiscordRPCCapture(receive)
        try:
            await asyncio.to_thread(capture.start)
        except Exception:
            await self.stop()
            raise
        self._capture = capture

    async def stop(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            await asyncio.to_thread(capture.stop)
        uploader, self._uploader = self._uploader, None
        if uploader is not None:
            await uploader.stop()
        self.activity_state.clear()
        self._loop = None
        self._on_change = None

    def _accept(self, activity: dict[str, Any] | None, nonce: Any) -> None:
        self.activity_state.update(activity)
        if self._uploader is not None:
            self._uploader.submit(activity, nonce)
        if self._on_change is not None:
            self._on_change()


def _safe_nonce(value: Any) -> str | int | None:
    if type(value) is int and 0 <= value <= MAXIMUM_SAFE_INTEGER:
        return value
    if isinstance(value, str):
        return normalize_text(value, 128)
    return None


def _ready_payload() -> dict[str, Any]:
    return {
        "cmd": "DISPATCH",
        "evt": "READY",
        "data": {
            "v": 1,
            "config": {
                "cdn_host": "cdn.discordapp.com",
                "api_endpoint": "//discord.com/api",
                "environment": "production",
            },
            "user": {
                "id": "0",
                "username": "Yohaku Companion",
                "discriminator": "0000",
                "avatar": None,
                "bot": False,
            },
        },
    }
