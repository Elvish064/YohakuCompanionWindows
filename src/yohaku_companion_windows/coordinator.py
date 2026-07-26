from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime

from .capture import PresenceCapture
from .credentials import CredentialStore
from .domain import ClearReason, RuntimeState, SanitizedPresenceSnapshot
from .http_client import CompanionHTTPClient, ResponseFailure
from .identity import APP_VERSION
from .protocol import (
    NegotiationError,
    ServerConfiguration,
    negotiate_presence,
    validate_clock_skew,
)
from .storage import StateStore
from .writer import PresenceWriter

presence_log = logging.getLogger("yohaku.Presence")
network_log = logging.getLogger("yohaku.网络")


class LiveDeskCoordinator:
    """Fresh-capture, coalescing Presence coordinator with bounded ordered clears."""

    POLL_SECONDS = 1.0

    def __init__(
        self,
        store: StateStore,
        credentials: CredentialStore,
        capture: PresenceCapture,
        on_state: Callable[[RuntimeState], None],
        on_published_snapshot: Callable[[SanitizedPresenceSnapshot], None] | None = None,
    ) -> None:
        self._store = store
        self._credentials = credentials
        self._capture = capture
        self._on_state = on_state
        self._on_published_snapshot = on_published_snapshot
        self._task: asyncio.Task[None] | None = None
        self._clear_task: asyncio.Task[None] | None = None
        self._refresh = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._generation = 0
        self._writer: PresenceWriter | None = None
        self._client: CompanionHTTPClient | None = None
        self._suspended = False
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self.running or self._suspended or self._stopping:
            return
        self._generation += 1
        generation = self._generation
        self._set_state(RuntimeState.CONNECTING)
        self._task = asyncio.create_task(self._run(generation), name="live-desk")

    async def request_refresh(self) -> None:
        self._refresh.set()

    async def stop(self, reason: ClearReason, final_state: RuntimeState) -> None:
        async with self._lifecycle_lock:
            await self._stop_locked(reason, final_state)

    async def _stop_locked(self, reason: ClearReason, final_state: RuntimeState) -> None:
        if final_state is not RuntimeState.SUSPENDED:
            self._suspended = False
        self._generation += 1
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._capture.reset_media_continuity()
        await self._begin_bounded_clear(reason)
        await self._discard_client()
        self._set_state(final_state)

    async def suspend(self) -> None:
        async with self._lifecycle_lock:
            if self._suspended:
                return
            self._suspended = True
            await self._stop_locked(ClearReason.SLEEP, RuntimeState.SUSPENDED)

    async def resume(self, should_start: bool = True) -> None:
        async with self._lifecycle_lock:
            if not self._suspended or self._stopping:
                return
            if self._clear_task is not None:
                await self._clear_task
            self._suspended = False
            if should_start:
                await self._start_locked()

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._stopping:
                if self._clear_task is not None:
                    await self._clear_task
                return
            self._stopping = True
            await self._stop_locked(ClearReason.SHUTDOWN, RuntimeState.DISABLED)

    async def _run(self, generation: int) -> None:
        while generation == self._generation and not self._suspended:
            metadata = self._store.load_connection()
            token = await self._credentials.get_token()
            if metadata is None or not metadata.live_desk_enabled or not token:
                self._set_state(RuntimeState.DISABLED)
                return
            try:
                client = CompanionHTTPClient(ServerConfiguration(metadata.base_url))
                self._client = client
                capabilities = await client.fetch_capabilities()
                network_log.info("能力协商完成")
                configuration = negotiate_presence(capabilities, APP_VERSION)
                validate_clock_skew(
                    capabilities.server_time,
                    configuration.maximum_clock_skew_seconds,
                )
                self._writer = PresenceWriter(
                    metadata, token, configuration, self._store, client
                )
            except NegotiationError as error:
                await self._discard_client()
                state = (
                    RuntimeState.UPDATE_REQUIRED
                    if "update" in str(error)
                    else RuntimeState.FEATURE_UNAVAILABLE
                )
                self._set_state(state)
                await self._delay_or_refresh(300)
                continue
            except Exception as error:
                await self._discard_client()
                self._set_state(RuntimeState.DEGRADED)
                network_log.warning(
                    "连接协商失败：%s：%s",
                    type(error).__name__,
                    str(error) or "无详细信息",
                )
                await self._delay_or_refresh(30)
                continue

            self._set_state(RuntimeState.ACTIVE)
            minimum_interval = 60.0 / configuration.requests_per_minute
            requested_lease = min(
                max(90, configuration.minimum_lease_seconds),
                configuration.maximum_lease_seconds,
            )
            heartbeat = min(
                configuration.recommended_heartbeat_seconds,
                max(1, requested_lease // 3),
            )
            last_sent = 0.0
            last_heartbeat = 0.0
            last_semantic: tuple[object, ...] | None = None
            icon_rejected = False
            try:
                while generation == self._generation and not self._suspended:
                    snapshot = await self._capture.capture(
                        include_media=configuration.supports_media_timeline
                    )
                    if icon_rejected:
                        snapshot = _without_application_icon(snapshot)
                    semantic = snapshot.semantic_fingerprint()
                    now = time.monotonic()
                    due = last_heartbeat == 0.0 or now - last_heartbeat >= heartbeat
                    changed = last_semantic is None or semantic != last_semantic
                    if changed or due or self._refresh.is_set():
                        self._refresh.clear()
                        wait = minimum_interval - (now - last_sent)
                        if wait > 0:
                            await asyncio.sleep(wait)
                            # Events during rate-limit waiting collapse into a
                            # new privacy evaluation of the latest desired state.
                            snapshot = await self._capture.capture(
                                include_media=configuration.supports_media_timeline
                            )
                            if icon_rejected:
                                snapshot = _without_application_icon(snapshot)
                            semantic = snapshot.semantic_fingerprint()
                        assert self._writer is not None
                        last_sent = time.monotonic()
                        try:
                            await self._writer.replace(snapshot)
                        except ResponseFailure as error:
                            if (
                                error.error.status_code == 422
                                and error.error.code
                                == "COMPANION_ICON_HOST_NOT_ALLOWED"
                                and snapshot.application is not None
                                and snapshot.application.icon_url is not None
                            ):
                                icon_rejected = True
                                snapshot = _without_application_icon(snapshot)
                                semantic = snapshot.semantic_fingerprint()
                                network_log.warning(
                                    "服务器拒绝软件图标域名（HTTP 422，"
                                    "COMPANION_ICON_HOST_NOT_ALLOWED）；"
                                    "本次连接已自动禁用软件图标并继续上报"
                                )
                                await self._writer.replace(snapshot)
                            else:
                                raise
                        presence_log.info(
                            "Presence 已上报：可见性=%s 应用=%s 媒体=%s",
                            snapshot.availability.value,
                            "分享" if snapshot.application is not None else "隐藏",
                            "分享" if snapshot.media is not None else "隐藏",
                        )
                        if self._on_published_snapshot is not None:
                            self._on_published_snapshot(snapshot)
                        last_heartbeat = last_sent
                        last_semantic = semantic
                        self._set_state(RuntimeState.ACTIVE)
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._refresh.wait(), self.POLL_SECONDS)
                return
            except asyncio.CancelledError:
                raise
            except ResponseFailure as error:
                self._set_state(RuntimeState.DEGRADED)
                network_log.warning(
                    "Presence 请求被服务器拒绝：HTTP %d，错误代码=%s",
                    error.error.status_code,
                    error.error.code or "未知",
                )
                await self._discard_client()
                await self._delay_or_refresh(30)
            except Exception as error:
                self._set_state(RuntimeState.DEGRADED)
                network_log.warning("Presence 连接中断：%s", type(error).__name__)
                await self._discard_client()
                await self._delay_or_refresh(30)

    async def _delay_or_refresh(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._refresh.wait(), seconds)
            self._refresh.clear()
        except TimeoutError:
            pass

    async def _begin_bounded_clear(self, reason: ClearReason) -> None:
        writer = self._writer
        if writer is None:
            return
        if self._clear_task is None or self._clear_task.done():
            async def clear() -> None:
                with suppress(Exception):
                    await asyncio.wait_for(
                        writer.clear(reason, datetime.now(UTC)), timeout=0.5
                    )

            self._clear_task = asyncio.create_task(clear(), name="presence-clear")
        await self._clear_task

    async def _discard_client(self) -> None:
        client, self._client = self._client, None
        self._writer = None
        if client is not None:
            await client.close()

    def _set_state(self, state: RuntimeState) -> None:
        self._on_state(state)


def _without_application_icon(
    snapshot: SanitizedPresenceSnapshot,
) -> SanitizedPresenceSnapshot:
    application = snapshot.application
    if application is None or application.icon_url is None:
        return snapshot
    return replace(
        snapshot,
        application=replace(application, icon_url=None),
    )
