from __future__ import annotations

import logging
from pathlib import Path

from yohaku_companion_windows.logging_service import ProcessLogService, redact_message


def test_log_ring_is_thread_safe_bounded_and_redacted(tmp_path: Path) -> None:
    logs = ProcessLogService(tmp_path, maximum_entries=3)
    logs.install()
    logger = logging.getLogger("yohaku.网络")
    try:
        for index in range(5):
            logger.info("request=%d token=secret-%d", index, index)
        entries = logs.entries()
        assert len(entries) == 3
        assert [entry.sequence for entry in entries] == [3, 4, 5]
        assert all("secret-" not in entry.message for entry in entries)
    finally:
        logs.uninstall()


def test_redaction_covers_authorization_and_api_key() -> None:
    message = redact_message("Bearer abc X-API-Key: xyz pairing_code=123")
    assert "abc" not in message
    assert "xyz" not in message
    assert "123" not in message


def test_optional_file_log_uses_same_sanitized_message(tmp_path: Path) -> None:
    logs = ProcessLogService(tmp_path)
    logs.install()
    logs.set_file_enabled(True)
    try:
        logging.getLogger("yohaku.VRC 上传").warning("api_key=super-secret")
    finally:
        logs.uninstall()
    content = (tmp_path / "companion.log").read_text(encoding="utf-8")
    assert "super-secret" not in content
    assert "已脱敏" in content


def test_vrchat_debug_logs_are_opt_in_but_warnings_remain(tmp_path: Path) -> None:
    logs = ProcessLogService(tmp_path)
    logs.install()
    logger = logging.getLogger("yohaku.VRChat 捕获")
    try:
        logger.debug("high frequency update")
        logger.warning("capture stopped")
        assert [entry.message for entry in logs.entries()] == ["capture stopped"]
        logs.set_vrchat_debug_enabled(True)
        logger.debug("aggregated diagnostics")
        assert logs.entries()[-1].message == "aggregated diagnostics"
    finally:
        logs.uninstall()
