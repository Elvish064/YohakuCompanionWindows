from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yohaku_companion_windows.media_capture import WinRTMediaProvider


class Properties:
    title = "Track"
    artist = "Artist"
    album_title = "Album"
    playback_type = "music"


class Playback:
    playback_status = "playing"
    playback_rate = 1.0


class Timeline:
    start_time = timedelta(0)
    end_time = timedelta(seconds=180)
    position = timedelta(seconds=10)
    min_seek_time = timedelta(0)
    max_seek_time = timedelta(0)
    last_updated_time: datetime | None = None


class Session:
    source_app_user_model_id = "Vendor.Player!App"

    async def try_get_media_properties_async(self) -> Properties:
        return Properties()

    def get_playback_info(self) -> Playback:
        return Playback()

    def get_timeline_properties(self) -> Timeline:
        return Timeline()


class Manager:
    def __init__(self) -> None:
        self.session = Session()

    def get_current_session(self) -> Session | None:
        return self.session


@pytest.mark.asyncio
async def test_semantic_session_uuid_ignores_natural_position_progress() -> None:
    provider = WinRTMediaProvider()
    manager = Manager()
    provider._manager = manager
    first = await provider.current_media()
    assert first is not None
    Timeline.position = timedelta(seconds=25)
    second = await provider.current_media()
    assert second is not None
    assert first.session_id == second.session_id
    Properties.title = "Next Track"
    third = await provider.current_media()
    assert third is not None
    assert third.session_id != second.session_id


@pytest.mark.asyncio
async def test_disappearing_or_long_paused_session_is_cleared() -> None:
    provider = WinRTMediaProvider()
    manager = Manager()
    provider._manager = manager
    Playback.playback_status = "paused"
    assert await provider.current_media() is not None
    provider._paused_since = datetime.now(UTC) - timedelta(seconds=301)
    assert await provider.current_media() is None
    manager.session = None  # type: ignore[assignment]
    assert await provider.current_media() is None


class NumericEnum:
    def __init__(self, name: str, value: int) -> None:
        self.name = name
        self.value = value

    def __int__(self) -> int:
        return self.value

    def __str__(self) -> str:
        return str(self.value)


@pytest.mark.asyncio
async def test_numeric_pywinrt_enums_and_timeline_anchor_advance() -> None:
    now = datetime(2026, 7, 19, 3, 0, 10, tzinfo=UTC)
    provider = WinRTMediaProvider(lambda: now)
    provider._manager = Manager()
    Playback.playback_status = NumericEnum("PLAYING", 4)
    Playback.playback_rate = 1.25
    Properties.playback_type = NumericEnum("VIDEO", 2)
    Timeline.position = timedelta(0)
    Timeline.last_updated_time = now - timedelta(seconds=8)
    Timeline.end_time = timedelta(seconds=100)
    result = await provider.current_media()
    assert result is not None
    assert result.state.value == "playing"
    assert result.kind.value == "video"
    assert result.rate == 1.25
    assert result.position_seconds == 10


@pytest.mark.asyncio
async def test_paused_does_not_advance_and_seek_range_supplies_duration() -> None:
    now = datetime(2026, 7, 19, 3, 0, 10, tzinfo=UTC)
    provider = WinRTMediaProvider(lambda: now)
    provider._manager = Manager()
    Playback.playback_status = NumericEnum("PAUSED", 5)
    Playback.playback_rate = 2.0
    Timeline.position = timedelta(seconds=12)
    Timeline.last_updated_time = now - timedelta(seconds=30)
    Timeline.end_time = timedelta(0)
    Timeline.min_seek_time = timedelta(seconds=2)
    Timeline.max_seek_time = timedelta(seconds=202)
    result = await provider.current_media()
    assert result is not None
    assert result.state.value == "paused"
    assert result.rate == 0
    assert result.position_seconds == 12
    assert result.duration_seconds == 200
