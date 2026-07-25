from __future__ import annotations

import unicodedata
from uuid import uuid4

import regex
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .domain import (
    SensitiveAction,
    SensitiveField,
    SensitivePatternKind,
    SensitivePatternModule,
    SensitiveTextRule,
)

FIELD_NAMES: dict[SensitiveField, str] = {
    SensitiveField.APPLICATION_NAME: "应用名",
    SensitiveField.WINDOW_TITLE: "窗口标题",
    SensitiveField.MEDIA_TITLE: "媒体标题",
    SensitiveField.MEDIA_ARTIST: "艺术家",
    SensitiveField.MEDIA_ALBUM: "专辑",
    SensitiveField.PLAYER_NAME: "播放器名",
}

ACTION_NAMES: dict[SensitiveAction, str] = {
    SensitiveAction.MASK_MATCH: "替换命中",
    SensitiveAction.HIDE_FIELD: "隐藏字段",
    SensitiveAction.HIDE_CONTEXT: "隐藏上下文",
}

MODULE_NAMES: dict[SensitivePatternKind, str] = {
    SensitivePatternKind.CONTAINS: "包含文字",
    SensitivePatternKind.EXACT: "完整等于",
    SensitivePatternKind.PREFIX: "以文字开头",
    SensitivePatternKind.SUFFIX: "以文字结尾",
    SensitivePatternKind.ANY_WORD: "任一关键词",
    SensitivePatternKind.NUMBER: "数字序列",
    SensitivePatternKind.EMAIL: "电子邮箱",
    SensitivePatternKind.URL: "网页地址",
}


