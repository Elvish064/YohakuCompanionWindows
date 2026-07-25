from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .domain import (
    ApplicationRule,
    LoggingSettings,
    PrivacyDefaults,
    RuntimeState,
    SensitiveAction,
    SensitiveTextRule,
    ServiceViewState,
    ShareMode,
    SourceSettings,
    VRChatIntegrationSettings,
)
from .logging_service import ProcessLogService
from .sensitive_rules_ui import ACTION_NAMES, FIELD_NAMES, SensitiveRuleEditor
from .service import ApplicationService
from .startup import StartupManager
from .visuals import MODE_VISUALS, STATUS_VISUALS, mode_icon, status_icon

_STATE_NAMES = {
    RuntimeState.DISABLED: "已关闭",
    RuntimeState.CONNECTING: "正在连接",
    RuntimeState.UPDATE_REQUIRED: "需要更新客户端",
    RuntimeState.FEATURE_UNAVAILABLE: "服务器暂不可用",
    RuntimeState.ACTIVE: "已连接并公开",
    RuntimeState.DEGRADED: "连接中断，正在重试",
    RuntimeState.SUSPENDED: "已暂停",
}


class LogPanel(QWidget):
    def __init__(
        self,
        logs: ProcessLogService,
        service: ApplicationService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._logs = logs
        self._service = service
        self._render_signature: tuple[object, ...] | None = None
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.level_filter = QComboBox()
        self.level_filter.addItems(["全部级别", "DEBUG", "INFO", "WARNING", "ERROR"])
        self.category_filter = QComboBox()
        self.category_filter.addItem("全部类别")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索脱敏日志")
        self.pause_refresh = QCheckBox("暂停刷新")
        self.auto_scroll = QCheckBox("自动滚动")
        self.auto_scroll.setChecked(True)
        filters.addWidget(self.level_filter)
        filters.addWidget(self.category_filter)
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.pause_refresh)
        filters.addWidget(self.auto_scroll)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["时间", "级别", "类别", "消息"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        _configure_resizable_header(self.table, (145, 85, 120, 420))
        self.table.setAccessibleName("运行与上报日志")
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        copy_button = QPushButton("复制所选")
        copy_button.clicked.connect(self._copy_selected)
        clear_button = QPushButton("清空内存日志")
        clear_button.clicked.connect(self._clear)
        open_button = QPushButton("打开日志目录")
        open_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs.log_directory)))
        )
        actions.addWidget(copy_button)
        actions.addWidget(clear_button)
        actions.addWidget(open_button)
        if service is not None:
            logging_settings = service.store.load_logging_settings()
            self.file_logging = QCheckBox("写入按日文件日志")
            self.file_logging.setChecked(logging_settings.file_enabled)
            self.vrchat_debug_logging = QCheckBox("记录 VRChat 调试日志")
            self.vrchat_debug_logging.setToolTip(
                "默认关闭。仅排查问题时开启；高频 Activity 不会逐条显示。"
            )
            self.vrchat_debug_logging.setChecked(
                logging_settings.vrchat_debug_enabled
            )
            self.file_logging.toggled.connect(self._save_logging_settings)
            self.vrchat_debug_logging.toggled.connect(self._save_logging_settings)
            actions.addWidget(self.file_logging)
            actions.addWidget(self.vrchat_debug_logging)
        actions.addStretch()
        layout.addLayout(actions)
        self.level_filter.currentIndexChanged.connect(self.refresh)
        self.category_filter.currentIndexChanged.connect(self.refresh)
        self.search_edit.textChanged.connect(self.refresh)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        if self.pause_refresh.isChecked() or not self.isVisible():
            return
        entries = self._logs.entries()
        categories = sorted({entry.category for entry in entries})
        current_category = self.category_filter.currentText()
        existing_categories = [
            self.category_filter.itemText(i)
            for i in range(1, self.category_filter.count())
        ]
        if existing_categories != categories:
            self.category_filter.blockSignals(True)
            self.category_filter.clear()
            self.category_filter.addItem("全部类别")
            self.category_filter.addItems(categories)
            index = self.category_filter.findText(current_category)
            self.category_filter.setCurrentIndex(max(0, index))
            self.category_filter.blockSignals(False)
        level = self.level_filter.currentText()
        category = self.category_filter.currentText()
        needle = self.search_edit.text().strip().casefold()
        signature = (
            entries[-1].sequence if entries else 0,
            len(entries),
            level,
            category,
            needle,
        )
        if signature == self._render_signature:
            return
        self._render_signature = signature
        filtered = [
            entry
            for entry in entries
            if (level == "全部级别" or entry.level == level)
            and (category == "全部类别" or entry.category == category)
            and (
                not needle
                or needle in entry.message.casefold()
                or needle in entry.category.casefold()
            )
        ]
        self.table.setRowCount(len(filtered))
        for row, entry in enumerate(filtered):
            values = (
                entry.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
                entry.level,
                entry.category,
                entry.message,
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        if self.auto_scroll.isChecked() and filtered:
            self.table.scrollToBottom()

    def _copy_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        text = "\n".join(
            "\t".join(_table_text(self.table, row, column) for column in range(4))
            for row in rows
        )
        QApplication.clipboard().setText(text)

    def _clear(self) -> None:
        self._logs.clear()
        self._render_signature = None
        self.refresh()

    def _save_logging_settings(self, _enabled: bool) -> None:
        if self._service is not None:
            asyncio.create_task(
                self._service.save_logging_settings(
                    LoggingSettings(
                        self.file_logging.isChecked(),
                        self.vrchat_debug_logging.isChecked(),
                    )
                )
            )


class LogWindow(QDialog):
    def __init__(self, logs: ProcessLogService, service: ApplicationService) -> None:
        super().__init__()
        self.setWindowTitle("Yohaku Companion 日志")
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(LogPanel(logs, service, self))

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()


class SettingsWindow(QMainWindow):
    def __init__(
        self,
        service: ApplicationService,
        startup: StartupManager,
        icon: QIcon,
    ) -> None:
        super().__init__()
        self.service = service
        self.startup = startup
        self._quitting = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._sensitive_rules = list(self.service.store.load_sensitive_rules())
        self._log_window = (
            LogWindow(service.logs, service) if service.logs is not None else None
        )
        self._vr_loaded_settings: VRChatIntegrationSettings | None = None
        self.setWindowTitle("Yohaku Companion Windows")
        self.setWindowIcon(icon)
        self.resize(820, 650)
        self.setMinimumSize(680, 520)
        self._stack = QStackedWidget()
        self._pairing_page = self._make_pairing_page()
        self._paired_page = self._make_paired_page()
        self._stack.addWidget(self._pairing_page)
        self._stack.addWidget(self._paired_page)
        self.setCentralWidget(self._stack)
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(1000)
        self._preview_timer.timeout.connect(self._refresh_preview_display)
        self._preview_timer.start()
        service.subscribe(self.render_state)

    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def begin_quit(self) -> None:
        self._quitting = True
        if self._log_window is not None:
            self._log_window.close()

    def show_logs(self) -> None:
        if self._log_window is not None:
            self._log_window.show_and_raise()

    def create_task(
        self, operation: Coroutine[Any, Any, None]
    ) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(operation)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            event.accept()
        else:
            event.ignore()
            self.hide()

    def render_state(self, state: ServiceViewState) -> None:
        self._stack.setCurrentWidget(
            self._pairing_page if state.connection is None else self._paired_page
        )
        if state.connection is None:
            self._pairing_notice.setText(state.notice or "")
            self._pair_button.setEnabled(not state.busy)
            return
        enabled = state.connection.live_desk_enabled
        self._preview_group.setTitle(
            "最近成功发布的净化快照"
            if enabled
            else "待确认的净化预览（开启前请核对）"
        )
        self._connection_label.setText(
            f"设备：{state.connection.device_id}\n服务器：{state.connection.base_url}"
        )
        glyph, color, accessible = STATUS_VISUALS[state.runtime_state]
        self._runtime_icon.setPixmap(status_icon(state.runtime_state).pixmap(26, 26))
        self._runtime_icon.setAccessibleName(accessible)
        self._runtime_label.setText(f"状态：{_STATE_NAMES[state.runtime_state]}")
        self._runtime_label.setStyleSheet(
            f"font-size: 18px; font-weight: 600; color: {color}"
        )
        self._runtime_badge.setAccessibleName(f"状态：{accessible}，{glyph}")
        self._notice_label.setText(state.notice or "")
        self._refresh_preview_display()
        self._live_button.setText("关闭 Live Desk" if enabled else "开启 Live Desk")
        self._live_button.setEnabled(not state.busy and (enabled or state.preview_current))
        self._pause_button.setText("恢复" if state.paused else "暂停")
        self._pause_button.setEnabled(not state.busy and enabled)
        self._refresh_button.setEnabled(not state.busy)
        self._sync_privacy_table(state)
        self._sync_vrchat_state(state)

    def _refresh_preview_display(self) -> None:
        if hasattr(self, "_preview"):
            self._preview.setPlainText(_preview_text(self.service.state, datetime.now(UTC)))

    def _make_pairing_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("连接 Yohaku Live Desk")
        title.setStyleSheet("font-size: 22px; font-weight: 600")
        explanation = QLabel(
            "配对不会立即公开任何信息。成功后仍需检查净化预览并明确开启 Live Desk。"
        )
        explanation.setWordWrap(True)
        self._pairing_notice = QLabel()
        self._pairing_notice.setWordWrap(True)
        form = QFormLayout()
        self._server_edit = QLineEdit("https://")
        self._server_edit.setAccessibleName("服务器地址")
        self._code_edit = QLineEdit()
        self._code_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._code_edit.setAccessibleName("一次性配对码")
        self._device_edit = QLineEdit("Windows 电脑")
        form.addRow("服务器地址", self._server_edit)
        transport_notice = QLabel(
            "局域网服务器可使用 HTTP，但配对码和设备令牌会以明文在局域网传输；"
            "HTTP 主机必须只解析到私有或链路本地 IP。"
        )
        transport_notice.setWordWrap(True)
        form.addRow("", transport_notice)
        form.addRow("一次性配对码", self._code_edit)
        form.addRow("设备名称", self._device_edit)
        self._pair_button = QPushButton("验证并配对")
        self._pair_button.setDefault(True)
        self._pair_button.clicked.connect(
            lambda: self._run(
                lambda: self.service.pair(
                    self._server_edit.text(),
                    self._code_edit.text(),
                    self._device_edit.text(),
                ),
                on_success=lambda: self._code_edit.clear(),
            )
        )
        log_button = QPushButton("查看运行日志")
        log_button.setEnabled(self._log_window is not None)
        log_button.clicked.connect(self.show_logs)
        layout.addStretch()
        layout.addWidget(title)
        layout.addWidget(explanation)
        layout.addWidget(self._pairing_notice)
        layout.addLayout(form)
        layout.addWidget(self._pair_button)
        layout.addWidget(log_button)
        layout.addStretch(2)
        return page

    def _make_paired_page(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._make_overview_tab(), "Live Desk")
        tabs.addTab(self._make_privacy_tab(), "隐私")
        tabs.addTab(self._make_vrchat_tab(), "VRChat 集成")
        if self.service.logs is not None:
            tabs.addTab(LogPanel(self.service.logs, self.service), "日志")
        tabs.addTab(self._make_general_tab(), "常规")
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(tabs)
        return container

    def _make_overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._connection_label = QLabel()
        self._connection_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._runtime_badge = QWidget()
        runtime_layout = QHBoxLayout(self._runtime_badge)
        runtime_layout.setContentsMargins(0, 0, 0, 0)
        self._runtime_icon = QLabel()
        self._runtime_icon.setFixedSize(28, 28)
        self._runtime_label = QLabel()
        runtime_layout.addWidget(self._runtime_icon)
        runtime_layout.addWidget(self._runtime_label)
        runtime_layout.addStretch()
        self._notice_label = QLabel()
        self._notice_label.setWordWrap(True)
        self._preview_group = QGroupBox("待确认的净化预览（开启前请核对）")
        self._preview_group.setToolTip(
            "开启后，前台应用和媒体变化会按当前隐私规则重新净化并发布；"
            "这里将显示最近成功发布的快照。"
        )
        preview_layout = QVBoxLayout(self._preview_group)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setAccessibleName("净化预览")
        preview_layout.addWidget(self._preview)
        button_row = QHBoxLayout()
        self._refresh_button = QPushButton("重新采集预览")
        self._live_button = QPushButton("开启 Live Desk")
        self._pause_button = QPushButton("暂停")
        self._refresh_button.clicked.connect(
            lambda: self._run(self.service.refresh_preview)
        )
        self._live_button.clicked.connect(self._toggle_live)
        self._pause_button.clicked.connect(self._toggle_pause)
        button_row.addWidget(self._refresh_button)
        button_row.addStretch()
        button_row.addWidget(self._pause_button)
        button_row.addWidget(self._live_button)
        layout.addWidget(self._runtime_badge)
        layout.addWidget(self._connection_label)
        layout.addWidget(self._notice_label)
        layout.addWidget(self._preview_group, 1)
        layout.addLayout(button_row)
        return page

    def _make_privacy_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        global_group = QGroupBox("采集来源与默认规则")
        grid = QFormLayout(global_group)
        self._source_apps = QCheckBox("采集前台应用")
        self._source_titles = QCheckBox("允许采集窗口标题")
        self._source_media = QCheckBox("采集系统媒体")
        self._default_apps = QCheckBox("默认分享应用")
        self._default_titles = QCheckBox("默认分享窗口标题")
        self._default_media = QCheckBox("默认分享媒体")
        grid.addRow(self._source_apps, self._default_apps)
        grid.addRow(self._source_titles, self._default_titles)
        grid.addRow(self._source_media, self._default_media)
        self._rules = QTableWidget(0, 7)
        self._rules.setHorizontalHeaderLabels(
            ["应用", "标识", "应用", "标题", "媒体", "显示别名", "自定义标题"]
        )
        self._rules.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        _configure_resizable_header(
            self._rules,
            (130, 190, 80, 80, 80, 140, 220),
        )
        custom_title_header = self._rules.horizontalHeaderItem(6)
        if custom_title_header is not None:
            custom_title_header.setToolTip(
                "标题分享被允许时，优先使用此固定标题；留空则自动采集"
            )
        app_rules_page = QWidget()
        app_rules_layout = QVBoxLayout(app_rules_page)
        app_rules_layout.addWidget(QLabel("应用规则（隐藏优先于显示别名）"))
        app_rules_layout.addWidget(self._rules)

        sensitive_page = QWidget()
        sensitive_layout = QVBoxLayout(sensitive_page)
        self._sensitive_table = QTableWidget(0, 5)
        self._sensitive_table.setHorizontalHeaderLabels(
            ["启用", "名称", "表达式", "动作", "字段范围"]
        )
        self._sensitive_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._sensitive_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        _configure_resizable_header(
            self._sensitive_table,
            (65, 130, 280, 110, 190),
        )
        self._sensitive_table.doubleClicked.connect(lambda _index: self._edit_sensitive_rule())
        sensitive_buttons = QHBoxLayout()
        for label, callback in (
            ("添加", self._add_sensitive_rule),
            ("编辑", self._edit_sensitive_rule),
            ("删除", self._delete_sensitive_rule),
            ("上移", lambda: self._move_sensitive_rule(-1)),
            ("下移", lambda: self._move_sensitive_rule(1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            sensitive_buttons.addWidget(button)
        sensitive_buttons.addStretch()
        sensitive_layout.addWidget(
            QLabel("规则按顺序执行；超时会隐藏字段，表达式和命中内容绝不会上传。")
        )
        sensitive_layout.addWidget(self._sensitive_table)
        sensitive_layout.addLayout(sensitive_buttons)
        self._render_sensitive_rules()

        privacy_pages = QTabWidget()
        privacy_pages.addTab(app_rules_page, "应用规则")
        privacy_pages.addTab(sensitive_page, "敏感词规则")
        save = QPushButton("保存隐私设置")
        save.clicked.connect(lambda: self._run(self._save_privacy))
        layout.addWidget(global_group)
        layout.addWidget(privacy_pages, 1)
        layout.addWidget(save)
        self._load_global_privacy()
        return page

    def _make_general_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._startup = QCheckBox("登录 Windows 后在后台启动")
        self._startup.setEnabled(self.startup.available)
        self._startup.setChecked(self.startup.is_enabled())
        if not self.startup.available:
            self._startup.setToolTip("仅打包后的 Release EXE 支持开机启动")
        self._startup.toggled.connect(
            lambda checked: self._run(lambda: self._set_startup(checked))
        )
        remove = QPushButton("移除当前设备连接")
        remove.clicked.connect(self._confirm_remove)
        layout.addWidget(self._startup)
        layout.addStretch()
        layout.addWidget(remove)
        return page

    def _make_vrchat_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        explanation = QLabel(
            "从本机 Discord RPC 命名管道接收 VRChat/VRCX Activity。"
            "世界名称仍需通过窗口标题开关、应用规则和敏感词规则。"
        )
        explanation.setWordWrap(True)
        self._vr_enabled = QCheckBox("启用 VRChat 集成")
        self._vr_replace_title = QCheckBox("前台为 VRChat 时用世界名称替换窗口标题")
        self._vr_upload = QCheckBox("上传净化后的 VRC 状态")
        self._vr_endpoint = QLineEdit()
        self._vr_endpoint.setPlaceholderText("https://example.com/api/vrc/activity")
        self._vr_endpoint.setAccessibleName("VRC API POST 端点")
        self._vr_api_key = QLineEdit()
        self._vr_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._vr_api_key.setAccessibleName("VRC API 密匙")
        self._vr_api_key.setPlaceholderText("未设置")
        form = QFormLayout()
        form.addRow("", self._vr_enabled)
        form.addRow("", self._vr_replace_title)
        form.addRow("", self._vr_upload)
        form.addRow("完整 POST 端点", self._vr_endpoint)
        form.addRow("API 密匙", self._vr_api_key)
        self._vr_status = QLabel("未启用")
        self._vr_status.setWordWrap(True)
        form.addRow("运行状态", self._vr_status)
        button_row = QHBoxLayout()
        save = QPushButton("保存 VRChat 设置")
        save.clicked.connect(lambda: self._run(self._save_vrchat))
        clear_key = QPushButton("清除已保存密匙")
        clear_key.clicked.connect(self._confirm_clear_vrchat_key)
        button_row.addWidget(save)
        button_row.addWidget(clear_key)
        button_row.addStretch()
        warning = QLabel(
            "密匙仅保存到 Windows 凭据保险箱；留空保存表示保留现有密匙。"
            "修改设置会关闭正在公开的 Live Desk，并要求重新确认预览。"
        )
        warning.setWordWrap(True)
        layout.addWidget(explanation)
        layout.addLayout(form)
        layout.addWidget(warning)
        layout.addLayout(button_row)
        layout.addStretch()
        self._vr_enabled.toggled.connect(self._sync_vrchat_controls)
        self._vr_upload.toggled.connect(self._sync_vrchat_controls)
        return page

    def _sync_vrchat_controls(self) -> None:
        enabled = self._vr_enabled.isChecked()
        upload = enabled and self._vr_upload.isChecked()
        self._vr_replace_title.setEnabled(enabled)
        self._vr_upload.setEnabled(enabled)
        self._vr_endpoint.setEnabled(upload)
        self._vr_api_key.setEnabled(upload)

    def _sync_vrchat_state(self, state: ServiceViewState) -> None:
        settings = state.vrchat_settings
        if settings != self._vr_loaded_settings:
            self._vr_loaded_settings = settings
            self._vr_enabled.setChecked(settings.enabled)
            self._vr_replace_title.setChecked(settings.replace_world_title)
            self._vr_upload.setChecked(settings.upload_activity)
            self._vr_endpoint.setText(settings.endpoint_url)
            self._vr_api_key.clear()
        self._vr_api_key.setPlaceholderText(
            "已保存（留空表示保留）"
            if state.vrchat_api_key_present
            else "尚未保存密匙"
        )
        self._vr_status.setText(state.vrchat_status)
        self._sync_vrchat_controls()

    async def _save_vrchat(self) -> None:
        settings = VRChatIntegrationSettings(
            enabled=self._vr_enabled.isChecked(),
            replace_world_title=self._vr_replace_title.isChecked(),
            upload_activity=self._vr_upload.isChecked(),
            endpoint_url=self._vr_endpoint.text(),
        )
        await self.service.save_vrchat_settings(settings, self._vr_api_key.text())
        self._vr_api_key.clear()

    def _confirm_clear_vrchat_key(self) -> None:
        answer = QMessageBox.question(
            self,
            "清除 VRC API 密匙",
            "确定从 Windows 凭据保险箱删除 VRC API 密匙吗？",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(self.service.clear_vrchat_api_key)

    def _load_global_privacy(self) -> None:
        sources = self.service.store.load_sources()
        defaults = self.service.store.load_privacy_defaults()
        self._source_apps.setChecked(sources.applications)
        self._source_titles.setChecked(sources.window_titles)
        self._source_media.setChecked(sources.media)
        self._default_apps.setChecked(defaults.application)
        self._default_titles.setChecked(defaults.window_title)
        self._default_media.setChecked(defaults.media)

    def _sync_privacy_table(self, state: ServiceViewState) -> None:
        rules = {rule.identifier: rule for rule in self.service.store.load_rules()}
        candidates = {item.identifier: item.display_name for item in state.rule_candidates}
        for identifier, rule in rules.items():
            candidates.setdefault(identifier, rule.display_name)
        if self._rules.rowCount() == len(candidates) and {
            _table_text(self._rules, row, 1) for row in range(self._rules.rowCount())
        } == set(candidates):
            return
        self._rules.setRowCount(0)
        ordered_candidates = sorted(
            candidates.items(), key=lambda item: item[1].casefold()
        )
        for identifier, display_name in ordered_candidates:
            rule = rules.get(identifier, ApplicationRule(identifier, display_name))
            row = self._rules.rowCount()
            self._rules.insertRow(row)
            self._rules.setItem(row, 0, QTableWidgetItem(display_name))
            identifier_item = QTableWidgetItem(identifier)
            identifier_item.setFlags(identifier_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._rules.setItem(row, 1, identifier_item)
            for column, mode in (
                (2, rule.application), (3, rule.window_title), (4, rule.media)
            ):
                combo = _mode_combo(mode)
                self._rules.setCellWidget(row, column, combo)
            self._rules.setItem(row, 5, QTableWidgetItem(rule.alias or ""))
            self._rules.setItem(row, 6, QTableWidgetItem(rule.custom_title or ""))

    def _render_sensitive_rules(self, selected_row: int | None = None) -> None:
        self._sensitive_table.setRowCount(0)
        for row, rule in enumerate(self._sensitive_rules):
            self._sensitive_table.insertRow(row)
            enabled = QTableWidgetItem()
            enabled.setCheckState(
                Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked
            )
            enabled.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            enabled.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                "已启用" if rule.enabled else "已禁用",
            )
            self._sensitive_table.setItem(row, 0, enabled)
            self._sensitive_table.setItem(row, 1, QTableWidgetItem(rule.name))
            self._sensitive_table.setItem(row, 2, QTableWidgetItem(rule.pattern))
            self._sensitive_table.setItem(
                row, 3, QTableWidgetItem(ACTION_NAMES[rule.action])
            )
            field_names = "、".join(FIELD_NAMES[field] for field in rule.fields)
            self._sensitive_table.setItem(row, 4, QTableWidgetItem(field_names))
        if selected_row is not None and self._sensitive_rules:
            self._sensitive_table.selectRow(
                min(max(selected_row, 0), len(self._sensitive_rules) - 1)
            )

    def _selected_sensitive_row(self) -> int | None:
        rows = self._sensitive_table.selectionModel().selectedRows()
        return None if not rows else rows[0].row()

    def _sync_sensitive_enabled_states(self) -> None:
        updated: list[SensitiveTextRule] = []
        for row, rule in enumerate(self._sensitive_rules):
            item = self._sensitive_table.item(row, 0)
            enabled = bool(item and item.checkState() == Qt.CheckState.Checked)
            updated.append(
                SensitiveTextRule(
                    identifier=rule.identifier,
                    name=rule.name,
                    pattern=rule.pattern,
                    fields=rule.fields,
                    action=rule.action,
                    enabled=enabled,
                    ignore_case=rule.ignore_case,
                    sort_order=row,
                    pattern_modules=rule.pattern_modules,
                )
            )
        self._sensitive_rules = updated

    def _add_sensitive_rule(self) -> None:
        if len(self._sensitive_rules) >= 50:
            QMessageBox.warning(self, "无法添加", "最多只能保存 50 条敏感词规则")
            return
        editor = SensitiveRuleEditor(self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            self._sensitive_rules.append(editor.rule(len(self._sensitive_rules)))
            self._render_sensitive_rules(len(self._sensitive_rules) - 1)

    def _edit_sensitive_rule(self) -> None:
        row = self._selected_sensitive_row()
        if row is None:
            return
        self._sync_sensitive_enabled_states()
        editor = SensitiveRuleEditor(self, self._sensitive_rules[row])
        if editor.exec() == QDialog.DialogCode.Accepted:
            self._sensitive_rules[row] = editor.rule(row)
            self._render_sensitive_rules(row)

    def _delete_sensitive_rule(self) -> None:
        row = self._selected_sensitive_row()
        if row is None:
            return
        del self._sensitive_rules[row]
        self._renumber_sensitive_rules()
        self._render_sensitive_rules(row)

    def _move_sensitive_rule(self, offset: int) -> None:
        row = self._selected_sensitive_row()
        if row is None:
            return
        target = row + offset
        if not 0 <= target < len(self._sensitive_rules):
            return
        self._sync_sensitive_enabled_states()
        self._sensitive_rules[row], self._sensitive_rules[target] = (
            self._sensitive_rules[target],
            self._sensitive_rules[row],
        )
        self._renumber_sensitive_rules()
        self._render_sensitive_rules(target)

    def _renumber_sensitive_rules(self) -> None:
        self._sensitive_rules = [
            SensitiveTextRule(
                identifier=rule.identifier,
                name=rule.name,
                pattern=rule.pattern,
                fields=rule.fields,
                action=SensitiveAction(rule.action),
                enabled=rule.enabled,
                ignore_case=rule.ignore_case,
                sort_order=index,
                pattern_modules=rule.pattern_modules,
            )
            for index, rule in enumerate(self._sensitive_rules)
        ]

    async def _save_privacy(self) -> None:
        self._sync_sensitive_enabled_states()
        rules: list[ApplicationRule] = []
        for row in range(self._rules.rowCount()):
            identifier = _table_text(self._rules, row, 1)
            rules.append(
                ApplicationRule(
                    identifier=identifier,
                    display_name=_table_text(self._rules, row, 0),
                    application=_combo_mode(self._rules.cellWidget(row, 2)),
                    window_title=_combo_mode(self._rules.cellWidget(row, 3)),
                    media=_combo_mode(self._rules.cellWidget(row, 4)),
                    alias=_table_text(self._rules, row, 5),
                    custom_title=_table_text(self._rules, row, 6),
                )
            )
        await self.service.save_privacy(
            SourceSettings(
                self._source_apps.isChecked(),
                self._source_titles.isChecked(),
                self._source_media.isChecked(),
            ),
            PrivacyDefaults(
                self._default_apps.isChecked(),
                self._default_titles.isChecked(),
                self._default_media.isChecked(),
            ),
            tuple(rules),
            tuple(self._sensitive_rules),
        )
        self._sensitive_rules = list(self.service.store.load_sensitive_rules())
        self._render_sensitive_rules()

    async def _set_startup(self, enabled: bool) -> None:
        async with self.service.mutation_lock:
            self.startup.set_enabled(enabled)

    def _toggle_live(self) -> None:
        connection = self.service.state.connection
        if connection and connection.live_desk_enabled:
            self._run(self.service.disable_live_desk)
        else:
            self._run(self.service.enable_live_desk)

    def _toggle_pause(self) -> None:
        self._run(self.service.resume if self.service.state.paused else self.service.pause)

    def _confirm_remove(self) -> None:
        answer = QMessageBox.question(
            self,
            "移除设备连接",
            "这会清除公开 Presence、Windows 凭据中的设备令牌和本地连接信息。继续吗？",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(self.service.remove_device)

    def _run(
        self,
        operation: Callable[[], Coroutine[Any, Any, None]],
        on_success: Callable[[], None] | None = None,
    ) -> None:
        async def execute() -> None:
            try:
                await operation()
                if on_success is not None:
                    on_success()
            except Exception as error:
                QMessageBox.warning(self, "操作未完成", str(error))

        self.create_task(execute())


class TrayController:
    def __init__(
        self,
        app: QApplication,
        service: ApplicationService,
        window: SettingsWindow,
        icon: QIcon,
        on_quit: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        self._service = service
        self._window = window
        self._on_quit = on_quit
        self.tray = QSystemTrayIcon(icon, app)
        self.tray.setToolTip("Yohaku Companion")
        menu = QMenu()
        open_action = QAction("打开设置", menu)
        logs_action = QAction("查看日志", menu)
        self._live_action = QAction("开启 Live Desk", menu)
        self._pause_action = QAction("暂停", menu)
        quit_action = QAction("退出", menu)
        open_action.triggered.connect(window.show_and_raise)
        logs_action.triggered.connect(window.show_logs)
        logs_action.setEnabled(window._log_window is not None)
        self._live_action.triggered.connect(self._toggle_live)
        self._pause_action.triggered.connect(self._toggle_pause)
        quit_action.triggered.connect(lambda: asyncio.create_task(on_quit()))
        menu.addAction(open_action)
        menu.addAction(logs_action)
        menu.addSeparator()
        menu.addAction(self._live_action)
        menu.addAction(self._pause_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: window.show_and_raise()
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
        service.subscribe(self._render)

    def show(self) -> None:
        self.tray.show()

    def hide(self) -> None:
        self.tray.hide()

    def _render(self, state: ServiceViewState) -> None:
        enabled = bool(state.connection and state.connection.live_desk_enabled)
        self._live_action.setText("关闭 Live Desk" if enabled else "开启 Live Desk")
        self._live_action.setEnabled(state.connection is not None)
        self._pause_action.setText("恢复" if state.paused else "暂停")
        self._pause_action.setEnabled(enabled)
        self.tray.setToolTip(f"Yohaku Companion · {_STATE_NAMES[state.runtime_state]}")

    def _toggle_live(self) -> None:
        connection = self._service.state.connection
        operation = (
            self._service.disable_live_desk
            if connection and connection.live_desk_enabled
            else self._service.enable_live_desk
        )
        self._execute_tray_action(operation)

    def _toggle_pause(self) -> None:
        operation = self._service.resume if self._service.state.paused else self._service.pause
        self._execute_tray_action(operation)

    def _execute_tray_action(
        self, operation: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        async def execute() -> None:
            try:
                await operation()
            except Exception as error:
                self._window.show_and_raise()
                QMessageBox.warning(self._window, "操作未完成", str(error))

        self._window.create_task(execute())


def _configure_resizable_header(
    table: QTableWidget,
    widths: tuple[int, ...],
) -> None:
    """Keep every divider user-draggable while providing useful initial widths."""
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setMinimumSectionSize(45)
    header.setStretchLastSection(False)
    for column, width in enumerate(widths):
        table.setColumnWidth(column, width)


def _mode_combo(mode: ShareMode) -> QComboBox:
    combo = QComboBox()
    choices = (
        ("继承", ShareMode.INHERIT),
        ("分享", ShareMode.SHARE),
        ("隐藏", ShareMode.HIDE),
    )
    for name, value in choices:
        combo.addItem(mode_icon(value), name, value.value)
        index = combo.count() - 1
        _, color, accessible = MODE_VISUALS[value]
        combo.setItemData(index, QColor(color), Qt.ItemDataRole.ForegroundRole)
        combo.setItemData(index, accessible, Qt.ItemDataRole.AccessibleDescriptionRole)
    combo.setCurrentIndex(combo.findData(mode.value))
    combo.setAccessibleName("分享规则")
    combo.setAccessibleDescription(MODE_VISUALS[mode][2])
    combo.currentIndexChanged.connect(
        lambda _index: combo.setAccessibleDescription(
            MODE_VISUALS[ShareMode(str(combo.currentData()))][2]
        )
    )
    return combo


def _combo_mode(widget: QWidget | None) -> ShareMode:
    if not isinstance(widget, QComboBox):
        return ShareMode.INHERIT
    return ShareMode(str(widget.currentData()))


def _table_text(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    return "" if item is None else item.text()


def _preview_text(state: ServiceViewState, now: datetime | None = None) -> str:
    snapshot = state.preview
    if snapshot is None:
        return "尚未采集。开启前必须先重新采集并检查预览。"
    lines = [f"可见性：{'在线' if snapshot.availability.value == 'active' else '空闲'}"]
    if snapshot.application is None:
        lines.append("应用：不分享")
    else:
        lines.append(f"应用：{snapshot.application.display_name}")
        lines.append(f"窗口标题：{snapshot.application.window_title or '不分享'}")
    if snapshot.media is None:
        lines.append("媒体：不分享")
    else:
        media = snapshot.media
        playback = media.playback
        position = playback.position_seconds
        if (
            position is not None
            and playback.state.value == "playing"
            and playback.rate > 0
        ):
            current = (now or datetime.now(UTC)).astimezone(UTC)
            position += max(0.0, (current - playback.sampled_at).total_seconds()) * playback.rate
        if position is not None and playback.duration_seconds is not None:
            position = min(position, playback.duration_seconds)
        state_icon = "▶" if playback.state.value == "playing" else "⏸"
        timeline = (
            f"{_format_duration(position)} / {_format_duration(playback.duration_seconds)}"
            if playback.duration_seconds is not None
            else f"{_format_duration(position)} / 未知时长"
        )
        lines.extend(
            (
                f"媒体：{media.title or '（无标题）'}",
                f"艺术家：{media.artist or '不分享/无数据'}",
                f"专辑：{media.album or '不分享/无数据'}",
                f"播放器：{media.player_display_name or '不分享/无数据'}",
                "播放状态："
                f"{state_icon} {'播放中' if playback.state.value == 'playing' else '已暂停'}",
                f"播放进度：{timeline}",
            )
        )
    return "\n".join(lines)


def _format_duration(value: float | None) -> str:
    if value is None:
        return "--:--"
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"{hours}:{minutes:02d}:{seconds:02d}"
        if hours
        else f"{minutes}:{seconds:02d}"
    )
