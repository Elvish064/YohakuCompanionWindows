from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit

from tests.helpers import DEVICE_ID
from tests.test_service import Credentials, Media
from yohaku_companion_windows.capture import PresenceCapture
from yohaku_companion_windows.domain import (
    ConnectionMetadata,
    RuntimeState,
    SensitiveField,
    SensitivePatternKind,
    SensitiveTextRule,
    ShareMode,
)
from yohaku_companion_windows.identity import AppIdentity
from yohaku_companion_windows.sensitive_rules_ui import SensitiveRuleEditor
from yohaku_companion_windows.service import ApplicationService
from yohaku_companion_windows.startup import StartupManager
from yohaku_companion_windows.storage import StateStore
from yohaku_companion_windows.ui import SettingsWindow, TrayController, _mode_combo
from yohaku_companion_windows.visuals import status_icon
from yohaku_companion_windows.win32_capture import ApplicationProvider


class NoApplication(ApplicationProvider):
    def current_application(self):  # type: ignore[no-untyped-def]
        return None

    def read_window_title(self, window_handle: int):  # type: ignore[no-untyped-def]
        raise AssertionError("title must not be read")


def test_chinese_pairing_and_paired_states_hide_sensitive_values(
    qtbot, tmp_path: Path  # type: ignore[no-untyped-def]
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    media = Media()
    service = ApplicationService(
        store,
        Credentials("never-visible-token"),
        PresenceCapture(store, NoApplication(), media),
        media,
    )
    window = SettingsWindow(
        service,
        StartupManager(AppIdentity("dev.innei.YohakuCompanion.windows.debug")),
        QIcon(),
    )
    qtbot.addWidget(window)
    assert window._stack.currentWidget() is window._pairing_page
    assert window._code_edit.echoMode() is QLineEdit.EchoMode.Password
    window._code_edit.setFocus()
    qtbot.keyClicks(window._code_edit, "ABC123")
    assert window._code_edit.text() == "ABC123"
    service.state.connection = ConnectionMetadata(
        "https://example.com", DEVICE_ID, (), 0, False
    )
    service.state.runtime_state = RuntimeState.DISABLED
    window.render_state(service.state)
    assert window._stack.currentWidget() is window._paired_page
    assert window._preview_group.title() == "待确认的净化预览（开启前请核对）"
    all_text = " ".join(label.text() for label in window.findChildren(QLineEdit))
    assert "never-visible-token" not in all_text
    assert not window._live_button.isEnabled()
    application = QApplication.instance()
    assert application is not None

    async def quit_application() -> None:
        return None

    tray = TrayController(application, service, window, QIcon(), quit_application)
    assert tray._live_action.text() == "开启 Live Desk"
    service.state.connection = ConnectionMetadata(
        "https://example.com", DEVICE_ID, (), 0, True
    )
    window.render_state(service.state)
    assert window._preview_group.title() == "最近成功发布的净化快照"
    tray._render(service.state)
    assert tray._live_action.text() == "关闭 Live Desk"
    assert tray._pause_action.isEnabled()
    tray.hide()
    window.begin_quit()
    window.close()
    store.close()


def test_status_and_share_modes_have_icons_text_and_accessibility(qtbot) -> None:  # type: ignore[no-untyped-def]
    for state in RuntimeState:
        assert not status_icon(state).isNull()
    for mode in ShareMode:
        combo = _mode_combo(mode)
        qtbot.addWidget(combo)
        assert combo.count() == 3
        assert all(not combo.itemIcon(index).isNull() for index in range(combo.count()))
        assert combo.itemText(combo.currentIndex()) in {"继承", "分享", "隐藏"}
        assert combo.accessibleDescription()


def test_sensitive_rule_editor_validates_and_tests_regex(qtbot) -> None:  # type: ignore[no-untyped-def]
    editor = SensitiveRuleEditor()
    qtbot.addWidget(editor)
    save = editor.buttons.button(QDialogButtonBox.StandardButton.Save)
    assert save is not None and not save.isEnabled()
    editor.name_edit.setText("账号屏蔽")
    editor.pattern_edit.setText(r"token-\d+")
    editor.field_checks[SensitiveField.WINDOW_TITLE].setChecked(True)
    editor.test_edit.setPlainText("公开 token-123 内容")
    assert save.isEnabled()
    assert editor.test_result.toPlainText() == "公开 ••• 内容"
    rule = editor.rule()
    assert rule.name == "账号屏蔽"
    assert rule.fields == (SensitiveField.WINDOW_TITLE,)


def test_graphical_expression_modules_generate_safe_regex(qtbot) -> None:  # type: ignore[no-untyped-def]
    editor = SensitiveRuleEditor()
    qtbot.addWidget(editor)
    editor.module_value.setText("账号(123)")
    editor._add_module()
    assert editor.pattern_edit.text() == r"(?:账号\(123\))"
    assert editor._modules[0].kind is SensitivePatternKind.CONTAINS
    editor.module_kind.setCurrentIndex(
        editor.module_kind.findData(SensitivePatternKind.EMAIL.value)
    )
    editor._add_module()
    assert "@" in editor.pattern_edit.text()
    assert editor.module_list.count() == 2


@pytest.mark.asyncio
async def test_dialog_accept_adds_rule_and_main_save_keeps_it_visible(
    qtbot, tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    media = Media()
    service = ApplicationService(
        store,
        Credentials(),
        PresenceCapture(store, NoApplication(), media),
        media,
    )
    window = SettingsWindow(
        service,
        StartupManager(AppIdentity("dev.innei.YohakuCompanion.windows.debug")),
        QIcon(),
    )
    qtbot.addWidget(window)
    rule = SensitiveTextRule(
        "123e4567-e89b-12d3-a456-426614174055",
        "保留规则",
        "secret",
        (SensitiveField.WINDOW_TITLE,),
    )

    class FakeEditor:
        def __init__(self, *_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def exec(self) -> int:
            return 1

        def rule(self, sort_order: int) -> SensitiveTextRule:
            return SensitiveTextRule(
                rule.identifier,
                rule.name,
                rule.pattern,
                rule.fields,
                sort_order=sort_order,
            )

    monkeypatch.setattr(
        "yohaku_companion_windows.ui.SensitiveRuleEditor", FakeEditor
    )
    window._add_sensitive_rule()
    assert window._sensitive_table.rowCount() == 1
    await window._save_privacy()
    assert window._sensitive_table.rowCount() == 1
    assert store.load_sensitive_rules()[0].name == "保留规则"
    window.begin_quit()
    window.close()
    store.close()