class SensitiveRuleEditor(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        rule: SensitiveTextRule | None = None,
    ) -> None:
        super().__init__(parent)
        self._identifier = rule.identifier if rule is not None else str(uuid4())
        self._sort_order = rule.sort_order if rule is not None else 0
        self._modules = list(rule.pattern_modules if rule is not None else ())
        self.setWindowTitle("编辑敏感词规则" if rule is not None else "添加敏感词规则")
        self.resize(720, 680)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(rule.name if rule else "")
        self.name_edit.setAccessibleName("规则名称")
        self.pattern_edit = QLineEdit(rule.pattern if rule else "")
        self.pattern_edit.setMaxLength(256)
        self.pattern_edit.setAccessibleName("正则表达式")
        self.action_combo = QComboBox()
        for action, name in ACTION_NAMES.items():
            self.action_combo.addItem(name, action.value)
        selected_action = rule.action if rule else SensitiveAction.HIDE_FIELD
        self.action_combo.setCurrentIndex(self.action_combo.findData(selected_action.value))
        self.ignore_case = QCheckBox("忽略大小写（Unicode）")
        self.ignore_case.setChecked(True if rule is None else rule.ignore_case)
        self.enabled = QCheckBox("启用规则")
        self.enabled.setChecked(True if rule is None else rule.enabled)
        form.addRow("名称", self.name_edit)
        form.addRow("动作", self.action_combo)
        form.addRow("", self.ignore_case)
        form.addRow("", self.enabled)
        layout.addLayout(form)

        expression_tabs = QTabWidget()
        graphical_page = QWidget()
        graphical_layout = QVBoxLayout(graphical_page)
        module_row = QHBoxLayout()
        self.module_kind = QComboBox()
        for kind, name in MODULE_NAMES.items():
            self.module_kind.addItem(name, kind.value)
        self.module_kind.currentIndexChanged.connect(self._module_kind_changed)
        self.module_value = QLineEdit()
        self.module_value.setPlaceholderText("文字；多个关键词可用逗号分隔")
        self.module_add = QPushButton("添加模块")
        self.module_add.clicked.connect(self._add_module)
        module_row.addWidget(self.module_kind)
        module_row.addWidget(self.module_value, 1)
        module_row.addWidget(self.module_add)
        self.module_list = QListWidget()
        self.module_list.setAccessibleName("表达式模块列表")
        module_buttons = QHBoxLayout()
        remove_module = QPushButton("删除所选模块")
        remove_module.clicked.connect(self._remove_module)
        clear_modules = QPushButton("清空模块")
        clear_modules.clicked.connect(self._clear_modules)
        module_buttons.addWidget(remove_module)
        module_buttons.addWidget(clear_modules)
        module_buttons.addStretch()
        graphical_layout.addWidget(
            QLabel("添加一个或多个模块；任一模块命中即执行规则。")
        )
        graphical_layout.addLayout(module_row)
        graphical_layout.addWidget(self.module_list)
        graphical_layout.addLayout(module_buttons)

        advanced_page = QWidget()
        advanced_form = QFormLayout(advanced_page)
        advanced_form.addRow("正则表达式", self.pattern_edit)
        advanced_help = QLabel("高级模式支持完整正则语法；修改后会清除图形模块。")
        advanced_help.setWordWrap(True)
        advanced_form.addRow("", advanced_help)
        expression_tabs.addTab(graphical_page, "图形化构建")
        expression_tabs.addTab(advanced_page, "高级正则")
        if rule is not None and not self._modules:
            expression_tabs.setCurrentWidget(advanced_page)
        layout.addWidget(expression_tabs)
        self._render_modules()
        self._module_kind_changed()

        fields_group = QGroupBox("字段范围（至少选择一项）")
        fields_layout = QHBoxLayout(fields_group)
        selected_fields = set(rule.fields if rule else ())
        self.field_checks: dict[SensitiveField, QCheckBox] = {}
        for field, name in FIELD_NAMES.items():
            checkbox = QCheckBox(name)
            checkbox.setChecked(field in selected_fields)
            checkbox.setAccessibleName(f"字段：{name}")
            checkbox.toggled.connect(self._validate)
            self.field_checks[field] = checkbox
            fields_layout.addWidget(checkbox)
        layout.addWidget(fields_group)

        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        layout.addWidget(self.validation_label)
        layout.addWidget(QLabel("测试文本（仅在本机验证，不会保存）"))
        self.test_edit = QPlainTextEdit()
        self.test_edit.setMaximumHeight(90)
        self.test_edit.setAccessibleName("正则测试文本")
        layout.addWidget(self.test_edit)
        layout.addWidget(QLabel("命中结果"))
        self.test_result = QPlainTextEdit()
        self.test_result.setReadOnly(True)
        self.test_result.setMaximumHeight(90)
        self.test_result.setAccessibleName("正则命中结果")
        layout.addWidget(self.test_result)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.name_edit.textChanged.connect(self._validate)
        self.pattern_edit.textChanged.connect(self._validate)
        self.pattern_edit.textEdited.connect(self._advanced_pattern_edited)
        self.ignore_case.toggled.connect(self._validate)
        self.test_edit.textChanged.connect(self._validate)
        self._validate()

    def selected_fields(self) -> tuple[SensitiveField, ...]:
        return tuple(field for field, box in self.field_checks.items() if box.isChecked())

    def rule(self, sort_order: int | None = None) -> SensitiveTextRule:
        return SensitiveTextRule(
            identifier=self._identifier,
            name=self.name_edit.text(),
            pattern=self.pattern_edit.text(),
            fields=self.selected_fields(),
            action=SensitiveAction(str(self.action_combo.currentData())),
            enabled=self.enabled.isChecked(),
            ignore_case=self.ignore_case.isChecked(),
            sort_order=self._sort_order if sort_order is None else sort_order,
            pattern_modules=tuple(self._modules),
        ).normalized()

    def _add_module(self) -> None:
        kind = SensitivePatternKind(str(self.module_kind.currentData()))
        try:
            module = SensitivePatternModule(kind, self.module_value.text()).normalized()
            _module_pattern(module)
        except ValueError as error:
            self.validation_label.setText(str(error))
            self.validation_label.setStyleSheet("color: #d64545")
            return
        if len(self._modules) >= 20:
            self.validation_label.setText("最多只能添加 20 个表达式模块")
            self.validation_label.setStyleSheet("color: #d64545")
            return
        self._modules.append(module)
        self.module_value.clear()
        self._apply_modules()

    def _module_kind_changed(self) -> None:
        kind = SensitivePatternKind(str(self.module_kind.currentData()))
        needs_value = kind not in {
            SensitivePatternKind.NUMBER,
            SensitivePatternKind.EMAIL,
            SensitivePatternKind.URL,
        }
        self.module_value.setEnabled(needs_value)
        self.module_value.setPlaceholderText(
            (
                "多个词可用 ,，、换行或 | 分隔"
                if kind is SensitivePatternKind.ANY_WORD
                else "请输入完整文字"
            )
            if needs_value
            else "该模块无需填写内容"
        )

    def _remove_module(self) -> None:
        row = self.module_list.currentRow()
        if row >= 0:
            del self._modules[row]
            self._apply_modules()

    def _clear_modules(self) -> None:
        self._modules.clear()
        self._apply_modules()

    def _advanced_pattern_edited(self) -> None:
        if self._modules:
            self._modules.clear()
            self._render_modules()

    def _apply_modules(self) -> None:
        patterns = [_module_pattern(module) for module in self._modules]
        generated = "" if not patterns else "(?:" + "|".join(patterns) + ")"
        self.pattern_edit.setText(generated)
        self._render_modules()

    def _render_modules(self) -> None:
        self.module_list.clear()
        for module in self._modules:
            detail = module.value if module.value else "自动识别"
            item = QListWidgetItem(f"{MODULE_NAMES[module.kind]}：{detail}")
            item.setData(Qt.ItemDataRole.UserRole, module.kind.value)
            self.module_list.addItem(item)

    def _validate(self) -> None:
        error: str | None = None
        output = ""
        if not self.name_edit.text().strip():
            error = "请输入规则名称"
        elif not self.pattern_edit.text():
            error = "请输入正则表达式"
        elif not self.selected_fields():
            error = "请至少选择一个字段"
        else:
            try:
                flags = regex.IGNORECASE | regex.FULLCASE if self.ignore_case.isChecked() else 0
                compiled = regex.compile(self.pattern_edit.text(), flags)
                probe = self.test_edit.toPlainText()
                if probe:
                    matches = list(compiled.finditer(probe, timeout=0.005))
                    output = (
                        compiled.sub("•••", probe, timeout=0.005)
                        if matches
                        else "未命中"
                    )
                compiled.search("a" * 4096 + "!", timeout=0.005)
            except regex.error as exc:
                error = f"表达式无效：{exc}"
            except TimeoutError:
                error = "表达式执行超时，请简化规则"
        self.validation_label.setText(error or "表达式有效")
        self.validation_label.setStyleSheet(
            "color: #d64545" if error else "color: #2e9d5b"
        )
        self.test_result.setPlainText("" if error else output)
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        if save is not None:
            save.setEnabled(error is None)


def _module_pattern(module: SensitivePatternModule) -> str:
    kind = module.kind
    escaped = regex.escape(module.value)
    if kind is SensitivePatternKind.CONTAINS:
        return escaped
    if kind is SensitivePatternKind.EXACT:
        return rf"\A(?:{escaped})\Z"
    if kind is SensitivePatternKind.PREFIX:
        return rf"\A(?:{escaped})"
    if kind is SensitivePatternKind.SUFFIX:
        return rf"(?:{escaped})\Z"
    if kind is SensitivePatternKind.ANY_WORD:
        words: list[str] = []
        seen: set[str] = set()
        for raw_word in regex.split(r"[,，、|\r\n]+", module.value):
            word = unicodedata.normalize("NFC", raw_word.strip())
            if word and word not in seen:
                words.append(word)
                seen.add(word)
        if not words:
            raise ValueError("请输入至少一个关键词")
        return "(?:" + "|".join(regex.escape(word) for word in words) + ")"
    if kind is SensitivePatternKind.NUMBER:
        return r"\d+"
    if kind is SensitivePatternKind.EMAIL:
        return r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+"
    if kind is SensitivePatternKind.URL:
        return r"https?://[^\s]+"
    raise ValueError("不支持的表达式模块")
