from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class ShareMode(StrEnum):
    INHERIT = "inherit"
    SHARE = "share"
    HIDE = "hide"


class SensitiveField(StrEnum):
    APPLICATION_NAME = "applicationName"
    WINDOW_TITLE = "windowTitle"
    MEDIA_TITLE = "mediaTitle"
    MEDIA_ARTIST = "mediaArtist"
    MEDIA_ALBUM = "mediaAlbum"
    PLAYER_NAME = "playerName"


class SensitiveAction(StrEnum):
    MASK_MATCH = "maskMatch"
    HIDE_FIELD = "hideField"
    HIDE_CONTEXT = "hideContext"


class SensitivePatternKind(StrEnum):
    CONTAINS = "contains"
    EXACT = "exact"
    PREFIX = "prefix"
    SUFFIX = "suffix"
    ANY_WORD = "anyWord"
    NUMBER = "number"
    EMAIL = "email"
    URL = "url"


class MediaKind(StrEnum):
    MUSIC = "music"
    PODCAST = "podcast"
    VIDEO = "video"
    UNKNOWN = "unknown"


class PlaybackState(StrEnum):
    PLAYING = "playing"
    PAUSED = "paused"


class Availability(StrEnum):
    IDLE = "idle"
    ACTIVE = "active"


class RuntimeState(StrEnum):
    DISABLED = "disabled"
    CONNECTING = "connecting"
    UPDATE_REQUIRED = "update_required"
    FEATURE_UNAVAILABLE = "feature_unavailable"
    ACTIVE = "active"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"


class ClearReason(StrEnum):
    PAUSED = "paused"
    SLEEP = "sleep"
    SHUTDOWN = "shutdown"
    PRIVACY_CHANGED = "privacyChanged"
    CONNECTION_REMOVED = "connectionRemoved"


PRESENCE_SCOPE = "companion:presence:write"


