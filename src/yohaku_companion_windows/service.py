from __future__ import annotations

import asyncio
from collections.abc import Callable

from .capture import PresenceCapture
from .coordinator import LiveDeskCoordinator
from .credentials import CredentialError, CredentialStore
from .domain import (
    ApplicationRule,
    ClearReason,
    PrivacyDefaults,
    RuleCandidate,
    RuntimeState,
    SanitizedPresenceSnapshot,
    SensitiveTextRule,
    ServiceViewState,
    SourceSettings,
)
from .media_capture import MediaProvider
from .pairing import PairingInstaller
from .privacy import PreviewConsentGate
from .storage import StateStore


class ServiceError(RuntimeError):
    pass


class ApplicationService:
    """One process-wide owner of credentials, consent, capture and Live Desk."""

    def __init__(
        self,
        store: StateStore,
        credentials: CredentialStore,
        capture: PresenceCapture,
        media: MediaProvider,
    ) -> None:
        self.store = store
        self.credentials = credentials
        self.capture = capture
        self.media = media
        self.consent = PreviewConsentGate()
        self.mutation_lock = asyncio.Lock()
        self.state = ServiceViewState(
            connection=store.load_connection(),
            paused=store.is_paused(),
        )
        self._listeners: list[Callable[[ServiceViewState], None]] = []
        self._lifecycle_available = False
        self._session_suspended = False
        capture.set_candidates_callback(self._candidates_changed)
        self.coordinator = LiveDeskCoordinator(
            store,
            credentials,
            capture,
            self._runtime_changed,
            self._published_snapshot,
        )

    def subscribe(self, listener: Callable[[ServiceViewState], None]) -> None:
        self._listeners.append(listener)
        listener(self.state)

    def set_lifecycle_available(self, available: bool) -> None:
        self._lifecycle_available = available
        if not available and self.state.connection and self.state.connection.live_desk_enabled:
            self.state.notice = "无法监听 Windows 锁屏事件，Live Desk 已保持关闭"
        self._notify()

    async def initialize(self) -> None:
        await self.media.start(self.coordinator.request_refresh)
        credential_error = False
        try:
            token = await self.credentials.get_token()
        except CredentialError:
            credential_error = True
            token = None
        metadata = self.store.load_connection()
        if metadata is not None and token is None:
            # Non-secret metadata is not a credential. Never publish partially paired state.
            metadata = self.store.set_live_desk_enabled(False)
            self.state.notice = (
                "Windows 凭据后端不可用，Live Desk 已保持关闭"
                if credential_error
                else "Windows 凭据中缺少设备令牌，请重新配对"
            )
        elif credential_error:
            self.state.notice = "Windows 凭据后端不可用，无法进行配对"
        self.state.connection = metadata
        self.state.paused = self.store.is_paused()
        if (
            metadata is not None
            and metadata.live_desk_enabled
            and not self.state.paused
            and self._lifecycle_available
            and not self._session_suspended
        ):
            await self.coordinator.start()
        self._notify()

    async def refresh_preview(self) -> None:
        async with self.mutation_lock:
            snapshot = await self.capture.capture()
            self.state.preview = snapshot
            self.consent.record(snapshot)
            self.state.preview_current = True
            self.state.rule_candidates = self.capture.candidates
            self.state.notice = (
                "敏感词规则匹配超时，相关字段已按安全策略隐藏"
                if self.capture.last_filter_timed_out
                else None
            )
            self._notify()

    async def pair(
        self, base_url: str, pairing_code: str, device_name: str
    ) -> None:
        async with self.mutation_lock:
            self._set_busy(True)
            try:
                if self.store.load_connection() is not None:
                    self.state.connection = self.store.set_live_desk_enabled(False)
                await self.coordinator.stop(
                    ClearReason.CONNECTION_REMOVED, RuntimeState.DISABLED
                )
                result = await PairingInstaller(
                    self.store, self.credentials
                ).pair(base_url, pairing_code, device_name)
                self.consent.clear()
                self.state.connection = result.metadata
                self.state.preview = None
                self.state.preview_current = False
                self.state.paused = False
                self.state.notice = "配对成功。请检查净化预览后再开启 Live Desk。"
            finally:
                self._set_busy(False)

    async def enable_live_desk(self) -> None:
        async with self.mutation_lock:
            metadata = self.store.load_connection()
            if metadata is None or await self.credentials.get_token() is None:
                raise ServiceError("设备未完成配对")
            if not self._lifecycle_available:
                raise ServiceError("无法监听锁屏事件，不能安全开启 Live Desk")
            if self._session_suspended:
                raise ServiceError("锁屏或休眠期间不能开启 Live Desk")
            candidate = self.consent.confirmation
            snapshot = await self.capture.capture()
            if self._session_suspended:
                raise ServiceError("锁屏或休眠期间不能开启 Live Desk")
            if not self.consent.validates(candidate, snapshot):
                self.consent.clear()
                self.state.preview_current = False
                self._notify()
                raise ServiceError("预览已变化，请重新检查净化预览")
            self.state.connection = self.store.set_live_desk_enabled(True)
            self.store.set_paused(False)
            self.state.paused = False
            await self.coordinator.start()
            self._notify()

    async def disable_live_desk(self) -> None:
        async with self.mutation_lock:
            self.state.connection = self.store.set_live_desk_enabled(False)
            await self.coordinator.stop(ClearReason.PAUSED, RuntimeState.DISABLED)
            self._notify()

    async def pause(self) -> None:
        async with self.mutation_lock:
            self.store.set_paused(True)
            self.state.paused = True
            await self.coordinator.stop(ClearReason.PAUSED, RuntimeState.SUSPENDED)
            self._notify()

    async def resume(self) -> None:
        async with self.mutation_lock:
            metadata = self.store.load_connection()
            if metadata and metadata.live_desk_enabled:
                if not self._lifecycle_available:
                    raise ServiceError("无法监听锁屏事件，不能恢复 Live Desk")
                if self._session_suspended:
                    raise ServiceError("锁屏或休眠期间不能恢复 Live Desk")
                if await self.credentials.get_token() is None:
                    raise ServiceError("Windows 凭据中缺少设备令牌")
            self.store.set_paused(False)
            self.state.paused = False
            if metadata and metadata.live_desk_enabled:
                await self.coordinator.start()
            self._notify()

    async def remove_device(self) -> None:
        async with self.mutation_lock:
            self.state.connection = self.store.set_live_desk_enabled(False)
            await self.coordinator.stop(
                ClearReason.CONNECTION_REMOVED, RuntimeState.DISABLED
            )
            await self.credentials.delete_token()
            self.store.remove_connection()
            self.store.set_paused(False)
            self.consent.clear()
            self.state = ServiceViewState(notice="设备连接已移除")
            self._notify()

    async def save_privacy(
        self,
        sources: SourceSettings,
        defaults: PrivacyDefaults,
        rules: tuple[ApplicationRule, ...],
        sensitive_rules: tuple[SensitiveTextRule, ...] = (),
    ) -> None:
        async with self.mutation_lock:
            was_enabled = bool(
                self.state.connection and self.state.connection.live_desk_enabled
            )
            if was_enabled:
                self.state.connection = self.store.set_live_desk_enabled(False)
                await self.coordinator.stop(
                    ClearReason.PRIVACY_CHANGED, RuntimeState.DISABLED
                )
            self.store.save_privacy_configuration(
                sources,
                defaults,
                rules,
                sensitive_rules,
            )
            self.consent.policy_changed()
            self.state.preview = None
            self.state.preview_current = False
            self.state.notice = "隐私策略已更新，请重新检查净化预览"
            self._notify()

    async def handle_suspend(self) -> None:
        self._session_suspended = True
        if (
            self.state.connection
            and self.state.connection.live_desk_enabled
            and not self.state.paused
        ):
            await self.coordinator.suspend()

    async def handle_resume(self) -> None:
        self._session_suspended = False
        if self.state.connection and self.state.connection.live_desk_enabled:
            await self.coordinator.resume(should_start=not self.state.paused)

    async def shutdown(self) -> None:
        async with self.mutation_lock:
            await self.coordinator.shutdown()
            await self.media.stop()
            self.store.close()

    def _runtime_changed(self, state: RuntimeState) -> None:
        self.state.runtime_state = state
        self._notify()

    def _published_snapshot(self, snapshot: SanitizedPresenceSnapshot) -> None:
        self.state.preview = snapshot
        if self.capture.last_filter_timed_out:
            self.state.notice = "敏感词规则匹配超时，相关字段已按安全策略隐藏"
        self._notify()

    def _candidates_changed(self, candidates: tuple[RuleCandidate, ...]) -> None:
        self.state.rule_candidates = candidates
        self._notify()

    def _set_busy(self, value: bool) -> None:
        self.state.busy = value
        self._notify()

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener(self.state)
