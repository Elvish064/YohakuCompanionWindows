from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .domain import RuleCandidate, SanitizedPresenceSnapshot
from .media_capture import MediaProvider
from .privacy import PrivacyEvaluator, sanitize_application, sanitize_media
from .storage import StateStore
from .win32_capture import ApplicationProvider

if TYPE_CHECKING:
    from .vrchat import VRChatActivityState

privacy_log = logging.getLogger("yohaku.隐私")


class PresenceCapture:
    """The only raw-to-sanitized boundary used by preview and network code."""

    def __init__(
        self,
        store: StateStore,
        applications: ApplicationProvider,
        media: MediaProvider,
        vrchat_activity: VRChatActivityState | None = None,
        on_candidates_changed: Callable[[tuple[RuleCandidate, ...]], None] | None = None,
    ) -> None:
        self._store = store
        self._applications = applications
        self._media = media
        self._vrchat_activity = vrchat_activity
        self._candidates: dict[str, RuleCandidate] = {}
        self._on_candidates_changed = on_candidates_changed
        self._lock = asyncio.Lock()
        self._privacy_key: tuple[object, ...] | None = None
        self._privacy_evaluator: PrivacyEvaluator | None = None
        self._last_filter_timed_out = False

    @property
    def candidates(self) -> tuple[RuleCandidate, ...]:
        return tuple(
            sorted(
                self._candidates.values(),
                key=lambda item: item.display_name.casefold(),
            )
        )

    def set_candidates_callback(
        self,
        callback: Callable[[tuple[RuleCandidate, ...]], None] | None,
    ) -> None:
        self._on_candidates_changed = callback

    @property
    def last_filter_timed_out(self) -> bool:
        return self._last_filter_timed_out

    async def capture(self, include_media: bool = True) -> SanitizedPresenceSnapshot:
        async with self._lock:
            sources = self._store.load_sources()
            defaults = self._store.load_privacy_defaults()
            rules = self._store.load_rules()
            sensitive_rules = self._store.load_sensitive_rules()
            privacy_key = (defaults, rules, sensitive_rules)
            if self._privacy_evaluator is None or privacy_key != self._privacy_key:
                self._privacy_key = privacy_key
                self._privacy_evaluator = PrivacyEvaluator(
                    defaults,
                    rules,
                    sensitive_rules,
                )
            evaluator = self._privacy_evaluator
            evaluator.reset_diagnostics()

            raw_application = (
                await asyncio.to_thread(self._applications.current_application)
                if sources.applications
                else None
            )
            application = None
            if raw_application is not None:
                self._remember(raw_application.identifier, raw_application.display_name)
                decision = evaluator.application_decision(raw_application.identifier)
                title = None
                if sources.window_titles and decision.shares_window_title:
                    title = decision.custom_title
                    if title is None:
                        vrchat = self._store.load_vrchat_settings()
                        if (
                            raw_application.identifier.casefold() == "win32:vrchat.exe"
                            and vrchat.enabled
                            and vrchat.replace_world_title
                            and self._vrchat_activity is not None
                        ):
                            title = self._vrchat_activity.world_name()
                        if title is None:
                            title = await asyncio.to_thread(
                                self._applications.read_window_title,
                                raw_application.window_handle,
                            )
                application = evaluator.filter_application(
                    sanitize_application(raw_application, title, sources, decision)
                )

            if include_media and sources.media:
                raw_media = await self._media.current_media()
            else:
                self.reset_media_continuity()
                raw_media = None
            media = None
            if raw_media is not None:
                self._remember(raw_media.identifier, raw_media.player_display_name)
                media = evaluator.filter_media(
                    sanitize_media(
                        raw_media,
                        sources,
                        evaluator.media_decision(raw_media.identifier),
                    )
                )
            self._last_filter_timed_out = bool(evaluator.timed_out_rule_names)
            if self._last_filter_timed_out:
                privacy_log.warning(
                    "敏感词规则执行超时；受影响字段已 fail-closed 隐藏"
                )
            return SanitizedPresenceSnapshot(datetime.now(UTC), application, media)

    def reset_media_continuity(self) -> None:
        reset = getattr(self._media, "reset_continuity", None)
        if callable(reset):
            reset()

    def _remember(self, identifier: str, display_name: str) -> None:
        candidate = RuleCandidate(identifier.casefold(), display_name)
        if self._candidates.get(candidate.identifier) == candidate:
            return
        self._candidates[candidate.identifier] = candidate
        if self._on_candidates_changed is not None:
            self._on_candidates_changed(self.candidates)
