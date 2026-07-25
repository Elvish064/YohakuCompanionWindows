from __future__ import annotations

import logging
import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LogEntry:
    sequence: int
    occurred_at: datetime
    level: str
    category: str
    message: str


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[^\s,;]+"),
    re.compile(r"(?i)(X-API-Key|api[_ -]?key|pairing[_ -]?code|token)\s*[:=]\s*[^\s,;]+"),
)


def redact_message(message: str) -> str:
    value = str(message).replace("\r", " ").replace("\n", " ")
    value = _SECRET_PATTERNS[0].sub("Bearer [已脱敏]", value)
    value = _SECRET_PATTERNS[1].sub(lambda match: f"{match.group(1)}=[已脱敏]", value)
    return value[:2000]


class ProcessLogService(logging.Handler):
    """Thread-safe bounded log center and optional sanitized daily file sink."""

    def __init__(self, log_directory: Path, maximum_entries: int = 1000) -> None:
        super().__init__(logging.DEBUG)
        self.log_directory = log_directory
        self._entries: deque[LogEntry] = deque(maxlen=maximum_entries)
        self._lock = threading.RLock()
        self._sequence = 0
        self._file_handler: TimedRotatingFileHandler | None = None
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    def install(self) -> None:
        root = logging.getLogger("yohaku")
        root.setLevel(logging.DEBUG)
        root.propagate = False
        if self not in root.handlers:
            root.addHandler(self)
        self.set_vrchat_debug_enabled(False)

    def uninstall(self) -> None:
        root = logging.getLogger("yohaku")
        if self in root.handlers:
            root.removeHandler(self)
        self.set_file_enabled(False)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact_message(record.getMessage())
            category = record.name.removeprefix("yohaku.") or "运行状态"
            occurred_at = datetime.fromtimestamp(record.created).astimezone()
            with self._lock:
                self._sequence += 1
                self._entries.append(
                    LogEntry(
                        self._sequence,
                        occurred_at,
                        record.levelname,
                        category,
                        message,
                    )
                )
                file_handler = self._file_handler
                if file_handler is not None:
                    clone = logging.makeLogRecord(record.__dict__.copy())
                    clone.msg = message
                    clone.args = ()
                    file_handler.emit(clone)
        except Exception:
            self.handleError(record)

    def entries(self, after_sequence: int = 0) -> tuple[LogEntry, ...]:
        with self._lock:
            return tuple(item for item in self._entries if item.sequence > after_sequence)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    @property
    def file_enabled(self) -> bool:
        with self._lock:
            return self._file_handler is not None

    def set_file_enabled(self, enabled: bool) -> None:
        with self._lock:
            if enabled and self._file_handler is None:
                self.log_directory.mkdir(parents=True, exist_ok=True)
                handler = TimedRotatingFileHandler(
                    self.log_directory / "companion.log",
                    when="midnight",
                    interval=1,
                    backupCount=1,
                    encoding="utf-8",
                    delay=True,
                )
                handler.setFormatter(self.formatter)
                self._file_handler = handler
            elif not enabled and self._file_handler is not None:
                self._file_handler.close()
                self._file_handler = None

    @staticmethod
    def set_vrchat_debug_enabled(enabled: bool) -> None:
        level = logging.DEBUG if enabled else logging.WARNING
        logging.getLogger("yohaku.VRChat 捕获").setLevel(level)
        logging.getLogger("yohaku.VRC 上传").setLevel(level)