def normalize_text(value: str | None, maximum: int | None = None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        return None
    if maximum is not None:
        normalized = normalized[:maximum]
    return normalized


def normalize_time(value: float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0:
        return None
    return result


@dataclass(frozen=True, slots=True)
class SourceSettings:
    applications: bool = True
    window_titles: bool = False
    media: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "applications": self.applications,
            "windowTitles": self.window_titles,
            "media": self.media,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourceSettings:
        return cls(
            applications=_stored_bool(value, "applications", True),
            window_titles=_stored_bool(value, "windowTitles", False),
            media=_stored_bool(value, "media", True),
        )


@dataclass(frozen=True, slots=True)
class PrivacyDefaults:
    application: bool = True
    window_title: bool = False
    media: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "application": self.application,
            "windowTitle": self.window_title,
            "media": self.media,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PrivacyDefaults:
        return cls(
            application=_stored_bool(value, "application", True),
            window_title=_stored_bool(value, "windowTitle", False),
            media=_stored_bool(value, "media", True),
        )


@dataclass(frozen=True, slots=True)
class ApplicationRule:
    identifier: str
    display_name: str
    application: ShareMode = ShareMode.INHERIT
    window_title: ShareMode = ShareMode.INHERIT
    media: ShareMode = ShareMode.INHERIT
    alias: str | None = None

    def normalized(self) -> ApplicationRule:
        identifier = unicodedata.normalize("NFC", self.identifier.strip()).casefold()
        display_name = normalize_text(self.display_name, 120) or identifier
        return ApplicationRule(
            identifier=identifier,
            display_name=display_name,
            application=self.application,
            window_title=self.window_title,
            media=self.media,
            alias=normalize_text(self.alias, 120),
        )


@dataclass(frozen=True, slots=True)
class SensitivePatternModule:
    kind: SensitivePatternKind
    value: str = ""

    def normalized(self) -> SensitivePatternModule:
        kind = SensitivePatternKind(self.kind)
        value = unicodedata.normalize("NFC", self.value.strip())[:256]
        if kind in {
            SensitivePatternKind.CONTAINS,
            SensitivePatternKind.EXACT,
            SensitivePatternKind.PREFIX,
            SensitivePatternKind.SUFFIX,
            SensitivePatternKind.ANY_WORD,
        } and not value:
            raise ValueError("pattern module value is required")
        return SensitivePatternModule(kind, value)


@dataclass(frozen=True, slots=True)
class SensitiveTextRule:
    identifier: str
    name: str
    pattern: str
    fields: tuple[SensitiveField, ...]
    action: SensitiveAction = SensitiveAction.HIDE_FIELD
    enabled: bool = True
    ignore_case: bool = True
    sort_order: int = 0
    pattern_modules: tuple[SensitivePatternModule, ...] = ()

    def normalized(self) -> SensitiveTextRule:
        try:
            identifier = str(UUID(self.identifier))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("invalid sensitive rule identifier") from error
        name = normalize_text(self.name, 80)
        if name is None:
            raise ValueError("sensitive rule name is required")
        pattern = unicodedata.normalize("NFC", self.pattern)
        if not pattern or len(pattern) > 256:
            raise ValueError("sensitive rule pattern must contain 1 to 256 characters")
        fields = tuple(dict.fromkeys(SensitiveField(field) for field in self.fields))
        if not fields:
            raise ValueError("sensitive rule must select at least one field")
        if not 0 <= self.sort_order < 50:
            raise ValueError("invalid sensitive rule sort order")
        modules = tuple(module.normalized() for module in self.pattern_modules)
        if len(modules) > 20:
            raise ValueError("at most 20 pattern modules are allowed")
        return SensitiveTextRule(
            identifier=identifier,
            name=name,
            pattern=pattern,
            fields=fields,
            action=SensitiveAction(self.action),
            enabled=bool(self.enabled),
            ignore_case=bool(self.ignore_case),
            sort_order=self.sort_order,
            pattern_modules=modules,
        )


@dataclass(frozen=True, slots=True)
class ConnectionMetadata:
    base_url: str
    device_id: str
    scopes: tuple[str, ...]
    pairing_next_sequence: int
    live_desk_enabled: bool = False
    storage_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "storageVersion": self.storage_version,
            "baseURL": self.base_url,
            "deviceID": self.device_id,
            "scopes": list(self.scopes),
            "pairingNextSequence": self.pairing_next_sequence,
            "isLiveDeskEnabled": self.live_desk_enabled,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ConnectionMetadata:
        if value.get("storageVersion") != 1:
            raise ValueError("unsupported connection metadata")
        if not isinstance(value.get("baseURL"), str):
            raise ValueError("invalid base URL")
        if not isinstance(value.get("deviceID"), str):
            raise ValueError("invalid device ID")
        if not isinstance(value.get("scopes"), list) or not all(
            isinstance(item, str) for item in value["scopes"]
        ):
            raise ValueError("invalid scopes")
        if type(value.get("pairingNextSequence")) is not int:
            raise ValueError("invalid next sequence")
        if type(value.get("isLiveDeskEnabled")) is not bool:
            raise ValueError("invalid Live Desk state")
        return cls(
            base_url=value["baseURL"],
            device_id=value["deviceID"],
            scopes=tuple(sorted(set(value["scopes"]))),
            pairing_next_sequence=value["pairingNextSequence"],
            live_desk_enabled=value["isLiveDeskEnabled"],
        )


@dataclass(frozen=True, slots=True)
class RawApplicationIdentity:
    identifier: str
    display_name: str
    window_handle: int


@dataclass(frozen=True, slots=True)
class RawMediaPresence:
    identifier: str
    player_display_name: str
    session_id: UUID
    kind: MediaKind
    title: str | None
    artist: str | None
    album: str | None
    state: PlaybackState
    duration_seconds: float | None
    position_seconds: float | None
    sampled_at: datetime
    rate: float


@dataclass(frozen=True, slots=True)
class SanitizedApplicationPresence:
    display_name: str
    window_title: str | None = None

    def __post_init__(self) -> None:
        display_name = normalize_text(self.display_name, 120)
        if display_name is None:
            raise ValueError("application display name is required")
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "window_title", normalize_text(self.window_title, 500))


@dataclass(frozen=True, slots=True)
class SanitizedMediaPlayback:
    state: PlaybackState
    duration_seconds: float | None
    position_seconds: float | None
    sampled_at: datetime
    rate: float

    def __post_init__(self) -> None:
        duration = normalize_time(self.duration_seconds)
        position = normalize_time(self.position_seconds)
        if duration is not None and position is not None:
            position = min(position, duration)
        if not math.isfinite(self.rate) or not 0 <= self.rate <= 4:
            raise ValueError("invalid playback rate")
        if self.state is PlaybackState.PAUSED and self.rate != 0:
            raise ValueError("paused playback rate must be zero")
        if self.state is PlaybackState.PLAYING and self.rate <= 0:
            raise ValueError("playing playback rate must be positive")
        if self.sampled_at.tzinfo is None or self.sampled_at.utcoffset() is None:
            raise ValueError("sample time must be timezone-aware")
        sampled_at = self.sampled_at.astimezone(UTC)
        object.__setattr__(self, "duration_seconds", duration)
        object.__setattr__(self, "position_seconds", position)
        object.__setattr__(self, "sampled_at", sampled_at)


@dataclass(frozen=True, slots=True)
class SanitizedMediaPresence:
    session_id: UUID
    kind: MediaKind
    title: str | None
    artist: str | None
    album: str | None
    player_display_name: str | None
    playback: SanitizedMediaPlayback

    def __post_init__(self) -> None:
        title = normalize_text(self.title, 300)
        artist = normalize_text(self.artist, 300)
        if title is None and artist is None:
            raise ValueError("media identity is required")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "artist", artist)
        object.__setattr__(self, "album", normalize_text(self.album, 300))
        object.__setattr__(
            self,
            "player_display_name",
            normalize_text(self.player_display_name, 120),
        )


