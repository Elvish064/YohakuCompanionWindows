from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import regex

from .domain import (
    ApplicationIconTemplateSettings,
    ApplicationRule,
    ConnectionMetadata,
    LoggingSettings,
    PrivacyDefaults,
    SensitiveAction,
    SensitiveField,
    SensitivePatternKind,
    SensitivePatternModule,
    SensitiveTextRule,
    ShareMode,
    SourceSettings,
    VRChatIntegrationSettings,
)
from .protocol import (
    MAXIMUM_SAFE_INTEGER,
    ServerConfiguration,
    validate_identifier,
    validate_safe_integer,
)


class StorageError(RuntimeError):
    pass


class StateStore:
    """SQLite authority for non-secret state and durable device sequences."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY NOT NULL,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS privacy_rules (
                    identifier TEXT PRIMARY KEY NOT NULL,
                    display_name TEXT NOT NULL,
                    application TEXT NOT NULL,
                    window_title TEXT NOT NULL,
                    media TEXT NOT NULL,
                    alias TEXT,
                    custom_title TEXT,
                    icon_filename TEXT,
                    activity_key TEXT,
                    activity_custom_label TEXT,
                    media_artwork_url TEXT
                );
                CREATE TABLE IF NOT EXISTS presence_sequences (
                    device_id TEXT PRIMARY KEY NOT NULL,
                    next_sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS privacy_regex_rules (
                    identifier TEXT PRIMARY KEY NOT NULL,
                    name TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    fields TEXT NOT NULL,
                    action TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    ignore_case INTEGER NOT NULL,
                    sort_order INTEGER NOT NULL,
                    pattern_modules TEXT NOT NULL DEFAULT '[]'
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(privacy_regex_rules)"
                ).fetchall()
            }
            if "pattern_modules" not in columns:
                self._connection.execute(
                    "ALTER TABLE privacy_regex_rules "
                    "ADD COLUMN pattern_modules TEXT NOT NULL DEFAULT '[]'"
                )
            privacy_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(privacy_rules)"
                ).fetchall()
            }
            if "custom_title" not in privacy_columns:
                self._connection.execute(
                    "ALTER TABLE privacy_rules ADD COLUMN custom_title TEXT"
                )
            for name in (
                "icon_filename",
                "activity_key",
                "activity_custom_label",
                "media_artwork_url",
            ):
                if name not in privacy_columns:
                    self._connection.execute(
                        f"ALTER TABLE privacy_rules ADD COLUMN {name} TEXT"
                    )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def load_connection(self) -> ConnectionMetadata | None:
        raw = self._read_metadata("connection.v1")
        if raw is None:
            return None
        try:
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError
            metadata = ConnectionMetadata.from_dict(decoded)
            ServerConfiguration(metadata.base_url)
            validate_identifier(metadata.device_id, "connection.deviceId")
            validate_safe_integer(metadata.pairing_next_sequence, "connection.nextSequence")
            return metadata
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError("invalid connection metadata") from error

    def save_connection(self, metadata: ConnectionMetadata) -> None:
        validate_identifier(metadata.device_id, "connection.deviceId")
        validate_safe_integer(metadata.pairing_next_sequence, "connection.nextSequence")
        self._write_metadata("connection.v1", _json(metadata.to_dict()))

    def install_connection(
        self,
        metadata: ConnectionMetadata,
        replaced_device_id: str | None = None,
    ) -> None:
        """Atomically installs non-secret pairing state in its disabled state."""
        validate_identifier(metadata.device_id, "connection.deviceId")
        validate_safe_integer(metadata.pairing_next_sequence, "connection.nextSequence")
        if metadata.live_desk_enabled:
            raise StorageError("a new connection must start disabled")
        if replaced_device_id is not None:
            validate_identifier(replaced_device_id, "connection.replacedDeviceId")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("connection.v1", _json(metadata.to_dict())),
            )
            self._connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("live_desk.paused", "0"),
            )
            if replaced_device_id is not None and replaced_device_id != metadata.device_id:
                self._connection.execute(
                    "DELETE FROM presence_sequences WHERE device_id = ?",
                    (replaced_device_id,),
                )

    def set_live_desk_enabled(self, enabled: bool) -> ConnectionMetadata | None:
        metadata = self.load_connection()
        if metadata is None:
            return None
        updated = ConnectionMetadata(
            base_url=metadata.base_url,
            device_id=metadata.device_id,
            scopes=metadata.scopes,
            pairing_next_sequence=metadata.pairing_next_sequence,
            live_desk_enabled=enabled,
        )
        self.save_connection(updated)
        return updated

    def remove_connection(self) -> None:
        metadata = self.load_connection()
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM metadata WHERE key = ?", ("connection.v1",))
            if metadata is not None:
                self._connection.execute(
                    "DELETE FROM presence_sequences WHERE device_id = ?",
                    (metadata.device_id,),
                )

    def load_sources(self) -> SourceSettings:
        raw = self._read_metadata("sources.v1")
        if raw is None:
            return SourceSettings()
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return SourceSettings.from_dict(value)
        except (TypeError, ValueError) as error:
            raise StorageError("invalid source settings") from error

    def save_sources(self, settings: SourceSettings) -> None:
        self._write_metadata("sources.v1", _json(settings.to_dict()))

    def load_privacy_defaults(self) -> PrivacyDefaults:
        raw = self._read_metadata("privacy.defaults.v1")
        if raw is None:
            return PrivacyDefaults()
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return PrivacyDefaults.from_dict(value)
        except (TypeError, ValueError) as error:
            raise StorageError("invalid privacy defaults") from error

    def save_privacy_defaults(self, defaults: PrivacyDefaults) -> None:
        self._write_metadata("privacy.defaults.v1", _json(defaults.to_dict()))

    def load_rules(self) -> tuple[ApplicationRule, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT identifier, display_name, application, window_title,
                       media, alias, custom_title, icon_filename, activity_key,
                       activity_custom_label, media_artwork_url
                FROM privacy_rules ORDER BY display_name COLLATE NOCASE, identifier
                """
            ).fetchall()
        try:
            return tuple(
                ApplicationRule(
                    identifier=str(row["identifier"]),
                    display_name=str(row["display_name"]),
                    application=ShareMode(str(row["application"])),
                    window_title=ShareMode(str(row["window_title"])),
                    media=ShareMode(str(row["media"])),
                    alias=None if row["alias"] is None else str(row["alias"]),
                    custom_title=(
                        None
                        if row["custom_title"] is None
                        else str(row["custom_title"])
                    ),
                    icon_filename=(
                        None
                        if row["icon_filename"] is None
                        else str(row["icon_filename"])
                    ),
                    activity_key=(
                        None
                        if row["activity_key"] is None
                        else str(row["activity_key"])
                    ),
                    activity_custom_label=(
                        None if row["activity_custom_label"] is None
                        else str(row["activity_custom_label"])
                    ),
                    media_artwork_url=(
                        None if row["media_artwork_url"] is None
                        else str(row["media_artwork_url"])
                    ),
                ).normalized()
                for row in rows
            )
        except ValueError as error:
            raise StorageError("invalid privacy rule") from error

    def save_rule(self, rule: ApplicationRule) -> None:
        normalized = rule.normalized()
        if not normalized.identifier:
            raise StorageError("privacy rule identifier is required")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO privacy_rules(
                    identifier, display_name, application, window_title,
                    media, alias, custom_title, icon_filename, activity_key,
                    activity_custom_label, media_artwork_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identifier) DO UPDATE SET
                    display_name = excluded.display_name,
                    application = excluded.application,
                    window_title = excluded.window_title,
                    media = excluded.media,
                    alias = excluded.alias,
                    custom_title = excluded.custom_title,
                    icon_filename = excluded.icon_filename,
                    activity_key = excluded.activity_key,
                    activity_custom_label = excluded.activity_custom_label,
                    media_artwork_url = excluded.media_artwork_url
                """,
                (
                    normalized.identifier,
                    normalized.display_name,
                    normalized.application.value,
                    normalized.window_title.value,
                    normalized.media.value,
                    normalized.alias,
                    normalized.custom_title,
                    normalized.icon_filename,
                    normalized.activity_key,
                    normalized.activity_custom_label,
                    normalized.media_artwork_url,
                ),
            )

    def replace_rules(self, rules: tuple[ApplicationRule, ...]) -> None:
        normalized = tuple(rule.normalized() for rule in rules)
        if len({rule.identifier for rule in normalized}) != len(normalized):
            raise StorageError("duplicate privacy rule")
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM privacy_rules")
            self._connection.executemany(
                """
                INSERT INTO privacy_rules(
                    identifier, display_name, application, window_title,
                    media, alias, custom_title, icon_filename, activity_key,
                    activity_custom_label, media_artwork_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        rule.identifier,
                        rule.display_name,
                        rule.application.value,
                        rule.window_title.value,
                        rule.media.value,
                        rule.alias,
                        rule.custom_title,
                        rule.icon_filename,
                        rule.activity_key,
                        rule.activity_custom_label,
                        rule.media_artwork_url,
                    )
                    for rule in normalized
                ],
            )

    def load_sensitive_rules(self) -> tuple[SensitiveTextRule, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT identifier, name, pattern, fields, action,
                       enabled, ignore_case, sort_order, pattern_modules
                FROM privacy_regex_rules ORDER BY sort_order, identifier
                """
            ).fetchall()
        try:
            rules = tuple(
                SensitiveTextRule(
                    identifier=str(row["identifier"]),
                    name=str(row["name"]),
                    pattern=str(row["pattern"]),
                    fields=tuple(
                        SensitiveField(item) for item in json.loads(str(row["fields"]))
                    ),
                    action=SensitiveAction(str(row["action"])),
                    enabled=bool(row["enabled"]),
                    ignore_case=bool(row["ignore_case"]),
                    sort_order=int(row["sort_order"]),
                    pattern_modules=tuple(
                        SensitivePatternModule(
                            SensitivePatternKind(item["kind"]),
                            str(item.get("value", "")),
                        )
                        for item in json.loads(str(row["pattern_modules"]))
                    ),
                ).normalized()
                for row in rows
            )
            if len(rules) > 50:
                raise ValueError("too many sensitive rules")
            _validate_sensitive_patterns(rules)
            return rules
        except (
            KeyError,
            TypeError,
            ValueError,
            TimeoutError,
            json.JSONDecodeError,
            regex.error,
        ) as error:
            raise StorageError("invalid sensitive text rule") from error

    @staticmethod
    def _normalize_sensitive_rules(
        rules: tuple[SensitiveTextRule, ...],
    ) -> tuple[SensitiveTextRule, ...]:
        if len(rules) > 50:
            raise StorageError("at most 50 sensitive text rules are allowed")
        normalized = tuple(rule.normalized() for rule in rules)
        if len({rule.identifier for rule in normalized}) != len(normalized):
            raise StorageError("duplicate sensitive text rule")
        try:
            _validate_sensitive_patterns(normalized)
        except (TimeoutError, regex.error) as error:
            raise StorageError("invalid or unsafe sensitive text rule") from error
        return tuple(sorted(normalized, key=lambda rule: (rule.sort_order, rule.identifier)))

    def replace_sensitive_rules(self, rules: tuple[SensitiveTextRule, ...]) -> None:
        normalized = self._normalize_sensitive_rules(rules)
        with self._lock, self._connection:
            self._replace_sensitive_rules_in_transaction(normalized)

    def save_privacy_configuration(
        self,
        sources: SourceSettings,
        defaults: PrivacyDefaults,
        rules: tuple[ApplicationRule, ...],
        sensitive_rules: tuple[SensitiveTextRule, ...],
        icon_template: ApplicationIconTemplateSettings | None = None,
    ) -> None:
        normalized = tuple(rule.normalized() for rule in rules)
        if len({rule.identifier for rule in normalized}) != len(normalized):
            raise StorageError("duplicate privacy rule")
        normalized_sensitive = self._normalize_sensitive_rules(sensitive_rules)
        with self._lock, self._connection:
            for key, value in (
                ("sources.v1", _json(sources.to_dict())),
                ("privacy.defaults.v1", _json(defaults.to_dict())),
                (
                    "application.icon-template.v1",
                    _json((icon_template or self.load_icon_template()).to_dict()),
                ),
            ):
                self._connection.execute(
                    """
                    INSERT INTO metadata(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
            self._connection.execute("DELETE FROM privacy_rules")
            self._connection.executemany(
                """
                INSERT INTO privacy_rules(
                    identifier, display_name, application, window_title,
                    media, alias, custom_title, icon_filename, activity_key,
                    activity_custom_label, media_artwork_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        rule.identifier,
                        rule.display_name,
                        rule.application.value,
                        rule.window_title.value,
                        rule.media.value,
                        rule.alias,
                        rule.custom_title,
                        rule.icon_filename,
                        rule.activity_key,
                        rule.activity_custom_label,
                        rule.media_artwork_url,
                    )
                    for rule in normalized
                ],
            )
            self._replace_sensitive_rules_in_transaction(normalized_sensitive)

    def load_icon_template(self) -> ApplicationIconTemplateSettings:
        raw = self._read_metadata("application.icon-template.v1")
        if raw is None:
            return ApplicationIconTemplateSettings()
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return ApplicationIconTemplateSettings.from_dict(value)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StorageError("invalid application icon template") from error

    def save_icon_template(self, settings: ApplicationIconTemplateSettings) -> None:
        self._write_metadata(
            "application.icon-template.v1",
            _json(settings.to_dict()),
        )

    def _replace_sensitive_rules_in_transaction(
        self,
        rules: tuple[SensitiveTextRule, ...],
    ) -> None:
        self._connection.execute("DELETE FROM privacy_regex_rules")
        self._connection.executemany(
            """
            INSERT INTO privacy_regex_rules(
                identifier, name, pattern, fields, action,
                enabled, ignore_case, sort_order, pattern_modules
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    rule.identifier,
                    rule.name,
                    rule.pattern,
                    json.dumps([field.value for field in rule.fields]),
                    rule.action.value,
                    int(rule.enabled),
                    int(rule.ignore_case),
                    rule.sort_order,
                    json.dumps(
                        [
                            {"kind": module.kind.value, "value": module.value}
                            for module in rule.pattern_modules
                        ],
                        ensure_ascii=False,
                    ),
                )
                for rule in rules
            ],
        )

    def remove_rule(self, identifier: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM privacy_rules WHERE identifier = ?", (identifier.casefold(),)
            )

    def is_paused(self) -> bool:
        return self._read_metadata("live_desk.paused") == "1"

    def set_paused(self, paused: bool) -> None:
        self._write_metadata("live_desk.paused", "1" if paused else "0")

    def load_vrchat_settings(self) -> VRChatIntegrationSettings:
        raw = self._read_metadata("vrchat.integration.v1")
        if raw is None:
            return VRChatIntegrationSettings()
        try:
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError
            return VRChatIntegrationSettings.from_dict(decoded)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StorageError("invalid VRChat integration settings") from error

    def save_vrchat_settings(self, settings: VRChatIntegrationSettings) -> None:
        self._write_metadata("vrchat.integration.v1", _json(settings.to_dict()))

    def load_logging_settings(self) -> LoggingSettings:
        raw = self._read_metadata("logging.settings.v1")
        if raw is None:
            return LoggingSettings()
        try:
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError
            return LoggingSettings.from_dict(decoded)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StorageError("invalid logging settings") from error

    def save_logging_settings(self, settings: LoggingSettings) -> None:
        self._write_metadata("logging.settings.v1", _json(settings.to_dict()))

    def reserve_sequence(self, device_id: str, pairing_next_sequence: int) -> int:
        validate_identifier(device_id, "sequence.deviceId")
        validate_safe_integer(pairing_next_sequence, "sequence.pairingNextSequence")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT next_sequence FROM presence_sequences WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                stored = pairing_next_sequence if row is None else int(row["next_sequence"])
                current = max(pairing_next_sequence, stored)
                if current >= MAXIMUM_SAFE_INTEGER:
                    raise StorageError("device sequence exhausted")
                following = current + 1
                self._connection.execute(
                    """
                    INSERT INTO presence_sequences(device_id, next_sequence) VALUES (?, ?)
                    ON CONFLICT(device_id) DO UPDATE SET next_sequence = excluded.next_sequence
                    """,
                    (device_id, following),
                )
                self._connection.commit()
                return current
            except Exception:
                self._connection.rollback()
                raise

    def reconcile_sequence(self, device_id: str, accepted_sequence: int) -> None:
        validate_safe_integer(accepted_sequence, "sequence.acceptedSequence")
        if accepted_sequence >= MAXIMUM_SAFE_INTEGER:
            raise StorageError("device sequence exhausted")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT next_sequence FROM presence_sequences WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                current = 0 if row is None else int(row["next_sequence"])
                reconciled = max(current, accepted_sequence + 1)
                self._connection.execute(
                    """
                    INSERT INTO presence_sequences(device_id, next_sequence) VALUES (?, ?)
                    ON CONFLICT(device_id) DO UPDATE SET next_sequence = excluded.next_sequence
                    """,
                    (device_id, reconciled),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def next_sequence(self, device_id: str) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT next_sequence FROM presence_sequences WHERE device_id = ?", (device_id,)
            ).fetchone()
        return None if row is None else int(row["next_sequence"])

    def remove_sequence(self, device_id: str) -> None:
        validate_identifier(device_id, "sequence.deviceId")
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM presence_sequences WHERE device_id = ?", (device_id,)
            )

    def _read_metadata(self, key: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row["value"])

    def _write_metadata(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_sensitive_patterns(rules: tuple[SensitiveTextRule, ...]) -> None:
    probe = "a" * 4096 + "!"
    for rule in rules:
        flags = regex.IGNORECASE | regex.FULLCASE if rule.ignore_case else 0
        compiled = regex.compile(rule.pattern, flags)
        compiled.search(probe, timeout=0.005)
