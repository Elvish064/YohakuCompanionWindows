from __future__ import annotations

import asyncio
from typing import Any, Protocol
from uuid import uuid4


class CredentialError(RuntimeError):
    pass


class CredentialStore(Protocol):
    async def ensure_available(self) -> None: ...

    async def get_token(self) -> str | None: ...

    async def set_token(self, value: str) -> None: ...

    async def delete_token(self) -> None: ...


class VRChatCredentialStore(Protocol):
    async def get_api_key(self) -> str | None: ...

    async def set_api_key(self, value: str) -> None: ...

    async def delete_api_key(self) -> None: ...


class WindowsCredentialStore:
    _ACCOUNT = "yohaku.companion.device-token.v1"
    _PROBE_ACCOUNT = "yohaku.companion.credential-probe.v1"

    def __init__(self, service_name: str) -> None:
        self._service_name = service_name

    async def ensure_available(self) -> None:
        backend = await self._backend()

        def probe() -> None:
            marker = f"probe-{uuid4()}"
            previous = backend.get_password(self._service_name, self._PROBE_ACCOUNT)
            try:
                backend.set_password(self._service_name, self._PROBE_ACCOUNT, marker)
                if backend.get_password(self._service_name, self._PROBE_ACCOUNT) != marker:
                    raise CredentialError("Windows 凭据写入验证失败")
            finally:
                if previous is None:
                    backend.delete_password(self._service_name, self._PROBE_ACCOUNT)
                else:
                    backend.set_password(
                        self._service_name, self._PROBE_ACCOUNT, previous
                    )

        try:
            await asyncio.to_thread(probe)
        except CredentialError:
            raise
        except Exception as error:
            raise CredentialError("Windows 凭据保险箱不可写") from error

    async def get_token(self) -> str | None:
        backend = await self._backend()
        try:
            value = await asyncio.to_thread(
                backend.get_password,
                self._service_name,
                self._ACCOUNT,
            )
        except Exception as error:
            raise CredentialError("Windows 凭据保险箱不可用") from error
        normalized = None if value is None else value.strip()
        return normalized or None

    async def set_token(self, value: str) -> None:
        normalized = value.strip()
        if not normalized:
            raise CredentialError("设备令牌为空")
        backend = await self._backend()
        try:
            await asyncio.to_thread(
                backend.set_password,
                self._service_name,
                self._ACCOUNT,
                normalized,
            )
        except Exception as error:
            raise CredentialError("无法保存设备令牌") from error

    async def delete_token(self) -> None:
        backend = await self._backend()
        existing = await self.get_token()
        if existing is None:
            return
        try:
            await asyncio.to_thread(
                backend.delete_password,
                self._service_name,
                self._ACCOUNT,
            )
        except Exception as error:
            raise CredentialError("无法删除设备令牌") from error

    async def _backend(self) -> Any:
        def create_backend() -> Any:
            try:
                from keyring.backends.Windows import WinVaultKeyring
            except Exception as error:
                raise CredentialError("Windows 凭据后端未安装") from error
            backend = WinVaultKeyring()
            if float(backend.priority) <= 0:
                raise CredentialError("Windows 凭据后端不可用")
            return backend

        return await asyncio.to_thread(create_backend)


class WindowsVRChatCredentialStore:
    """Separate Windows Credential Locker account for the optional VRC API."""

    _ACCOUNT = "yohaku.companion.vrchat-api-key.v1"

    def __init__(self, service_name: str) -> None:
        self._service_name = service_name

    async def get_api_key(self) -> str | None:
        backend = await _windows_backend()
        try:
            value = await asyncio.to_thread(
                backend.get_password, self._service_name, self._ACCOUNT
            )
        except Exception as error:
            raise CredentialError("Windows 凭据保险箱不可用") from error
        normalized = None if value is None else value.strip()
        return normalized or None

    async def set_api_key(self, value: str) -> None:
        normalized = value.strip()
        if not normalized:
            raise CredentialError("VRC API 密匙为空")
        backend = await _windows_backend()
        try:
            await asyncio.to_thread(
                backend.set_password, self._service_name, self._ACCOUNT, normalized
            )
        except Exception as error:
            raise CredentialError("无法保存 VRC API 密匙") from error

    async def delete_api_key(self) -> None:
        backend = await _windows_backend()
        if await self.get_api_key() is None:
            return
        try:
            await asyncio.to_thread(
                backend.delete_password, self._service_name, self._ACCOUNT
            )
        except Exception as error:
            raise CredentialError("无法删除 VRC API 密匙") from error


async def _windows_backend() -> Any:
    def create_backend() -> Any:
        try:
            from keyring.backends.Windows import WinVaultKeyring
        except Exception as error:
            raise CredentialError("Windows 凭据后端未安装") from error
        backend = WinVaultKeyring()
        if float(backend.priority) <= 0:
            raise CredentialError("Windows 凭据后端不可用")
        return backend

    return await asyncio.to_thread(create_backend)
