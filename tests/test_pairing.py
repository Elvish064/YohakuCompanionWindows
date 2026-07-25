from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from tests.helpers import DEVICE_ID, capabilities_payload
from yohaku_companion_windows.http_client import HTTPResponse
from yohaku_companion_windows.pairing import PairingInstaller
from yohaku_companion_windows.protocol import ProtocolError
from yohaku_companion_windows.storage import StateStore


class CredentialStub:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.token: str | None = None

    async def ensure_available(self) -> None:
        self.events.append("credential")

    async def get_token(self) -> str | None:
        return self.token

    async def set_token(self, value: str) -> None:
        self.token = value

    async def delete_token(self) -> None:
        self.token = None


class PairingTransport:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.requests: list[tuple[str, str, dict[str, str], bytes | None]] = []

    async def request(
        self, method: str, url: str, headers: dict[str, str], content: bytes | None
    ) -> HTTPResponse:
        self.requests.append((method, url, headers, content))
        if url.endswith("/companion/capabilities"):
            self.events.append("capabilities")
            return HTTPResponse(200, capabilities_payload())
        self.events.append("claim")
        return HTTPResponse(
            200,
            json.dumps(
                {
                    "data": {
                        "deviceId": DEVICE_ID,
                        "deviceToken": "device-secret",
                        "scopes": ["companion:presence:write"],
                        "nextSequence": 7,
                    }
                }
            ).encode(),
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_pairing_preflight_order_code_location_and_default_disabled(tmp_path: Path) -> None:
    events: list[str] = []
    store = StateStore(tmp_path / "state.sqlite3")
    credential = CredentialStub(events)
    transport = PairingTransport(events)
    result = await PairingInstaller(store, credential, transport).pair(
        "https://example.com", "one-time-code", "Windows"
    )
    assert events == ["credential", "capabilities", "claim"]
    claim = transport.requests[-1]
    assert "one-time-code" not in claim[1]
    assert all("one-time-code" not in value for value in claim[2].values())
    assert all(
        request[2]["User-Agent"] == "YohakuCompanion/1.7.10 (Windows)"
        for request in transport.requests
    )
    assert json.loads(claim[3] or b"{}")["pairingCode"] == "one-time-code"
    assert result.metadata.live_desk_enabled is False
    assert credential.token == "device-secret"
    assert b"device-secret" not in (tmp_path / "state.sqlite3").read_bytes()
    store.close()


@pytest.mark.asyncio
async def test_pairing_allows_private_lan_http(tmp_path: Path) -> None:
    events: list[str] = []
    store = StateStore(tmp_path / "state.sqlite3")
    transport = PairingTransport(events)
    await PairingInstaller(store, CredentialStub(events), transport).pair(
        "http://192.168.1.20:2333/api/v3", "one-time-code", "Windows"
    )
    assert transport.requests[0][1] == (
        "http://192.168.1.20:2333/api/v3/companion/capabilities"
    )
    assert transport.requests[1][1] == (
        "http://192.168.1.20:2333/api/v3/companion/pairings/claim"
    )
    store.close()


@pytest.mark.asyncio
async def test_http_dns_name_must_resolve_only_to_lan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resolve_lan(*_: object, **__: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 2333))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve_lan)
    events: list[str] = []
    store = StateStore(tmp_path / "lan.sqlite3")
    transport = PairingTransport(events)
    await PairingInstaller(store, CredentialStub(events), transport).pair(
        "http://core.home.example:2333/api/v3", "one-time-code", "Windows"
    )
    assert len(transport.requests) == 2
    store.close()

    def resolve_public(*_: object, **__: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve_public)
    events = []
    store = StateStore(tmp_path / "public.sqlite3")
    transport = PairingTransport(events)
    with pytest.raises(ProtocolError, match="私有或链路本地"):
        await PairingInstaller(store, CredentialStub(events), transport).pair(
            "http://public.example/api/v3", "one-time-code", "Windows"
        )
    assert transport.requests == []
    store.close()
