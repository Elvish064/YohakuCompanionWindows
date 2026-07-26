from __future__ import annotations

import ipaddress
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from .domain import (
    ClearReason,
    ConnectionMetadata,
    PresenceConfiguration,
    SanitizedPresenceSnapshot,
)

PRESENCE_SCHEMA = "yohaku.companion.presence"
PRESENCE_SCHEMA_VERSION = 2
MAXIMUM_SAFE_INTEGER = 9_007_199_254_740_991
CLIENT_VERSION_HEADER = "X-Yohaku-Companion-Version"

_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class ProtocolError(ValueError):
    pass


class NegotiationError(ProtocolError):
    pass


@dataclass(frozen=True, slots=True)
class ServerConfiguration:
    base_url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", validate_base_url(self.base_url))

    def endpoint(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


@dataclass(frozen=True, slots=True)
class Capabilities:
    server_time: datetime
    minimum_client_version: str
    presence_schema_versions: tuple[int, ...]
    moment_schema_versions: tuple[int, ...]
    live_desk: bool
    media_timeline: bool
    media_artwork: bool
    media_playback_links: bool
    presence_payload_bytes: int
    presence_requests_per_minute: int
    presence_lease_min_seconds: int
    presence_lease_max_seconds: int
    recommended_heartbeat_seconds: int
    maximum_clock_skew_seconds: int


@dataclass(frozen=True, slots=True)
class PairingClaim:
    base_url: str
    device_id: str
    device_token: str
    scopes: tuple[str, ...]
    next_sequence: int

    def __repr__(self) -> str:
        return "PairingClaim(<redacted>)"


@dataclass(frozen=True, slots=True)
class MutationResult:
    accepted_sequence: int
    response: dict[str, Any]


@dataclass(frozen=True, slots=True)
class APIError:
    status_code: int
    code: str | None
    retryable: bool
    accepted_sequence: int | None


def validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str):
        raise ProtocolError(f"invalid identifier: {field}")
    uuid_pattern = re.fullmatch(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        value,
    )
    if uuid_pattern is not None:
        try:
            UUID(value)
            return
        except ValueError:
            pass
    if not _ULID.fullmatch(value):
        raise ProtocolError(f"invalid identifier: {field}")


def validate_safe_integer(value: int, field: str) -> None:
    if type(value) is not int or not 0 <= value <= MAXIMUM_SAFE_INTEGER:
        raise ProtocolError(f"invalid safe integer: {field}")


