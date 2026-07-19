from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from tests.helpers import DEVICE_ID, REQUEST_ID, capabilities_payload, mutation_payload, now
from yohaku_companion_windows.domain import (
    MediaKind,
    PlaybackState,
    PresenceConfiguration,
    SanitizedApplicationPresence,
    SanitizedMediaPlayback,
    SanitizedMediaPresence,
    SanitizedPresenceSnapshot,
)
from yohaku_companion_windows.protocol import (
    NegotiationError,
    ProtocolError,
    ServerConfiguration,
    decode_json,
    encode_json,
    make_presence_request,
    negotiate_presence,
    parse_api_error,
    parse_capabilities_response,
    parse_mutation_response,
    wire_timestamp,
)


def configuration() -> PresenceConfiguration:
    return PresenceConfiguration(32768, 60, 30, 120, 60, 60, True, True, True)


def test_explicit_nulls_unicode_nfc_and_millisecond_timeline() -> None:
    media = SanitizedMediaPresence(
        UUID("123e4567-e89b-12d3-a456-426614174099"),
        MediaKind.MUSIC,
        "Cafe\u0301",
        None,
        None,
        "播放器",
        SanitizedMediaPlayback(
            PlaybackState.PLAYING,
            10.0,
            12.3456,
            now(),
            1.0,
        ),
    )
    snapshot = SanitizedPresenceSnapshot(
        now(), SanitizedApplicationPresence("编辑器", None), media
    )
    value = make_presence_request(
        snapshot, DEVICE_ID, 8, configuration(), request_id=REQUEST_ID
    )
    assert value["data"]["application"] == {
        "displayName": "编辑器",
        "activity": None,
        "window": None,
        "icon": None,
    }
    assert value["data"]["media"]["title"] == "Café"
    assert value["data"]["media"]["playback"]["durationMs"] == 10_000
    assert value["data"]["media"]["playback"]["positionMs"] == 10_000
    assert value["data"]["media"]["artwork"] is None
    assert value["data"]["media"]["link"] is None
    encoded = encode_json(value)
    assert b'"activity":null' in encoded
    assert decode_json(encoded)["meta"]["sequence"] == 8


def test_idle_has_explicit_application_and_media_null() -> None:
    value = make_presence_request(
        SanitizedPresenceSnapshot(now(), None, None),
        DEVICE_ID,
        0,
        configuration(),
        request_id=REQUEST_ID,
    )
    assert value["data"]["availability"] == "idle"
    assert value["data"]["application"] is None
    assert value["data"]["media"] is None


def test_wire_timestamp_is_rfc3339_milliseconds() -> None:
    assert wire_timestamp(now()) == "2026-07-18T01:02:03.456Z"
    assert wire_timestamp(datetime(2026, 1, 1, tzinfo=UTC)).endswith(".000Z")


def test_media_millisecond_rounding_matches_protocol() -> None:
    playback = SanitizedMediaPlayback(
        PlaybackState.PLAYING, 2.0, 1.2345, now(), 1.0
    )
    media = SanitizedMediaPresence(
        UUID("123e4567-e89b-12d3-a456-426614174099"),
        MediaKind.MUSIC,
        "Track",
        None,
        None,
        None,
        playback,
    )
    value = make_presence_request(
        SanitizedPresenceSnapshot(now(), None, media),
        DEVICE_ID,
        0,
        configuration(),
        request_id=REQUEST_ID,
    )
    assert value["data"]["media"]["playback"]["positionMs"] == 1235


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8",
        "https://user:secret@example.com",
        "https://example.com?q=token",
        "file:///tmp/server",
    ],
)
def test_server_url_rejects_insecure_or_secret_bearing_values(url: str) -> None:
    with pytest.raises(ProtocolError):
        ServerConfiguration(url)


def test_private_network_http_and_https_are_allowed() -> None:
    assert ServerConfiguration("http://127.0.0.1:3000/").base_url == "http://127.0.0.1:3000"
    assert ServerConfiguration("http://192.168.1.20:2333/api/v3/").base_url == (
        "http://192.168.1.20:2333/api/v3"
    )
    assert ServerConfiguration("https://example.com/api/").base_url == "https://example.com/api"


def test_capability_negotiation_and_minimum_version() -> None:
    capabilities = parse_capabilities_response(json.loads(capabilities_payload()))
    assert negotiate_presence(capabilities, "1.7.9").supports_media_timeline
    with pytest.raises(NegotiationError):
        negotiate_presence(capabilities, "1.6.9")


def test_mutation_response_correlates_request_id() -> None:
    result = parse_mutation_response(json.loads(mutation_payload(REQUEST_ID, 9)), REQUEST_ID)
    assert result.accepted_sequence == 9
    with pytest.raises(ProtocolError):
        parse_mutation_response(
            json.loads(mutation_payload("123e4567-e89b-12d3-a456-426614174077", 9)),
            REQUEST_ID,
        )


def test_safe_integer_and_error_envelope_validation() -> None:
    snapshot = SanitizedPresenceSnapshot(now(), None, None)
    with pytest.raises(ProtocolError):
        make_presence_request(
            snapshot,
            DEVICE_ID,
            True,  # type: ignore[arg-type]
            configuration(),
            request_id=REQUEST_ID,
        )
    error = parse_api_error(
        {
            "error": {
                "code": "RATE_LIMITED",
                "retryable": True,
                "acceptedSequence": 8,
            }
        },
        429,
    )
    assert error.code == "RATE_LIMITED"
    assert error.retryable
    assert error.accepted_sequence == 8
    malformed = parse_api_error(
        {"error": {"code": "RATE_LIMITED", "retryable": "yes"}}, 429
    )
    assert malformed.code is None and not malformed.retryable


def test_capabilities_reject_type_coercion() -> None:
    payload = json.loads(capabilities_payload())
    payload["data"]["features"]["liveDesk"] = "true"
    with pytest.raises(ProtocolError):
        parse_capabilities_response(payload)
