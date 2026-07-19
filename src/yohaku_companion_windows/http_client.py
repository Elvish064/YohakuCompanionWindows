from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .identity import APP_VERSION
from .protocol import (
    CLIENT_VERSION_HEADER,
    APIError,
    Capabilities,
    MutationResult,
    PairingClaim,
    ProtocolError,
    ServerConfiguration,
    decode_json,
    encode_json,
    lan_address_literal,
    parse_api_error,
    parse_capabilities_response,
    parse_mutation_response,
    parse_pairing_claim,
)


class TransportFailure(RuntimeError):
    pass


class ResponseFailure(RuntimeError):
    def __init__(self, error: APIError) -> None:
        super().__init__(f"server response failed: {error.status_code}")
        self.error = error


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status_code: int
    content: bytes


class HTTPTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        content: bytes | None,
    ) -> HTTPResponse: ...

    async def close(self) -> None: ...


class HTTPXTransport:
    def __init__(self, *, trust_env: bool = True) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            trust_env=trust_env,
        )

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        content: bytes | None,
    ) -> HTTPResponse:
        try:
            response = await self._client.request(
                method,
                url,
                headers=headers,
                content=content,
            )
        except httpx.HTTPError as error:
            raise TransportFailure("network transport failed") from error
        return HTTPResponse(response.status_code, response.content)

    async def close(self) -> None:
        await self._client.aclose()


class CompanionHTTPClient:
    def __init__(
        self,
        server: ServerConfiguration,
        transport: HTTPTransport | None = None,
        client_version: str = APP_VERSION,
        maximum_response_bytes: int = 64 * 1024,
    ) -> None:
        self.server = server
        self.transport = transport or HTTPXTransport(
            # LAN HTTP contains plaintext credentials and must never be sent
            # through an environment-configured proxy.
            trust_env=not server.base_url.casefold().startswith("http://")
        )
        self.client_version = client_version
        self.maximum_response_bytes = maximum_response_bytes

    async def close(self) -> None:
        await self.transport.close()

    async def fetch_capabilities(self) -> Capabilities:
        response = await self._request("GET", "/companion/capabilities", None, None)
        if not 200 <= response.status_code < 300:
            raise ResponseFailure(_error_from_response(response, None))
        return parse_capabilities_response(_decode_response(response))

    async def claim_pairing(self, pairing_code: str, device_name: str) -> PairingClaim:
        body = encode_json({"pairingCode": pairing_code, "deviceName": device_name})
        response = await self._request("POST", "/companion/pairings/claim", None, body)
        if not 200 <= response.status_code < 300:
            raise ResponseFailure(_error_from_response(response, None))
        return parse_pairing_claim(_decode_response(response), self.server.base_url)

    async def mutate(
        self,
        method: str,
        path: str,
        body: bytes,
        token: str,
        request_id: str,
    ) -> MutationResult:
        headers = {
            "Authorization": f"Bearer {token}",
            CLIENT_VERSION_HEADER: self.client_version,
        }
        response = await self._request(method, path, headers, body)
        if not 200 <= response.status_code < 300:
            raise ResponseFailure(_error_from_response(response, request_id))
        return parse_mutation_response(_decode_response(response), request_id)

    async def _request(
        self,
        method: str,
        path: str,
        extra_headers: dict[str, str] | None,
        body: bytes | None,
    ) -> HTTPResponse:
        await _verify_transport_address(self.server.base_url)
        headers = {
            "Accept": "application/json",
            # Core blocks generic crawler UAs such as python-httpx.  Identify
            # the actual first-party client on public and authenticated calls.
            "User-Agent": f"YohakuCompanion/{self.client_version} (Windows)",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        response = await self.transport.request(
            method,
            self.server.endpoint(path),
            headers,
            body,
        )
        if len(response.content) > self.maximum_response_bytes:
            raise ProtocolError("response too large")
        if not response.content:
            raise ProtocolError("empty server response")
        return response


async def _verify_transport_address(base_url: str) -> None:
    """Prevent plaintext credentials from leaving the local network."""
    parsed = urlsplit(base_url)
    if parsed.scheme.casefold() != "http":
        return
    host = parsed.hostname
    if host is None:
        raise ProtocolError("invalid server URL")
    literal_is_lan = lan_address_literal(host)
    if literal_is_lan is True:
        return
    if literal_is_lan is False:
        raise ProtocolError("HTTP 仅允许局域网地址")

    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            parsed.port or 80,
            0,
            socket.SOCK_STREAM,
        )
    except OSError as error:
        raise TransportFailure("无法解析局域网服务器地址") from error
    addresses = {str(record[4][0]).split("%", 1)[0] for record in records}
    if not addresses or any(lan_address_literal(address) is not True for address in addresses):
        raise ProtocolError("HTTP 主机必须只解析到私有或链路本地 IP")


def _decode_response(response: HTTPResponse) -> dict[str, Any]:
    return decode_json(response.content)


def _error_from_response(response: HTTPResponse, request_id: str | None) -> APIError:
    try:
        payload = decode_json(response.content)
    except ProtocolError:
        return APIError(
            response.status_code,
            None,
            500 <= response.status_code <= 599,
            None,
        )
    error = parse_api_error(payload, response.status_code, request_id)
    if error.code is None and 500 <= response.status_code <= 599:
        return APIError(response.status_code, None, True, error.accepted_sequence)
    return error
