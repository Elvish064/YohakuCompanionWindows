from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .capture import PresenceCapture
from .coordinator import LiveDeskCoordinator
from .credentials import CredentialError, CredentialStore, VRChatCredentialStore
from .custom_broadcast import CustomBroadcastController
from .domain import (
    ApplicationIconTemplateSettings,
    ApplicationRule,
    ClearReason,
    CustomBroadcastSpec,
    LoggingSettings,
    PrivacyDefaults,
    RuleCandidate,
    RuntimeState,
    SanitizedPresenceSnapshot,
    SensitiveTextRule,
    ServiceViewState,
    SourceSettings,
    VRChatIntegrationSettings,
)
from .http_client import verify_transport_address
from .logging_service import ProcessLogService
from .media_capture import MediaProvider
from .pairing import PairingInstaller
from .privacy import PreviewConsentGate, PrivacyEvaluator
from .storage import StateStore
from .vrchat import VRChatIntegration, validate_vrc_endpoint

runtime_log = logging.getLogger("yohaku.运行状态")
lifecycle_log = logging.getLogger("yohaku.生命周期")
media_log = logging.getLogger("yohaku.媒体")


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
        vrchat_credentials: VRChatCredentialStore | None = None,
        vrchat: VRChatIntegration | None = None,
        logs: ProcessLogService | None = None,
    ) -> None:
        self.store = store
        self.credentials = credentials
        self.capture = capture
        self.media = media
        self.vrchat_credentials = vrchat_credentials
        self.vrchat = vrchat
        self.logs = logs
        self.consent = PreviewConsentGate()
        self.custom_broadcast = CustomBroadcastController(store, credentials)
        self.mutation_lock = asyncio.Lock()
        self.state = ServiceViewState(
            connection=store.load_connection(),
            paused=store.is_paused(),
            vrchat_settings=store.load_vrchat_settings(),
        )
        self._listeners: list[Callable[[ServiceViewState], None]] = []
        self._lifecycle_available = False
        self._session_suspended = False
        self._shutting_down = False
        self._vrchat_task: asyncio.Task[None] | None = None
        self._vrchat_lock = asyncio.Lock()
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
        runtime_log.info("客户端初始化，版本状态已加载")
        media_available = await self.media.start(self.coordinator.request_refresh)
        media_log.info(
            "系统媒体能力%s",
            "可用" if media_available else "不可用，已降级为应用 Presence",
        )
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
        self.state.vrchat_settings = self.store.load_vrchat_settings()
        if self.vrchat_credentials is not None:
            try:
                self.state.vrchat_api_key_present = (
                    await self.vrchat_credentials.get_api_key() is not None
                )
            except CredentialError:
                self.state.vrchat_api_key_present = False
                self.state.notice = "Windows 凭据后端不可用，无法读取 VRC API 密匙状态"
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
            await self.custom_broadcast.stop(ClearReason.CONNECTION_REMOVED)
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
                runtime_log.info("设备配对成功；Live Desk 保持关闭")
            finally:
                self._set_busy(False)

    async def enable_live_desk(self) -> None:
        async with self.mutation_lock:
            if self.custom_broadcast.running:
                raise ServiceError("测试广播运行期间不能开启 Live Desk")
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
            await self.custom_broadcast.stop()
            await self._stop_vrchat()
            self.state.connection = self.store.set_live_desk_enabled(False)
            await self.coordinator.stop(ClearReason.PAUSED, RuntimeState.DISABLED)
            self._notify()

    async def pause(self) -> None:
        async with self.mutation_lock:
            await self.custom_broadcast.stop()
            await self._stop_vrchat()
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
            await self.custom_broadcast.stop(ClearReason.CONNECTION_REMOVED)
            await self._stop_vrchat()
            self.state.connection = self.store.set_live_desk_enabled(False)
            await self.coordinator.stop(
                ClearReason.CONNECTION_REMOVED, RuntimeState.DISABLED
            )
            await self.credentials.delete_token()
            if self.vrchat_credentials is not None:
                await self.vrchat_credentials.delete_api_key()
            disabled_vrchat = VRChatIntegrationSettings()
            self.store.save_vrchat_settings(disabled_vrchat)
            self.store.remove_connection()
            self.store.set_paused(False)
            self.consent.clear()
            self.state = ServiceViewState(
                notice="设备连接已移除",
                vrchat_settings=disabled_vrchat,
            )
            runtime_log.info("设备连接及关联凭据已移除")
            self._notify()

    async def save_privacy(
        self,
        sources: SourceSettings,
        defaults: PrivacyDefaults,
        rules: tuple[ApplicationRule, ...],
        sensitive_rules: tuple[SensitiveTextRule, ...] = (),
        icon_template: ApplicationIconTemplateSettings | None = None,
    ) -> None:
        async with self.mutation_lock:
            await self.custom_broadcast.stop(ClearReason.PRIVACY_CHANGED)
            normalized_icon_template = (
                icon_template or self.store.load_icon_template()
            ).normalized()
            was_enabled = bool(
                self.state.connection and self.state.connection.live_desk_enabled
            )
            await self._stop_vrchat()
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
                normalized_icon_template,
            )
            self.consent.policy_changed()
            self.state.preview = None
            self.state.preview_current = False
            self.state.notice = "隐私策略已更新，请重新检查净化预览"
            self._notify()

    async def handle_suspend(self) -> None:
        self._session_suspended = True
        await self.custom_broadcast.stop(ClearReason.SLEEP)
        await self._stop_vrchat()
        lifecycle_log.info("会话锁定或系统休眠，集成已停止")
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
            self._shutting_down = True
            await self.custom_broadcast.stop(ClearReason.SHUTDOWN)
            await self._stop_vrchat()
            await self.coordinator.shutdown()
            await self.media.stop()
            runtime_log.info("客户端有序退出")
            self.store.close()
            if self.logs is not None:
                self.logs.uninstall()

    def _runtime_changed(self, state: RuntimeState) -> None:
        self.state.runtime_state = state
        runtime_log.info("Live Desk 状态：%s", state.value)
        self._schedule_vrchat_reconcile()
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

    async def save_vrchat_settings(
        self,
        settings: VRChatIntegrationSettings,
        api_key: str = "",
    ) -> None:
        async with self.mutation_lock:
            await self.custom_broadcast.stop(ClearReason.PRIVACY_CHANGED)
            normalized = VRChatIntegrationSettings.from_dict(settings.to_dict())
            new_key = api_key.strip()
            existing_key = (
                await self.vrchat_credentials.get_api_key()
                if self.vrchat_credentials is not None
                else None
            )
            if normalized.enabled and normalized.upload_activity:
                validate_vrc_endpoint(normalized.endpoint_url)
                await verify_transport_address(normalized.endpoint_url)
                if not new_key and not existing_key:
                    raise ServiceError("开启 VRC 状态上传前必须填写 API 密匙")
            was_enabled = bool(
                self.state.connection and self.state.connection.live_desk_enabled
            )
            await self._stop_vrchat()
            if was_enabled:
                self.state.connection = self.store.set_live_desk_enabled(False)
                await self.coordinator.stop(
                    ClearReason.PRIVACY_CHANGED, RuntimeState.DISABLED
                )
            if new_key:
                if self.vrchat_credentials is None:
                    raise ServiceError("VRC API 凭据存储不可用")
                await self.vrchat_credentials.set_api_key(new_key)
                existing_key = new_key
            self.store.save_vrchat_settings(normalized)
            self.state.vrchat_settings = normalized
            self.state.vrchat_api_key_present = existing_key is not None
            self.state.vrchat_status = "等待 Live Desk 公开" if normalized.enabled else "未启用"
            self.consent.policy_changed()
            self.state.preview = None
            self.state.preview_current = False
            self.state.notice = "VRChat 设置已更新，请重新检查净化预览"
            runtime_log.info("VRChat 集成设置已保存；敏感内容未写入日志")
            self._notify()

    async def clear_vrchat_api_key(self) -> None:
        async with self.mutation_lock:
            await self.custom_broadcast.stop(ClearReason.PRIVACY_CHANGED)
            await self._stop_vrchat()
            if self.state.connection and self.state.connection.live_desk_enabled:
                self.state.connection = self.store.set_live_desk_enabled(False)
                await self.coordinator.stop(
                    ClearReason.PRIVACY_CHANGED, RuntimeState.DISABLED
                )
            if self.vrchat_credentials is not None:
                await self.vrchat_credentials.delete_api_key()
            self.state.vrchat_api_key_present = False
            self.state.vrchat_status = "缺少 API 密匙"
            self.consent.policy_changed()
            self.state.preview = None
            self.state.preview_current = False
            self.state.notice = "VRC API 密匙已清除，请重新检查净化预览"
            self._notify()

    async def save_logging_settings(self, settings: LoggingSettings) -> None:
        async with self.mutation_lock:
            self.store.save_logging_settings(settings)
            if self.logs is not None:
                self.logs.set_master_enabled(settings.master_enabled)
                self.logs.set_file_enabled(
                    settings.master_enabled and settings.file_enabled
                )
                self.logs.set_vrchat_debug_enabled(
                    settings.master_enabled and settings.vrchat_debug_enabled
                )
            runtime_log.info("文件日志已%s", "开启" if settings.file_enabled else "关闭")

    def prepare_custom_snapshot(
        self,
        snapshot: SanitizedPresenceSnapshot,
        apply_sensitive_rules: bool,
    ) -> SanitizedPresenceSnapshot:
        if not apply_sensitive_rules:
            return snapshot
        evaluator = PrivacyEvaluator(
            self.store.load_privacy_defaults(),
            self.store.load_rules(),
            self.store.load_sensitive_rules(),
            self.store.load_icon_template(),
        )
        return SanitizedPresenceSnapshot(
            snapshot.observed_at,
            evaluator.filter_application(snapshot.application),
            evaluator.filter_media(snapshot.media),
        )

    async def start_custom_broadcast(self, spec: CustomBroadcastSpec) -> None:
        async with self.mutation_lock:
            if not self._lifecycle_available or self._session_suspended:
                raise ServiceError("锁屏监听不可用或当前会话已锁定")
            await self._stop_vrchat()
            if self.state.connection and self.state.connection.live_desk_enabled:
                self.state.connection = self.store.set_live_desk_enabled(False)
                await self.coordinator.stop(
                    ClearReason.PRIVACY_CHANGED,
                    RuntimeState.DISABLED,
                )
            snapshot = self.prepare_custom_snapshot(
                spec.snapshot,
                spec.apply_sensitive_rules,
            )
            self.consent.clear()
            self.state.preview_current = False
            await self.custom_broadcast.start(
                CustomBroadcastSpec(
                    snapshot,
                    spec.duration_seconds,
                    spec.apply_sensitive_rules,
                )
            )
            self.state.test_broadcast_active = True
            self.state.test_broadcast_status = "正在广播"
            self.state.notice = "测试广播运行中；结束后需重新预览才能开启 Live Desk"
            self._notify()

    async def stop_custom_broadcast(self) -> None:
        async with self.mutation_lock:
            await self.custom_broadcast.stop()
            self.state.test_broadcast_active = False
            self.state.test_broadcast_status = self.custom_broadcast.status
            self._notify()

    def _schedule_vrchat_reconcile(self) -> None:
        if self.vrchat is None or self._shutting_down:
            return
        if self._vrchat_task is not None and not self._vrchat_task.done():
            self._vrchat_task.cancel()
        self._vrchat_task = asyncio.create_task(self._reconcile_vrchat())

    async def _reconcile_vrchat(self) -> None:
        async with self._vrchat_lock:
            settings = self.store.load_vrchat_settings()
            should_run = (
                settings.enabled
                and self.state.runtime_state is RuntimeState.ACTIVE
                and not self.state.paused
                and not self._session_suspended
            )
            if not should_run:
                await self._stop_vrchat_locked()
                if settings.enabled:
                    self.state.vrchat_status = "等待 Live Desk 公开"
                return
            if self.vrchat is None or self.vrchat.running:
                return
            try:
                api_key = (
                    await self.vrchat_credentials.get_api_key()
                    if self.vrchat_credentials is not None
                    else None
                )
                evaluator = PrivacyEvaluator(
                    self.store.load_privacy_defaults(),
                    self.store.load_rules(),
                    self.store.load_sensitive_rules(),
                )
                def request_refresh() -> None:
                    asyncio.create_task(self.coordinator.request_refresh())

                await self.vrchat.start(
                    settings,
                    api_key,
                    evaluator,
                    request_refresh,
                )
                self.state.vrchat_status = "正在捕获"
            except Exception as error:
                self.state.vrchat_status = f"启动失败：{error}"
                runtime_log.warning("VRChat 集成启动失败：%s", type(error).__name__)
            self._notify()

    async def _stop_vrchat(self) -> None:
        task, self._vrchat_task = self._vrchat_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._vrchat_lock:
            await self._stop_vrchat_locked()

    async def _stop_vrchat_locked(self) -> None:
        if self.vrchat is not None:
            await self.vrchat.stop()