def validate_base_url(value: str) -> str:
    normalized = value.strip()
    if any(ord(character) < 32 for character in normalized):
        raise ProtocolError("invalid server URL")
    parsed = urlsplit(normalized)
    if (
        not parsed.scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProtocolError("invalid server URL")
    scheme = parsed.scheme.casefold()
    try:
        _ = parsed.port
    except ValueError as error:
        raise ProtocolError("invalid server URL") from error
    if scheme not in {"http", "https"}:
        raise ProtocolError("server URL must use HTTP or HTTPS")
    if scheme == "http":
        literal_is_lan = lan_address_literal(parsed.hostname)
        if literal_is_lan is False:
            raise ProtocolError("HTTP server URL must use a private network address")
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def lan_address_literal(host: str) -> bool | None:
    """Return LAN safety for an IP literal, or None when DNS is required."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def wire_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolError("wire timestamp must be timezone-aware")
    utc_value = value.astimezone(UTC)
    milliseconds = utc_value.microsecond // 1000
    return utc_value.strftime("%Y-%m-%dT%H:%M:%S") + f".{milliseconds:03d}Z"


def parse_wire_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value
    ):
        raise ProtocolError(f"invalid wire timestamp: {field}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise ProtocolError(f"invalid wire timestamp: {field}") from error
    if wire_timestamp(parsed) != value:
        raise ProtocolError(f"non-canonical wire timestamp: {field}")
    return parsed


def encode_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def decode_json(value: bytes, field: str = "response") -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f"invalid JSON: {field}") from error
    if not isinstance(decoded, dict):
        raise ProtocolError(f"invalid object: {field}")
    return decoded


def make_presence_request(
    snapshot: SanitizedPresenceSnapshot,
    device_id: str,
    sequence: int,
    configuration: PresenceConfiguration,
    requested_lease_seconds: int = 90,
    request_id: str | None = None,
) -> dict[str, Any]:
    request_id = request_id or str(uuid4())
    validate_identifier(request_id, "meta.requestId")
    validate_identifier(device_id, "meta.deviceId")
    validate_safe_integer(sequence, "meta.sequence")

    application: dict[str, Any] | None = None
    if snapshot.application is not None:
        app = snapshot.application
        _validate_text(app.display_name, "data.application.displayName", 120)
        if app.window_title is not None:
            _validate_text(app.window_title, "data.application.window.title", 500)
        if app.activity_custom_label is not None:
            _validate_text(
                app.activity_custom_label,
                "data.application.activity.customLabel",
                80,
            )
        application = {
            "displayName": app.display_name,
            "activity": (
                None
                if app.activity_key is None and app.activity_custom_label is None
                else {
                    "key": app.activity_key,
                    "customLabel": app.activity_custom_label,
                }
            ),
            "window": None if app.window_title is None else {"title": app.window_title},
            "icon": None if app.icon_url is None else {"url": app.icon_url},
        }

    media: dict[str, Any] | None = None
    if snapshot.media is not None:
        source = snapshot.media
        validate_identifier(str(source.session_id), "data.media.sessionId")
        for field, value, limit in (
            ("title", source.title, 300),
            ("artist", source.artist, 300),
            ("album", source.album, 300),
            ("player.displayName", source.player_display_name, 120),
        ):
            if value is not None:
                _validate_text(value, f"data.media.{field}", limit)
        duration = _milliseconds(source.playback.duration_seconds, "durationMs")
        position = _milliseconds(source.playback.position_seconds, "positionMs")
        if duration is not None and position is not None:
            position = min(position, duration)
        media = {
            "sessionId": str(source.session_id),
            "kind": source.kind.value,
            "title": source.title,
            "artist": source.artist,
            "album": source.album,
            "player": (
                None
                if source.player_display_name is None
                else {"displayName": source.player_display_name}
            ),
            "playback": {
                "state": source.playback.state.value,
                "durationMs": duration,
                "positionMs": position,
                "sampledAt": wire_timestamp(source.playback.sampled_at),
                "rate": source.playback.rate,
            },
        }
        if configuration.supports_media_artwork:
            media["artwork"] = (
                None if source.artwork_url is None else {"url": source.artwork_url}
            )
        if configuration.supports_media_playback_links:
            media["link"] = None if source.link_url is None else {"url": source.link_url}

    lease = min(
        max(requested_lease_seconds, configuration.minimum_lease_seconds),
        configuration.maximum_lease_seconds,
    )
    return {
        "meta": _request_meta(request_id, device_id, sequence, snapshot.observed_at),
        "data": {
            "availability": snapshot.availability.value,
            "lease": {"ttlSeconds": lease},
            "application": application,
            "media": media,
        },
    }


def make_clear_request(
    reason: ClearReason,
    observed_at: datetime,
    device_id: str,
    sequence: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    request_id = request_id or str(uuid4())
    validate_identifier(request_id, "meta.requestId")
    validate_identifier(device_id, "meta.deviceId")
    validate_safe_integer(sequence, "meta.sequence")
    return {
        "meta": _request_meta(request_id, device_id, sequence, observed_at),
        "data": {"reason": reason.value},
    }


def _request_meta(
    request_id: str, device_id: str, sequence: int, observed_at: datetime
) -> dict[str, Any]:
    return {
        "schema": PRESENCE_SCHEMA,
        "schemaVersion": PRESENCE_SCHEMA_VERSION,
        "requestId": request_id,
        "deviceId": device_id,
        "sequence": sequence,
        "observedAt": wire_timestamp(observed_at),
    }


def _milliseconds(value: float | None, field: str) -> int | None:
    if value is None:
        return None
    result = math.floor(value * 1000 + 0.5)
    validate_safe_integer(result, f"data.media.playback.{field}")
    return result


def _validate_text(value: str, field: str, maximum: int) -> None:
    if len(value) > maximum:
        raise ProtocolError(f"text too long: {field}")


def parse_capabilities_response(payload: dict[str, Any]) -> Capabilities:
    _validate_response_meta(payload.get("meta"), None)
    server_time = parse_wire_timestamp(payload["meta"].get("serverTime"), "meta.serverTime")
    data = _required_dict(payload, "data")
    features = _required_dict(data, "features")
    limits = _required_dict(data, "limits")
    try:
        minimum = data["minimumClientVersion"]
        presence_versions = data["presenceSchemaVersions"]
        moment_versions = data["momentSchemaVersions"]
        if not isinstance(minimum, str):
            raise TypeError
        if not _integer_array(presence_versions) or not _integer_array(moment_versions):
            raise TypeError
        for key in ("liveDesk", "mediaTimeline"):
            if type(features.get(key)) is not bool:
                raise TypeError
        for key in ("mediaArtwork", "mediaPlaybackLinks"):
            if key in features and type(features[key]) is not bool:
                raise TypeError
        integer_limits = (
            "presencePayloadBytes",
            "presenceRequestsPerMinute",
            "presenceLeaseMinSeconds",
            "presenceLeaseMaxSeconds",
            "recommendedHeartbeatSeconds",
            "maximumClockSkewSeconds",
        )
        if any(type(limits.get(key)) is not int for key in integer_limits):
            raise TypeError
        capabilities = Capabilities(
            server_time=server_time,
            minimum_client_version=minimum,
            presence_schema_versions=tuple(presence_versions),
            moment_schema_versions=tuple(moment_versions),
            live_desk=features["liveDesk"],
            media_timeline=features["mediaTimeline"],
            media_artwork=features.get("mediaArtwork") is True,
            media_playback_links=features.get("mediaPlaybackLinks") is True,
            presence_payload_bytes=limits["presencePayloadBytes"],
            presence_requests_per_minute=limits["presenceRequestsPerMinute"],
            presence_lease_min_seconds=limits["presenceLeaseMinSeconds"],
            presence_lease_max_seconds=limits["presenceLeaseMaxSeconds"],
            recommended_heartbeat_seconds=limits["recommendedHeartbeatSeconds"],
            maximum_clock_skew_seconds=limits["maximumClockSkewSeconds"],
        )
    except (KeyError, TypeError) as error:
        raise ProtocolError("invalid capabilities") from error
    return capabilities


def negotiate_presence(capabilities: Capabilities, client_version: str) -> PresenceConfiguration:
    if _compare_semver(client_version, capabilities.minimum_client_version) < 0:
        raise NegotiationError("client update required")
    if PRESENCE_SCHEMA_VERSION not in capabilities.presence_schema_versions:
        raise NegotiationError("presence schema unsupported")
    if not capabilities.live_desk:
        raise NegotiationError("Live Desk unavailable")
    values = (
        capabilities.presence_payload_bytes,
        capabilities.presence_requests_per_minute,
        capabilities.presence_lease_min_seconds,
        capabilities.presence_lease_max_seconds,
        capabilities.recommended_heartbeat_seconds,
    )
    if any(value <= 0 for value in values):
        raise NegotiationError("invalid capabilities")
    if not (
        capabilities.presence_lease_min_seconds
        <= capabilities.recommended_heartbeat_seconds
        <= capabilities.presence_lease_max_seconds
    ):
        raise NegotiationError("invalid capabilities")
    if capabilities.maximum_clock_skew_seconds < 0:
        raise NegotiationError("invalid capabilities")
    return PresenceConfiguration(
        maximum_payload_bytes=capabilities.presence_payload_bytes,
        requests_per_minute=capabilities.presence_requests_per_minute,
        minimum_lease_seconds=capabilities.presence_lease_min_seconds,
        maximum_lease_seconds=capabilities.presence_lease_max_seconds,
        recommended_heartbeat_seconds=capabilities.recommended_heartbeat_seconds,
        maximum_clock_skew_seconds=capabilities.maximum_clock_skew_seconds,
        supports_media_timeline=capabilities.media_timeline,
        supports_media_artwork=capabilities.media_artwork,
        supports_media_playback_links=capabilities.media_playback_links,
    )


def parse_pairing_claim(payload: dict[str, Any], base_url: str) -> PairingClaim:
    data = _required_dict(payload, "data")
    try:
        device_id = data["deviceId"]
        raw_token = data["deviceToken"]
        raw_scopes = data["scopes"]
        next_sequence = data["nextSequence"]
        if not isinstance(device_id, str) or not isinstance(raw_token, str):
            raise TypeError
        if not isinstance(raw_scopes, list) or not all(
            isinstance(item, str) for item in raw_scopes
        ):
            raise TypeError
        if type(next_sequence) is not int:
            raise TypeError
        token = raw_token.strip()
        scopes = tuple(sorted(set(raw_scopes)))
    except (KeyError, TypeError) as error:
        raise ProtocolError("invalid pairing response") from error
    validate_identifier(device_id, "data.deviceId")
    validate_safe_integer(next_sequence, "data.nextSequence")
    if not token:
        raise ProtocolError("missing device token")
    return PairingClaim(base_url, device_id, token, scopes, next_sequence)


def parse_mutation_response(payload: dict[str, Any], request_id: str) -> MutationResult:
    _validate_response_meta(payload.get("meta"), request_id)
    data = _required_dict(payload, "data")
    try:
        accepted = data["acceptedSequence"]
        if type(accepted) is not int:
            raise TypeError
    except (KeyError, TypeError) as error:
        raise ProtocolError("invalid accepted sequence") from error
    validate_safe_integer(accepted, "data.acceptedSequence")
    parse_wire_timestamp(data.get("receivedAt"), "data.receivedAt")
    state = _required_dict(data, "state")
    if int(state.get("schemaVersion", -1)) != PRESENCE_SCHEMA_VERSION:
        raise ProtocolError("invalid public schema version")
    epoch = state.get("epoch")
    if not isinstance(epoch, str):
        raise ProtocolError("invalid public epoch")
    validate_identifier(epoch, "data.state.epoch")
    revision = state.get("revision")
    if type(revision) is not int:
        raise ProtocolError("invalid public revision")
    validate_safe_integer(revision, "data.state.revision")
    if "projection" not in state:
        raise ProtocolError("missing public projection")
    _validate_public_projection(state["projection"])
    return MutationResult(accepted, payload)


def parse_api_error(
    payload: dict[str, Any] | None,
    status_code: int,
    request_id: str | None = None,
) -> APIError:
    if payload is None:
        return APIError(status_code, None, False, None)
    if request_id is not None:
        _validate_response_meta(payload.get("meta"), request_id)
    try:
        error = _required_dict(payload, "error")
        code = error["code"]
        retryable = error["retryable"]
        if not isinstance(code, str) or type(retryable) is not bool:
            raise TypeError
        accepted_value = error.get("acceptedSequence")
        if accepted_value is not None and type(accepted_value) is not int:
            raise TypeError
        accepted = accepted_value
        if accepted is not None:
            validate_safe_integer(accepted, "error.acceptedSequence")
        return APIError(status_code, code, retryable, accepted)
    except (KeyError, TypeError, ValueError, ProtocolError):
        return APIError(status_code, None, False, None)


def metadata_from_claim(claim: PairingClaim) -> ConnectionMetadata:
    return ConnectionMetadata(
        base_url=claim.base_url,
        device_id=claim.device_id,
        scopes=claim.scopes,
        pairing_next_sequence=claim.next_sequence,
        live_desk_enabled=False,
    )


def validate_clock_skew(server_time: datetime, maximum_seconds: int) -> None:
    if maximum_seconds < 0:
        raise ProtocolError("invalid maximum clock skew")
    skew = abs((datetime.now(UTC) - server_time.astimezone(UTC)).total_seconds())
    if skew > maximum_seconds:
        raise ProtocolError("server clock skew exceeds negotiated limit")


def _validate_response_meta(value: Any, request_id: str | None) -> None:
    if not isinstance(value, dict):
        raise ProtocolError("missing response metadata")
    if value.get("schema") != PRESENCE_SCHEMA:
        raise ProtocolError("incompatible response schema")
    if value.get("schemaVersion") != PRESENCE_SCHEMA_VERSION:
        raise ProtocolError("incompatible response schema version")
    actual_request_id = value.get("requestId")
    if not isinstance(actual_request_id, str):
        raise ProtocolError("invalid response request ID")
    validate_identifier(actual_request_id, "meta.requestId")
    if request_id is not None and actual_request_id != request_id:
        raise ProtocolError("response request ID mismatch")
    parse_wire_timestamp(value.get("serverTime"), "meta.serverTime")


def _required_dict(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ProtocolError(f"missing object: {key}")
    return result


def _integer_array(value: Any) -> bool:
    return isinstance(value, list) and all(type(item) is int and item > 0 for item in value)


def _validate_public_projection(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ProtocolError("invalid public projection")
    if value.get("availability") not in ("idle", "active"):
        raise ProtocolError("invalid public availability")
    parse_wire_timestamp(value.get("updatedAt"), "data.state.projection.updatedAt")
    parse_wire_timestamp(value.get("expiresAt"), "data.state.projection.expiresAt")
    if "application" not in value or "media" not in value:
        raise ProtocolError("missing public projection field")
    _validate_public_application(value["application"])
    _validate_public_media(value["media"])


def _validate_public_application(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ProtocolError("invalid public application")
    _required_text(value.get("displayName"), "public application display name", 120)
    for key in ("activity", "window", "icon"):
        if key not in value:
            raise ProtocolError(f"missing public application field: {key}")
    activity = value["activity"]
    if activity is not None:
        if not isinstance(activity, dict):
            raise ProtocolError("invalid public activity")
        if "key" not in activity or "customLabel" not in activity:
            raise ProtocolError("missing public activity field")
        _nullable_text(activity["key"], "public activity key", 64)
        _nullable_text(activity["customLabel"], "public activity label", 80)
    window = value["window"]
    if window is not None:
        if not isinstance(window, dict):
            raise ProtocolError("invalid public window")
        _required_text(window.get("title"), "public window title", 500)
    icon = value["icon"]
    if icon is not None:
        if not isinstance(icon, dict):
            raise ProtocolError("invalid public icon")
        _required_text(icon.get("url"), "public icon URL", 2048)


def _validate_public_media(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ProtocolError("invalid public media")
    session_id = value.get("sessionId")
    if not isinstance(session_id, str):
        raise ProtocolError("invalid public media session")
    validate_identifier(session_id, "data.state.projection.media.sessionId")
    if value.get("kind") not in ("music", "podcast", "video", "unknown"):
        raise ProtocolError("invalid public media kind")
    for key in ("title", "artist", "album", "player"):
        if key not in value:
            raise ProtocolError(f"missing public media field: {key}")
    for key in ("title", "artist", "album"):
        _nullable_text(value[key], f"public media {key}", 300)
    player = value["player"]
    if player is not None:
        if not isinstance(player, dict):
            raise ProtocolError("invalid public player")
        _required_text(player.get("displayName"), "public player name", 120)
    playback = value.get("playback")
    if not isinstance(playback, dict):
        raise ProtocolError("invalid public playback")
    if playback.get("state") not in ("playing", "paused"):
        raise ProtocolError("invalid public playback state")
    for key in ("durationMs", "positionMs"):
        if key not in playback:
            raise ProtocolError(f"missing public playback field: {key}")
        number = playback[key]
        if number is not None:
            validate_safe_integer(number, f"public playback {key}")
    parse_wire_timestamp(playback.get("anchorAt"), "public playback anchorAt")
    rate = playback.get("rate")
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not math.isfinite(rate):
        raise ProtocolError("invalid public playback rate")
    for key in ("artwork", "link"):
        asset = value.get(key)
        if asset is not None:
            if not isinstance(asset, dict):
                raise ProtocolError(f"invalid public media {key}")
            _required_text(asset.get("url"), f"public media {key} URL", 2048)


def _nullable_text(value: Any, field: str, maximum: int) -> None:
    if value is not None:
        _required_text(value, field, maximum)


def _required_text(value: Any, field: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProtocolError(f"invalid text: {field}")


def _compare_semver(lhs: str, rhs: str) -> int:
    left = _parse_semver(lhs)
    right = _parse_semver(rhs)
    left_core, left_pre = left
    right_core, right_pre = right
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    if left_pre == right_pre:
        return 0
    if left_pre is None:
        return 1
    if right_pre is None:
        return -1
    for left_item, right_item in zip(left_pre, right_pre, strict=False):
        if left_item == right_item:
            continue
        if isinstance(left_item, int) and isinstance(right_item, str):
            return -1
        if isinstance(left_item, str) and isinstance(right_item, int):
            return 1
        if isinstance(left_item, int) and isinstance(right_item, int):
            return -1 if left_item < right_item else 1
        assert isinstance(left_item, str) and isinstance(right_item, str)
        return -1 if left_item < right_item else 1
    return (len(left_pre) > len(right_pre)) - (len(left_pre) < len(right_pre))


def _parse_semver(value: str) -> tuple[tuple[int, int, int], tuple[int | str, ...] | None]:
    match = _SEMVER.fullmatch(value.strip())
    if match is None:
        raise NegotiationError("invalid semantic version")
    prerelease: tuple[int | str, ...] | None = None
    if match.group(4) is not None:
        items: list[int | str] = []
        for item in match.group(4).split("."):
            if item.isdigit():
                if len(item) > 1 and item.startswith("0"):
                    raise NegotiationError("invalid semantic version")
                items.append(int(item))
            else:
                items.append(item)
        prerelease = tuple(items)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))), prerelease