@dataclass(frozen=True, slots=True)
class SanitizedPresenceSnapshot:
    observed_at: datetime
    application: SanitizedApplicationPresence | None
    media: SanitizedMediaPresence | None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observation time must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))

    @property
    def availability(self) -> Availability:
        if self.application is None and self.media is None:
            return Availability.IDLE
        return Availability.ACTIVE

    def semantic_fingerprint(self) -> tuple[Any, ...]:
        media = self.media
        return (
            self.application,
            None
            if media is None
            else (
                media.kind,
                media.title,
                media.artist,
                media.album,
                media.player_display_name,
                media.playback.state,
                media.playback.duration_seconds,
                media.playback.rate,
            ),
        )

    def consent_projection(self) -> tuple[Any, ...]:
        return self.semantic_fingerprint()


@dataclass(frozen=True, slots=True)
class PresenceConfiguration:
    maximum_payload_bytes: int
    requests_per_minute: int
    minimum_lease_seconds: int
    maximum_lease_seconds: int
    recommended_heartbeat_seconds: int
    maximum_clock_skew_seconds: int
    supports_media_timeline: bool
    supports_media_artwork: bool = False
    supports_media_playback_links: bool = False


@dataclass(frozen=True, slots=True)
class RuleCandidate:
    identifier: str
    display_name: str


@dataclass(slots=True)
class ServiceViewState:
    connection: ConnectionMetadata | None = None
    preview: SanitizedPresenceSnapshot | None = None
    preview_current: bool = False
    runtime_state: RuntimeState = RuntimeState.DISABLED
    paused: bool = False
    busy: bool = False
    notice: str | None = None
    rule_candidates: tuple[RuleCandidate, ...] = field(default_factory=tuple)


def _stored_bool(value: dict[str, Any], key: str, fallback: bool) -> bool:
    result = value.get(key, fallback)
    if type(result) is not bool:
        raise ValueError(f"invalid stored boolean: {key}")
    return result
