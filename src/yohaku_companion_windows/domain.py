from __future__ import annotations

import math
import re
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
class VRChatIntegrationSettings:
    enabled: bool = False
    replace_world_title: bool = True
    upload_activity: bool = True
    endpoint_url: str = ""

    def to_dict(self) -> dict[str, bool | str]:
        return {
            "enabled": self.enabled,
            "replaceWorldTitle": self.replace_world_title,
            "uploadActivity": self.upload_activity,
            "endpointURL": self.endpoint_url.strip(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> VRChatIntegrationSettings:
        endpoint = value.get("endpointURL", "")
        if not isinstance(endpoint, str):
            raise ValueError("invalid VRChat endpoint")
        return cls(
            enabled=_stored_bool(value, "enabled", False),
            replace_world_title=_stored_bool(value, "replaceWorldTitle", True),
            upload_activity=_stored_bool(value, "uploadActivity", True),
            endpoint_url=endpoint.strip(),
        )


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    file_enabled: bool = False
    vrchat_debug_enabled: bool = False
    master_enabled: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "fileEnabled": self.file_enabled,
            "vrchatDebugEnabled": self.vrchat_debug_enabled,
            "masterEnabled": self.master_enabled,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LoggingSettings:
        return cls(
            file_enabled=_stored_bool(value, "fileEnabled", False),
            vrchat_debug_enabled=_stored_bool(
                value, "vrchatDebugEnabled", False
            ),
            master_enabled=_stored_bool(value, "masterEnabled", True),
        )


@dataclass(frozen=True, slots=True)
class ApplicationIconTemplateSettings:
    enabled: bool = False
    prefix: str = ""
    suffix: str = ".webp"

    def to_dict(self) -> dict[str, bool | str]:
        return {
            "enabled": self.enabled,
            "prefix": self.prefix.strip(),
            "suffix": self.suffix.strip() or ".webp",
        }

    def normalized(self) -> ApplicationIconTemplateSettings:
        from urllib.parse import urlsplit

        prefix = self.prefix.strip()
        suffix = self.suffix.strip() or ".webp"
        if self.enabled:
            parsed = urlsplit(prefix)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("invalid icon URL prefix")
            if (
                not suffix.startswith(".")
                or any(character in suffix for character in "/\\?#%")
                or len(suffix) > 32
            ):
                raise ValueError("invalid icon URL suffix")
            normalize_https_url(f"{prefix}example{suffix}")
        return ApplicationIconTemplateSettings(self.enabled, prefix, suffix)

    @classmethod
    def from_dict(
        cls, value: dict[str, Any]
    ) -> ApplicationIconTemplateSettings:
        prefix = value.get("prefix", "")
        suffix = value.get("suffix", ".webp")
        if not isinstance(prefix, str) or not isinstance(suffix, str):
            raise ValueError("invalid application icon template")
        return cls(
            enabled=_stored_bool(value, "enabled", False),
            prefix=prefix.strip(),
            suffix=suffix.strip() or ".webp",
        ).normalized()


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
    custom_title: str | None = None
    icon_filename: str | None = None
    activity_key: str | None = None
    activity_custom_label: str | None = None
    media_artwork_url: str | None = None

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
            custom_title=normalize_text(self.custom_title, 500),
            icon_filename=normalize_icon_filename(self.icon_filename),
            activity_key=normalize_activity_key(self.activity_key),
            activity_custom_label=normalize_text(self.activity_custom_label, 80),
            media_artwork_url=normalize_artwork_url(self.media_artwork_url),
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
    icon_url: str | None = None
    activity_key: str | None = None
    activity_custom_label: str | None = None

    def __post_init__(self) -> None:
        display_name = normalize_text(self.display_name, 120)
        if display_name is None:
            raise ValueError("application display name is required")
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "window_title", normalize_text(self.window_title, 500))
        object.__setattr__(self, "icon_url", normalize_https_url(self.icon_url))
        object.__setattr__(self, "activity_key", normalize_activity_key(self.activity_key))
        object.__setattr__(
            self,
            "activity_custom_label",
            normalize_text(self.activity_custom_label, 80),
        )


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
    session_id: UUID | str
    kind: MediaKind
    title: str | None
    artist: str | None
    album: str | None
    player_display_name: str | None
    playback: SanitizedMediaPlayback
    artwork_url: str | None = None
    link_url: str | None = None

    def __post_init__(self) -> None:
        session = str(self.session_id)
        try:
            UUID(session)
        except ValueError:
            if re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", session.upper()) is None:
                raise ValueError("invalid media session identifier") from None
            session = session.upper()
        object.__setattr__(self, "session_id", session)
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
        object.__setattr__(self, "artwork_url", normalize_artwork_url(self.artwork_url))
        object.__setattr__(self, "link_url", normalize_media_link(self.link_url))


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
class CustomBroadcastSpec:
    snapshot: SanitizedPresenceSnapshot
    duration_seconds: int = 60
    apply_sensitive_rules: bool = True

    def __post_init__(self) -> None:
        if not 5 <= self.duration_seconds <= 86_400:
            raise ValueError("test broadcast duration must be 5 to 86400 seconds")


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
    vrchat_settings: VRChatIntegrationSettings = field(
        default_factory=VRChatIntegrationSettings
    )
    vrchat_api_key_present: bool = False
    vrchat_status: str = "未启用"
    test_broadcast_active: bool = False
    test_broadcast_status: str = "未运行"


_ACTIVITY_KEY = re.compile(r"^[a-z][\d.a-z-]{0,63}$")
_ICON_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize_activity_key(value: str | None) -> str | None:
    normalized = normalize_text(value, 64)
    if normalized is not None and _ACTIVITY_KEY.fullmatch(normalized) is None:
        raise ValueError("invalid activity key")
    return normalized


def normalize_icon_filename(value: str | None) -> str | None:
    normalized = normalize_text(value, 128)
    if normalized is None:
        return None
    if (
        _ICON_FILENAME.fullmatch(normalized) is None
        or ".." in normalized
        or "%" in normalized
    ):
        raise ValueError("invalid icon filename")
    return normalized


def normalize_https_url(value: str | None) -> str | None:
    from urllib.parse import urlsplit

    normalized = normalize_text(value)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or len(normalized.encode("utf-8")) > 2048
    ):
        raise ValueError("invalid HTTPS URL")
    return normalized


