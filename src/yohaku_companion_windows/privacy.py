from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import regex

from .domain import (
    ApplicationRule,
    PlaybackState,
    PrivacyDefaults,
    RawApplicationIdentity,
    RawMediaPresence,
    SanitizedApplicationPresence,
    SanitizedMediaPlayback,
    SanitizedMediaPresence,
    SanitizedPresenceSnapshot,
    SensitiveAction,
    SensitiveField,
    SensitiveTextRule,
    ShareMode,
    SourceSettings,
    normalize_text,
)


@dataclass(frozen=True, slots=True)
class ApplicationDecision:
    shares_application: bool
    shares_window_title: bool
    alias: str | None


@dataclass(frozen=True, slots=True)
class MediaDecision:
    shares_media: bool
    alias: str | None


class PrivacyEvaluator:
    def __init__(
        self,
        defaults: PrivacyDefaults,
        rules: tuple[ApplicationRule, ...],
        sensitive_rules: tuple[SensitiveTextRule, ...] = (),
    ) -> None:
        self._defaults = defaults
        self._rules = {rule.normalized().identifier: rule.normalized() for rule in rules}
        self._sensitive_rules = tuple(
            _CompiledSensitiveRule.from_rule(rule.normalized())
            for rule in sorted(sensitive_rules, key=lambda item: item.sort_order)
            if rule.enabled
        )
        self._timed_out_rule_names: list[str] = []

    @property
    def timed_out_rule_names(self) -> tuple[str, ...]:
        return tuple(self._timed_out_rule_names)

    def reset_diagnostics(self) -> None:
        self._timed_out_rule_names.clear()

    def application_decision(self, identifier: str) -> ApplicationDecision:
        rule = self._rules.get(identifier.casefold())
        shares_application = _resolve(
            None if rule is None else rule.application,
            self._defaults.application,
        )
        shares_window = shares_application and _resolve(
            None if rule is None else rule.window_title,
            self._defaults.window_title,
        )
        return ApplicationDecision(
            shares_application=shares_application,
            shares_window_title=shares_window,
            alias=(
                None
                if not shares_application or rule is None
                else normalize_text(rule.alias, 120)
            ),
        )

    def media_decision(self, identifier: str) -> MediaDecision:
        rule = self._rules.get(identifier.casefold())
        shares_media = _resolve(
            None if rule is None else rule.media,
            self._defaults.media,
        )
        return MediaDecision(
            shares_media=shares_media,
            alias=None if not shares_media or rule is None else normalize_text(rule.alias, 120),
        )

    def filter_application(
        self,
        value: SanitizedApplicationPresence | None,
    ) -> SanitizedApplicationPresence | None:
        if value is None:
            return None
        fields: dict[SensitiveField, str | None] = {
            SensitiveField.APPLICATION_NAME: value.display_name,
            SensitiveField.WINDOW_TITLE: value.window_title,
        }
        if self._filter_fields(fields, application_context=True):
            return None
        display_name = fields[SensitiveField.APPLICATION_NAME]
        if display_name is None:
            return None
        return SanitizedApplicationPresence(
            display_name=display_name,
            window_title=fields[SensitiveField.WINDOW_TITLE],
        )

    def filter_media(
        self,
        value: SanitizedMediaPresence | None,
    ) -> SanitizedMediaPresence | None:
        if value is None:
            return None
        fields: dict[SensitiveField, str | None] = {
            SensitiveField.MEDIA_TITLE: value.title,
            SensitiveField.MEDIA_ARTIST: value.artist,
            SensitiveField.MEDIA_ALBUM: value.album,
            SensitiveField.PLAYER_NAME: value.player_display_name,
        }
        if self._filter_fields(fields, application_context=False):
            return None
        if (
            fields[SensitiveField.MEDIA_TITLE] is None
            and fields[SensitiveField.MEDIA_ARTIST] is None
        ):
            return None
        return SanitizedMediaPresence(
            session_id=value.session_id,
            kind=value.kind,
            title=fields[SensitiveField.MEDIA_TITLE],
            artist=fields[SensitiveField.MEDIA_ARTIST],
            album=fields[SensitiveField.MEDIA_ALBUM],
            player_display_name=fields[SensitiveField.PLAYER_NAME],
            playback=value.playback,
        )

    def _filter_fields(
        self,
        fields: dict[SensitiveField, str | None],
        *,
        application_context: bool,
    ) -> bool:
        for rule in self._sensitive_rules:
            for field in rule.fields:
                if field not in fields or fields[field] is None:
                    continue
                original = fields[field]
                assert original is not None
                try:
                    matched = rule.pattern.search(original, timeout=0.005) is not None
                    if not matched:
                        continue
                    if rule.action is SensitiveAction.HIDE_CONTEXT:
                        return True
                    if rule.action is SensitiveAction.HIDE_FIELD:
                        fields[field] = None
                    else:
                        fields[field] = rule.pattern.sub("•••", original, timeout=0.005)
                except TimeoutError:
                    fields[field] = None
                    if rule.name not in self._timed_out_rule_names:
                        self._timed_out_rule_names.append(rule.name)
            required_field = (
                SensitiveField.APPLICATION_NAME
                if application_context
                else SensitiveField.MEDIA_TITLE
            )
            if application_context and fields.get(required_field) is None:
                return True
        return False


