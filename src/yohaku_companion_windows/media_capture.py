from __future__ import annotations

import asyncio
import math
import platform
from collections.abc import Callable, Coroutine
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from .domain import MediaKind, PlaybackState, RawMediaPresence, normalize_text


class MediaProvider(Protocol):
    @property
    def available(self) -> bool: ...

    async def start(
        self,
        on_change: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> bool: ...

    async def current_media(self) -> RawMediaPresence | None: ...

    def reset_continuity(self) -> None: ...

    async def stop(self) -> None: ...


class WinRTMediaProvider:
    """Global System Media Transport Controls reader with semantic UUID continuity."""

    MINIMUM_WINDOWS_BUILD = 17763
    PAUSED_RETENTION_SECONDS = 5 * 60

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._manager: Any | None = None
        self._available = False
        self._on_change: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._event_tokens: list[tuple[Any, str, Any]] = []
        self._observed_session: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semantic_key: tuple[object, ...] | None = None
        self._semantic_session_id: UUID | None = None
        self._paused_since: datetime | None = None
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def available(self) -> bool:
        return self._available

    async def start(
        self,
        on_change: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> bool:
        if not _supported_windows_build():
            return False
        self._on_change = on_change
        self._loop = asyncio.get_running_loop()
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager,
            )

            self._manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
            self._available = self._manager is not None
            if self._available:
                self._subscribe(self._manager, "current_session_changed")
                self._subscribe(self._manager, "sessions_changed")
            return self._available
        except Exception:
            self._manager = None
            self._available = False
            return False

    async def current_media(self) -> RawMediaPresence | None:
        if self._manager is None:
            return None
        try:
            session = self._manager.get_current_session()
            if session is None:
                self._remove_session_subscriptions()
                self._forget_session()
                return None
            if session is not self._observed_session:
                self._remove_session_subscriptions()
                self._observed_session = session
                for event_name in (
                    "media_properties_changed",
                    "playback_info_changed",
                    "timeline_properties_changed",
                ):
                    self._subscribe(session, event_name)
            properties = await session.try_get_media_properties_async()
            playback = session.get_playback_info()
            timeline = session.get_timeline_properties()
            state = _playback_state(playback)
            now = self._now().astimezone(UTC)
            identifier = normalize_text(session.source_app_user_model_id, 260) or "unknown"
            title = normalize_text(getattr(properties, "title", None), 300)
            artist = normalize_text(getattr(properties, "artist", None), 300)
            album = normalize_text(getattr(properties, "album_title", None), 300)
            kind = _media_kind(getattr(properties, "playback_type", None))
            start = _seconds(getattr(timeline, "start_time", None)) or 0.0
            end = _seconds(getattr(timeline, "end_time", None))
            min_seek = _seconds(getattr(timeline, "min_seek_time", None)) or 0.0
            max_seek = _seconds(getattr(timeline, "max_seek_time", None))
            duration = _positive_span(start, end)
            if duration is None:
                duration = _positive_span(min_seek, max_seek)
            raw_position = _seconds(getattr(timeline, "position", None))
            position = None if raw_position is None else max(0.0, raw_position - start)
            rate = _playback_rate(playback, state)
            last_updated = _as_utc_datetime(
                getattr(timeline, "last_updated_time", None)
            )
            if (
                position is not None
                and state is PlaybackState.PLAYING
                and last_updated is not None
            ):
                elapsed = max(0.0, (now - last_updated).total_seconds())
                position += elapsed * rate
            if duration is not None and position is not None:
                position = min(position, duration)
            semantic_key = (
                identifier.casefold(),
                kind,
                title,
                artist,
                album,
            )
            if semantic_key != self._semantic_key:
                self._semantic_key = semantic_key
                self._semantic_session_id = uuid4()
                self._paused_since = now if state is PlaybackState.PAUSED else None
            elif state is PlaybackState.PAUSED:
                if self._paused_since is None:
                    self._paused_since = now
            else:
                self._paused_since = None
            if (
                state is PlaybackState.PAUSED
                and self._paused_since is not None
                and (now - self._paused_since).total_seconds()
                > self.PAUSED_RETENTION_SECONDS
            ):
                self._forget_session()
                return None
            assert self._semantic_session_id is not None
            return RawMediaPresence(
                identifier=f"aumid:{identifier.casefold()}",
                player_display_name=_player_name(identifier),
                session_id=self._semantic_session_id,
                kind=kind,
                title=title,
                artist=artist,
                album=album,
                state=state,
                duration_seconds=duration,
                position_seconds=position,
                sampled_at=now,
                rate=rate,
            )
        except Exception:
            # Media integration is optional; application Presence must continue.
            return None

    async def stop(self) -> None:
        for target, event_name, token in self._event_tokens:
            with suppress(Exception):
                getattr(target, f"remove_{event_name}")(token)
        self._event_tokens.clear()
        self._observed_session = None
        self._manager = None
        self._available = False
        self._loop = None
        self._forget_session()

    def reset_continuity(self) -> None:
        self._forget_session()

    def _subscribe(self, target: Any, event_name: str) -> None:
        add = getattr(target, f"add_{event_name}", None)
        if add is None:
            return

        def changed(*_: object) -> None:
            callback = self._on_change
            loop = self._loop
            if callback is not None and loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(lambda: loop.create_task(callback()))

        try:
            token = add(changed)
            self._event_tokens.append((target, event_name, token))
        except Exception:
            pass

    def _forget_session(self) -> None:
        self._semantic_key = None
        self._semantic_session_id = None
        self._paused_since = None

    def _remove_session_subscriptions(self) -> None:
        retained: list[tuple[Any, str, Any]] = []
        for target, event_name, token in self._event_tokens:
            if target is self._manager:
                retained.append((target, event_name, token))
                continue
            with suppress(Exception):
                getattr(target, f"remove_{event_name}")(token)
        self._event_tokens = retained
        self._observed_session = None


def _supported_windows_build() -> bool:
    try:
        return platform.system() == "Windows" and int(platform.version().split(".")[-1]) >= 17763
    except (ValueError, IndexError):
        return False


def _seconds(value: object) -> float | None:
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        result = float(total_seconds())
        return result if result >= 0 else None
    return None


def _playback_state(playback: Any) -> PlaybackState:
    value = getattr(playback, "playback_status", None)
    name = str(getattr(value, "name", "")).casefold()
    numeric = _enum_integer(value)
    return (
        PlaybackState.PLAYING
        if name == "playing" or numeric == 4 or str(value).casefold() == "playing"
        else PlaybackState.PAUSED
    )


def _media_kind(value: object) -> MediaKind:
    raw = str(getattr(value, "name", value)).casefold()
    numeric = _enum_integer(value)
    if raw == "music" or numeric == 1:
        return MediaKind.MUSIC
    if raw == "video" or numeric == 2:
        return MediaKind.VIDEO
    if raw == "podcast":
        return MediaKind.PODCAST
    return MediaKind.UNKNOWN


def _enum_integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        raw = getattr(value, "value", None)
        return raw if type(raw) is int else None


def _playback_rate(playback: Any, state: PlaybackState) -> float:
    if state is not PlaybackState.PLAYING:
        return 0.0
    value = getattr(playback, "playback_rate", None)
    if value is None:
        return 1.0
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return 1.0
    return rate if math.isfinite(rate) and 0 < rate <= 4 else 1.0


def _positive_span(start: float, end: float | None) -> float | None:
    if end is None:
        return None
    span = end - start
    return span if span > 0 else None


def _as_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _player_name(identifier: str) -> str:
    tail = identifier.rsplit("!", 1)[-1].rsplit(".", 1)[-1]
    return normalize_text(tail, 120) or "媒体播放器"
