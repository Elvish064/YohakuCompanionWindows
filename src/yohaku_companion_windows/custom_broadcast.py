from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .credentials import CredentialStore
from .domain import ClearReason, CustomBroadcastSpec, SanitizedPresenceSnapshot
from .http_client import CompanionHTTPClient
from .identity import APP_VERSION
from .protocol import ServerConfiguration, negotiate_presence, validate_clock_skew
from .storage import StateStore
from .writer import PresenceWriter


class CustomBroadcastController:
    """A bounded test publisher independent from the normal Live Desk switch."""

    def __init__(self, store: StateStore, credentials: CredentialStore) -> None:
        self._store = store
        self._credentials = credentials
        self._task: asyncio.Task[None] | None = None
        self._writer: PresenceWriter | None = None
        self._client: CompanionHTTPClient | None = None
        self._deadline: datetime | None = None
        self.status = "未运行"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def remaining_seconds(self) -> int:
        if self._deadline is None:
            return 0
        return max(0, int((self._deadline - datetime.now(UTC)).total_seconds() + 0.999))

    async def start(self, spec: CustomBroadcastSpec) -> None:
        if self.running:
            raise RuntimeError("测试广播正在运行")
        metadata = self._store.load_connection()
        token = await self._credentials.get_token()
        if metadata is None or token is None:
            raise RuntimeError("必须先完成设备配对")
        client = CompanionHTTPClient(ServerConfiguration(metadata.base_url))
        try:
            capabilities = await client.fetch_capabilities()
            configuration = negotiate_presence(capabilities, APP_VERSION)
            validate_clock_skew(
                capabilities.server_time,
                configuration.maximum_clock_skew_seconds,
            )
            if spec.snapshot.media is not None:
                if (
                    spec.snapshot.media.artwork_url is not None
                    and not configuration.supports_media_artwork
                ):
                    raise RuntimeError("服务器不支持媒体封面")
                if (
                    spec.snapshot.media.link_url is not None
                    and not configuration.supports_media_playback_links
                ):
                    raise RuntimeError("服务器不支持媒体播放链接")
            self._client = client
            self._writer = PresenceWriter(
                metadata, token, configuration, self._store, client
            )
            self._deadline = datetime.now(UTC) + timedelta(seconds=spec.duration_seconds)
            self.status = "正在广播"
            self._task = asyncio.create_task(
                self._run(spec, configuration.recommended_heartbeat_seconds),
                name="custom-presence-broadcast",
            )
        except Exception:
            await client.close()
            raise

    async def _run(self, spec: CustomBroadcastSpec, heartbeat: int) -> None:
        assert self._writer is not None
        try:
            while self.remaining_seconds > 0:
                snapshot = _advance_snapshot(spec.snapshot)
                await self._writer.replace(snapshot, requested_lease_seconds=heartbeat * 3)
                await asyncio.sleep(min(max(1, heartbeat), self.remaining_seconds))
            self.status = "测试广播已完成"
        except asyncio.CancelledError:
            self.status = "测试广播已停止"
            raise
        except Exception as error:
            self.status = f"测试广播失败：{type(error).__name__}"
        finally:
            await self._clear()
            await self._close()

    async def stop(self, reason: ClearReason = ClearReason.PAUSED) -> None:
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            await self._clear(reason)
            await self._close()
        self._deadline = None

    async def _clear(self, reason: ClearReason = ClearReason.PAUSED) -> None:
        if self._writer is not None:
            with suppress(Exception):
                await asyncio.wait_for(
                    self._writer.clear(reason, datetime.now(UTC)),
                    timeout=0.5,
                )

    async def _close(self) -> None:
        client, self._client = self._client, None
        self._writer = None
        if client is not None:
            await client.close()


def _advance_snapshot(snapshot: SanitizedPresenceSnapshot) -> SanitizedPresenceSnapshot:
    now = datetime.now(UTC)
    media = snapshot.media
    if media is not None and media.playback.state.value == "playing":
        elapsed = max(0.0, (now - media.playback.sampled_at).total_seconds())
        position = (
            None
            if media.playback.position_seconds is None
            else media.playback.position_seconds + elapsed * media.playback.rate
        )
        if media.playback.duration_seconds is not None and position is not None:
            position = min(position, media.playback.duration_seconds)
        media = replace(
            media,
            playback=replace(
                media.playback,
                position_seconds=position,
                sampled_at=now,
            ),
        )
    return replace(snapshot, observed_at=now, media=media)
