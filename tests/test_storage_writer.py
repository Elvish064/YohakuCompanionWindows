from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.helpers import DEVICE_ID, mutation_payload
from yohaku_companion_windows.domain import (
    ApplicationIconTemplateSettings,
    ApplicationRule,
    ConnectionMetadata,
    PresenceConfiguration,
    SanitizedPresenceSnapshot,
)
from yohaku_companion_windows.http_client import (
    CompanionHTTPClient,
    HTTPResponse,
    TransportFailure,
)
from yohaku_companion_windows.protocol import ServerConfiguration
from yohaku_companion_windows.storage import StateStore
from yohaku_companion_windows.writer import PresenceWriter


class RetryTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    async def request(
        self, method: str, url: str, headers: dict[str, str], content: bytes | None
    ) -> HTTPResponse:
        self.calls.append((method, url, headers, content))
        if len(self.calls) == 1:
            raise TransportFailure("ambiguous")
        assert content is not None
        request = json.loads(content)
        return HTTPResponse(
            200,
            mutation_payload(request["meta"]["requestId"], request["meta"]["sequence"]),
        )

    async def close(self) -> None:
        return None


class ConcurrentTransport:
    def __init__(self) -> None:
        self.sequences: list[int] = []

    async def request(
        self, method: str, url: str, headers: dict[str, str], content: bytes | None
    ) -> HTTPResponse:
        del method, url, headers
        assert content is not None
        request = json.loads(content)
        sequence = request["meta"]["sequence"]
        self.sequences.append(sequence)
        await asyncio.sleep(0.01)
        return HTTPResponse(
            200, mutation_payload(request["meta"]["requestId"], sequence)
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_exact_request_retry_and_token_only_in_bearer(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    transport = RetryTransport()
    client = CompanionHTTPClient(ServerConfiguration("https://example.com"), transport)
    metadata = ConnectionMetadata("https://example.com", DEVICE_ID, (), 12)
    writer = PresenceWriter(
        metadata,
        "top-secret-token",
        PresenceConfiguration(32768, 60, 30, 120, 60, 60, True),
        store,
        client,
    )
    await writer.replace(SanitizedPresenceSnapshot(datetime.now(UTC), None, None))
    assert len(transport.calls) == 2
    assert transport.calls[0][3] == transport.calls[1][3]
    assert transport.calls[0][2]["Authorization"] == "Bearer top-secret-token"
    assert "top-secret-token" not in transport.calls[0][1]
    assert b"top-secret-token" not in (transport.calls[0][3] or b"")
    assert store.next_sequence(DEVICE_ID) == 13
    store.close()


def test_sequence_is_persisted_before_send_and_never_reused(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    assert store.reserve_sequence(DEVICE_ID, 50) == 50
    assert store.next_sequence(DEVICE_ID) == 51
    store.close()
    reopened = StateStore(path)
    assert reopened.reserve_sequence(DEVICE_ID, 50) == 51
    reopened.reconcile_sequence(DEVICE_ID, 80)
    assert reopened.reserve_sequence(DEVICE_ID, 50) == 81
    reopened.close()


def test_extended_rule_and_icon_template_persist(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    rule = ApplicationRule(
        "win32:code.exe",
        "Code",
        icon_filename="vscode",
        activity_key="coding",
        activity_custom_label="开发中",
        media_artwork_url=(
            "https://cdn.example.com/cover.webp?v=" + "b" * 64
        ),
    )
    store.save_rule(rule)
    store.save_icon_template(
        ApplicationIconTemplateSettings(
            True,
            "https://cdn.example.com/icons/",
            ".webp",
        )
    )
    assert store.load_rules() == (rule.normalized(),)
    assert store.load_icon_template().enabled
    store.close()


@pytest.mark.asyncio
async def test_concurrent_mutations_are_serialized(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    transport = ConcurrentTransport()
    writer = PresenceWriter(
        ConnectionMetadata("https://example.com", DEVICE_ID, (), 20),
        "token",
        PresenceConfiguration(32768, 120, 30, 120, 60, 60, True),
        store,
        CompanionHTTPClient(ServerConfiguration("https://example.com"), transport),
    )
    snapshot = SanitizedPresenceSnapshot(datetime.now(UTC), None, None)
    await asyncio.gather(writer.replace(snapshot), writer.replace(snapshot))
    assert transport.sequences == [20, 21]
    store.close()
