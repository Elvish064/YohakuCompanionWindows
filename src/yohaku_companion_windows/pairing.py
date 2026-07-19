from __future__ import annotations

from dataclasses import dataclass

from .credentials import CredentialStore
from .domain import PRESENCE_SCOPE, ConnectionMetadata, normalize_text
from .http_client import CompanionHTTPClient, HTTPTransport, ResponseFailure
from .identity import APP_VERSION
from .protocol import (
    ServerConfiguration,
    metadata_from_claim,
    negotiate_presence,
    validate_clock_skew,
)
from .storage import StateStore


class PairingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PairingResult:
    metadata: ConnectionMetadata


class PairingInstaller:
    """Consumes a one-time code only after all non-destructive preflights pass."""

    def __init__(
        self,
        store: StateStore,
        credentials: CredentialStore,
        transport: HTTPTransport | None = None,
    ) -> None:
        self._store = store
        self._credentials = credentials
        self._transport = transport

    async def pair(self, base_url: str, pairing_code: str, device_name: str) -> PairingResult:
        code = pairing_code.strip()
        name = normalize_text(device_name)
        if not 1 <= len(code) <= 32:
            raise PairingError("配对码无效")
        if name is None or len(name) > 120:
            raise PairingError("设备名称无效")

        # Order is security-sensitive: protected storage, URL, capabilities,
        # then and only then the one-time code.
        await self._credentials.ensure_available()
        server = ServerConfiguration(base_url)
        client = CompanionHTTPClient(server, transport=self._transport)
        try:
            try:
                capabilities = await client.fetch_capabilities()
            except ResponseFailure as error:
                raise PairingError(_response_message("能力检查", error)) from error
            configuration = negotiate_presence(capabilities, APP_VERSION)
            validate_clock_skew(
                capabilities.server_time, configuration.maximum_clock_skew_seconds
            )
            try:
                claim = await client.claim_pairing(code, name)
            except ResponseFailure as error:
                raise PairingError(_response_message("配对领取", error)) from error
        finally:
            if self._transport is None:
                await client.close()
        if PRESENCE_SCOPE not in claim.scopes:
            raise PairingError("服务器没有授予 Presence 写入权限")

        previous_metadata = self._store.load_connection()
        previous_token = await self._credentials.get_token()
        metadata = metadata_from_claim(claim)
        try:
            await self._credentials.set_token(claim.device_token)
            self._store.install_connection(
                metadata,
                None if previous_metadata is None else previous_metadata.device_id,
            )
        except Exception:
            if previous_token is None:
                await self._credentials.delete_token()
            else:
                await self._credentials.set_token(previous_token)
            if previous_metadata is None:
                self._store.remove_connection()
            else:
                self._store.save_connection(previous_metadata)
            raise
        return PairingResult(metadata)


def _response_message(stage: str, failure: ResponseFailure) -> str:
    error = failure.error
    suffix = f"，错误代码 {error.code}" if error.code else ""
    if error.status_code == 403 and stage == "能力检查":
        return (
            "能力检查返回 HTTP 403"
            f"{suffix}。请确认反向代理允许公开访问 /companion/capabilities"
        )
    if error.status_code == 403:
        return (
            f"配对领取返回 HTTP 403{suffix}。请确认反向代理将公开的 "
            "/companion/pairings/claim 路由转发到了 Core"
        )
    return f"{stage}返回 HTTP {error.status_code}{suffix}"
