from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

REQUEST_ID = "4d36e967-e325-11ce-bfc1-08002be10318"
DEVICE_ID = "123e4567-e89b-12d3-a456-426614174000"
EPOCH_ID = "123e4567-e89b-12d3-a456-426614174001"


def response_meta(request_id: str = REQUEST_ID) -> dict[str, Any]:
    return {
        "schema": "yohaku.companion.presence",
        "schemaVersion": 2,
        "requestId": request_id,
        "serverTime": wire_now(),
    }


def capabilities_payload() -> bytes:
    return json.dumps(
        {
            "meta": response_meta(),
            "data": {
                "minimumClientVersion": "1.7.0",
                "presenceSchemaVersions": [2],
                "momentSchemaVersions": [1],
                "features": {
                    "liveDesk": True,
                    "mediaTimeline": True,
                    "mediaArtwork": True,
                    "mediaPlaybackLinks": True,
                },
                "limits": {
                    "presencePayloadBytes": 32768,
                    "presenceRequestsPerMinute": 60,
                    "presenceLeaseMinSeconds": 30,
                    "presenceLeaseMaxSeconds": 120,
                    "recommendedHeartbeatSeconds": 60,
                    "maximumClockSkewSeconds": 60,
                },
            },
        },
        separators=(",", ":"),
    ).encode()


def mutation_payload(request_id: str, accepted: int) -> bytes:
    return json.dumps(
        {
            "meta": response_meta(request_id),
            "data": {
                "acceptedSequence": accepted,
                "receivedAt": wire_now(),
                "state": {
                    "schemaVersion": 2,
                    "epoch": EPOCH_ID,
                    "revision": accepted,
                    "projection": None,
                },
            },
        },
        separators=(",", ":"),
    ).encode()


def now() -> datetime:
    return datetime(2026, 7, 18, 1, 2, 3, 456789, tzinfo=UTC)


def wire_now() -> str:
    value = datetime.now(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S") + f".{value.microsecond // 1000:03d}Z"
