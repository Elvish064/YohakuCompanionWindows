from __future__ import annotations

from yohaku_companion_windows.domain import (
    SensitivePatternKind,
    SensitivePatternModule,
)
from yohaku_companion_windows.sensitive_rules_ui import _module_pattern


def test_any_word_splits_all_delimiters_deduplicates_and_escapes() -> None:
    module = SensitivePatternModule(
        SensitivePatternKind.ANY_WORD,
        "a,b，c、d\ne|f,a.c,,",
    )
    assert _module_pattern(module) == r"(?:a|b|c|d|e|f|a\.c)"


def test_other_text_modules_do_not_split_commas() -> None:
    module = SensitivePatternModule(SensitivePatternKind.CONTAINS, "a,b")
    assert _module_pattern(module) == "a,b"
