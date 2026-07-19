from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import uuid4

from .domain import (
    ClearReason,
    ConnectionMetadata,
    PresenceConfiguration,
    SanitizedPresenceSnapshot,
)
from .http_client import CompanionHTTPClient, ResponseFailure, TransportFailure
from .protocol import (
    MutationResult,
    ProtocolError,
    encode_json,
    make_clear_request,
    make_presence_request,
    parse_wire_timestamp,
)
from .storage import StateStore


class PayloadTooLarge(RuntimeError):
    pass


class PresenceWriter:
    """Serializes mutations and retries the exact request once when ambiguous."""

    def __init__(
        self,
        metadata: ConnectionMetadata,
        token: str,
        configuration: PresenceConfiguration,
        store: StateStore,
        client: CompanionHTTPClient,
    ) -> None:
        self._metadata = metadata
        self._token = token
        self._configuration = configuration
        self._store = store
        self._client = client
        self._send_lock = asyncio.Lock()

    async def replace(self, snapshot: SanitizedPresenceSnapshot) -> MutationResult:
        async with self._send_lock:
            sequence = self._store.reserve_sequence(
                self._metadata.device_id, self._metadata.pairing_next_sequence
            )
            request_id = str(uuid4())
            request = make_presence_request(
                snapshot,
                self._metadata.device_id,
                sequence,
                self._configuration,
                request_id=request_id,
            )
            return await self._send_exact(
                "PUT", "/companion/presence", encode_json(request), request_id
            )

    async def clear(self, reason: ClearReason, observed_at: datetime) -> MutationResult:
        async with self._send_lock:
            sequence = self._store.reserve_sequence(
                self._metadata.device_id, self._metadata.pairing_next_sequence
            )
            request_id = str(uuid4())
            request = make_clear_request(
                reason, observed_at, self._metadata.device_id, sequence, request_id
            )
            return await self._send_exact(
                "POST", "/companion/presence/clear", encode_json(request), request_id
            )

    async def _send_exact(
        self, method: str, path: str, body: bytes, request_id: str
    ) -> MutationResult:
        if len(body) > self._configuration.maximum_payload_bytes:
            raise PayloadTooLarge("Presence 请求超过服务器限制")

        async def operation() -> MutationResult:
            return await self._client.mutate(method, path, body, self._token, request_id)

        try:
            result = await operation()
        except ResponseFailure as error:
            self._reconcile_error(error)
            if not (500 <= error.error.status_code <= 599 and error.error.retryable):
                raise
            result = await self._retry(operation)
        except (TransportFailure, ProtocolError):
            result = await self._retry(operation)
        self._store.reconcile_sequence(self._metadata.device_id, result.accepted_sequence)
        server_time = parse_wire_timestamp(
            result.response.get("meta", {}).get("serverTime"), "meta.serverTime"
        )
        skew = abs((datetime.now(server_time.tzinfo) - server_time).total_seconds())
        if skew > self._configuration.maximum_clock_skew_seconds:
            raise ProtocolError("server clock skew exceeds negotiated limit")
        return result

    async def _retry(self, operation: Callable[[], Awaitable[MutationResult]]) -> MutationResult:
        try:
            return await operation()
        except ResponseFailure as error:
            self._reconcile_error(error)
            raise

    def _reconcile_error(self, error: ResponseFailure) -> None:
        if error.error.accepted_sequence is not None:
            self._store.reconcile_sequence(
                self._metadata.device_id, error.error.accepted_sequence
            )