def normalize_artwork_url(value: str | None) -> str | None:
    from urllib.parse import parse_qsl, urlsplit

    normalized = normalize_https_url(value)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if (
        parsed.fragment
        or len(query) != 1
        or query[0][0] != "v"
        or not _SHA256.fullmatch(query[0][1])
    ):
        raise ValueError("invalid media artwork URL")
    return normalized


def normalize_media_link(value: str | None) -> str | None:
    from urllib.parse import parse_qs, urlsplit

    normalized = normalize_https_url(value)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    qq = (
        parsed.hostname == "y.qq.com"
        and parsed.query == ""
        and parsed.fragment == ""
        and re.fullmatch(r"/n/ryqq/songDetail/[A-Za-z0-9]+", parsed.path)
    )
    query = parse_qs(parsed.query)
    netease = (
        parsed.hostname == "music.163.com"
        and parsed.path == "/song"
        and parsed.fragment == ""
        and set(query) == {"id"}
        and len(query["id"]) == 1
        and query["id"][0].isdigit()
    )
    if not (qq or netease):
        raise ValueError("unsupported media link")
    return normalized


def _stored_bool(value: dict[str, Any], key: str, fallback: bool) -> bool:
    result = value.get(key, fallback)
    if type(result) is not bool:
        raise ValueError(f"invalid stored boolean: {key}")
    return result