@dataclass(frozen=True, slots=True)
class _CompiledSensitiveRule:
    name: str
    fields: tuple[SensitiveField, ...]
    action: SensitiveAction
    pattern: regex.Pattern[str]

    @classmethod
    def from_rule(cls, rule: SensitiveTextRule) -> _CompiledSensitiveRule:
        flags = regex.IGNORECASE | regex.FULLCASE if rule.ignore_case else 0
        return cls(
            name=rule.name,
            fields=rule.fields,
            action=rule.action,
            pattern=regex.compile(rule.pattern, flags),
        )


def sanitize_application(
    identity: RawApplicationIdentity,
    captured_window_title: str | None,
    sources: SourceSettings,
    decision: ApplicationDecision,
) -> SanitizedApplicationPresence | None:
    if not sources.applications or not decision.shares_application:
        return None
    display_name = decision.alias or normalize_text(identity.display_name, 120)
    if display_name is None:
        return None
    window_title = None
    if sources.window_titles and decision.shares_window_title:
        window_title = normalize_text(captured_window_title, 500)
    return SanitizedApplicationPresence(display_name, window_title)


def sanitize_media(
    source: RawMediaPresence,
    sources: SourceSettings,
    decision: MediaDecision,
) -> SanitizedMediaPresence | None:
    if not sources.media or not decision.shares_media:
        return None
    title = normalize_text(source.title, 300)
    artist = normalize_text(source.artist, 300)
    if title is None and artist is None:
        return None
    player_name = decision.alias or normalize_text(source.player_display_name, 120)
    duration = source.duration_seconds
    position = source.position_seconds
    return SanitizedMediaPresence(
        session_id=source.session_id,
        kind=source.kind,
        title=title,
        artist=artist,
        album=normalize_text(source.album, 300),
        player_display_name=player_name,
        playback=SanitizedMediaPlayback(
            state=source.state,
            duration_seconds=duration,
            position_seconds=position,
            sampled_at=source.sampled_at.astimezone(UTC),
            rate=source.rate if source.state is PlaybackState.PLAYING else 0.0,
        ),
    )


def _resolve(mode: ShareMode | None, fallback: bool) -> bool:
    if mode is None or mode is ShareMode.INHERIT:
        return fallback
    return mode is ShareMode.SHARE


@dataclass(frozen=True, slots=True)
class PreviewConfirmation:
    policy_revision: int
    projection: tuple[object, ...]


class PreviewConsentGate:
    def __init__(self) -> None:
        self._policy_revision = 0
        self._confirmation: PreviewConfirmation | None = None

    @property
    def is_current(self) -> bool:
        return (
            self._confirmation is not None
            and self._confirmation.policy_revision == self._policy_revision
        )

    @property
    def confirmation(self) -> PreviewConfirmation | None:
        return self._confirmation

    def policy_changed(self) -> None:
        self._policy_revision += 1
        self._confirmation = None

    def record(self, snapshot: SanitizedPresenceSnapshot) -> PreviewConfirmation:
        projection = snapshot.consent_projection()
        confirmation = PreviewConfirmation(self._policy_revision, projection)
        self._confirmation = confirmation
        return confirmation

    def validates(
        self,
        candidate: PreviewConfirmation | None,
        snapshot: SanitizedPresenceSnapshot,
    ) -> bool:
        if candidate is None or candidate != self._confirmation:
            return False
        if candidate.policy_revision != self._policy_revision:
            return False
        return candidate.projection == snapshot.consent_projection()

    def clear(self) -> None:
        self._confirmation = None


def utc_now() -> datetime:
    return datetime.now(UTC)
