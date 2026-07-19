from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from yohaku_companion_windows.capture import PresenceCapture
from yohaku_companion_windows.domain import (
    ApplicationRule,
    MediaKind,
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
    SensitivePatternKind,
    SensitivePatternModule,
    SensitiveTextRule,
    ShareMode,
    SourceSettings,
)
from yohaku_companion_windows.privacy import PreviewConsentGate, PrivacyEvaluator
from yohaku_companion_windows.storage import StateStore


class ApplicationStub:
    def __init__(self) -> None:
        self.current_reads = 0
        self.title_reads = 0

    def current_application(self) -> RawApplicationIdentity:
        self.current_reads += 1
        return RawApplicationIdentity("win32:secret.exe", "Secret", 42)

    def read_window_title(self, window_handle: int) -> str:
        assert window_handle == 42
        self.title_reads += 1
        return "敏感文档标题"


class MediaStub:
    available = True

    async def start(self, on_change=None) -> bool:  # type: ignore[no-untyped-def]
        return True

    async def current_media(self) -> RawMediaPresence:
        return RawMediaPresence(
            "aumid:music.player",
            "Music",
            UUID("123e4567-e89b-12d3-a456-426614174099"),
            MediaKind.MUSIC,
            "Song",
            "Artist",
            None,
            PlaybackState.PLAYING,
            180,
            10,
            datetime.now(UTC),
            1,
        )

    async def stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_disabled_title_is_never_read(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    applications = ApplicationStub()
    capture = PresenceCapture(store, applications, MediaStub())
    snapshot = await capture.capture()
    assert applications.title_reads == 0
    assert snapshot.application is not None
    assert snapshot.application.window_title is None
    store.close()


@pytest.mark.asyncio
async def test_disabled_application_source_is_not_read(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.save_sources(SourceSettings(False, False, True))
    applications = ApplicationStub()
    snapshot = await PresenceCapture(store, applications, MediaStub()).capture()
    assert applications.current_reads == 0
    assert snapshot.application is None
    store.close()


@pytest.mark.asyncio
async def test_both_global_and_application_rules_required_for_title(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.save_sources(SourceSettings(True, True, True))
    store.save_privacy_defaults(PrivacyDefaults(True, True, True))
    store.save_rule(
        ApplicationRule(
            "win32:secret.exe", "Secret", window_title=ShareMode.HIDE
        )
    )
    applications = ApplicationStub()
    snapshot = await PresenceCapture(store, applications, MediaStub()).capture()
    assert applications.title_reads == 0
    assert snapshot.application is not None and snapshot.application.window_title is None
    store.close()


def test_hide_overrides_alias_and_media_is_independent() -> None:
    evaluator = PrivacyEvaluator(
        PrivacyDefaults(True, False, True),
        (
            ApplicationRule(
                "app", "App", application=ShareMode.HIDE, media=ShareMode.SHARE, alias="Alias"
            ),
        ),
    )
    app = evaluator.application_decision("app")
    media = evaluator.media_decision("app")
    assert not app.shares_application and app.alias is None
    assert media.shares_media and media.alias == "Alias"


def test_policy_change_invalidates_consent_but_position_progress_does_not() -> None:
    gate = PreviewConsentGate()
    session_id = UUID("123e4567-e89b-12d3-a456-426614174099")

    def snapshot(position: float) -> SanitizedPresenceSnapshot:
        media = SanitizedMediaPresence(
            session_id,
            MediaKind.MUSIC,
            "Track",
            "Artist",
            None,
            "Player",
            SanitizedMediaPlayback(
                PlaybackState.PLAYING,
                180,
                position,
                datetime.now(UTC),
                1,
            ),
        )
        return SanitizedPresenceSnapshot(datetime.now(UTC), None, media)

    first = snapshot(10)
    confirmation = gate.record(first)
    assert gate.validates(confirmation, snapshot(30))
    gate.policy_changed()
    assert not gate.validates(confirmation, snapshot(30))


def sensitive_rule(
    pattern: str,
    fields: tuple[SensitiveField, ...],
    action: SensitiveAction,
    *,
    ignore_case: bool = True,
) -> SensitiveTextRule:
    return SensitiveTextRule(
        "123e4567-e89b-12d3-a456-426614174000",
        "测试规则",
        pattern,
        fields,
        action,
        ignore_case=ignore_case,
    )


def test_sensitive_rules_run_after_alias_and_support_all_actions() -> None:
    evaluator = PrivacyEvaluator(
        PrivacyDefaults(),
        (),
        (
            sensitive_rule(
                "secret",
                (SensitiveField.APPLICATION_NAME,),
                SensitiveAction.MASK_MATCH,
            ),
            SensitiveTextRule(
                "123e4567-e89b-12d3-a456-426614174001",
                "标题",
                "机密",
                (SensitiveField.WINDOW_TITLE,),
                SensitiveAction.HIDE_FIELD,
                sort_order=1,
            ),
        ),
    )
    filtered = evaluator.filter_application(
        SanitizedApplicationPresence("Secret Browser", "机密项目")
    )
    assert filtered is not None
    assert filtered.display_name == "••• Browser"
    assert filtered.window_title is None

    context = PrivacyEvaluator(
        PrivacyDefaults(),
        (),
        (
            sensitive_rule(
                "artist",
                (SensitiveField.MEDIA_ARTIST,),
                SensitiveAction.HIDE_CONTEXT,
            ),
        ),
    )
    media = SanitizedMediaPresence(
        UUID("123e4567-e89b-12d3-a456-426614174099"),
        MediaKind.MUSIC,
        "Song",
        "ARTIST",
        "Album",
        "Player",
        SanitizedMediaPlayback(PlaybackState.PAUSED, 100, 10, datetime.now(UTC), 0),
    )
    assert context.filter_media(media) is None


def test_sensitive_regex_timeout_fails_closed_without_original_text() -> None:
    evaluator = PrivacyEvaluator(
        PrivacyDefaults(),
        (),
        (
            sensitive_rule(
                r"(a+)+$",
                (SensitiveField.WINDOW_TITLE,),
                SensitiveAction.MASK_MATCH,
            ),
        ),
    )
    filtered = evaluator.filter_application(
        SanitizedApplicationPresence("Browser", "a" * 499 + "!")
    )
    assert filtered is not None and filtered.window_title is None
    assert evaluator.timed_out_rule_names == ("测试规则",)


def test_sensitive_rule_storage_migrates_old_database_and_preserves_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
    )
    legacy.commit()
    legacy.close()
    store = StateStore(path)
    assert store.load_sensitive_rules() == ()
    first = sensitive_rule(
        "one", (SensitiveField.MEDIA_TITLE,), SensitiveAction.HIDE_FIELD
    )
    first = SensitiveTextRule(
        identifier=first.identifier,
        name=first.name,
        pattern=first.pattern,
        fields=first.fields,
        action=first.action,
        pattern_modules=(
            SensitivePatternModule(SensitivePatternKind.CONTAINS, "one"),
        ),
    )
    second = SensitiveTextRule(
        "123e4567-e89b-12d3-a456-426614174002",
        "第二条",
        "two",
        (SensitiveField.PLAYER_NAME,),
        SensitiveAction.MASK_MATCH,
        sort_order=1,
    )
    store.save_privacy_configuration(
        SourceSettings(), PrivacyDefaults(), (), (second, first)
    )
    assert [rule.name for rule in store.load_sensitive_rules()] == ["测试规则", "第二条"]
    assert store.load_sensitive_rules()[0].pattern_modules == (
        SensitivePatternModule(SensitivePatternKind.CONTAINS, "one"),
    )
    store.close()
