from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

from .domain import RuntimeState, ShareMode

STATUS_VISUALS: dict[RuntimeState, tuple[str, str, str]] = {
    RuntimeState.ACTIVE: ("✓", "#2e9d5b", "已公开"),
    RuntimeState.CONNECTING: ("↻", "#3478d4", "正在连接"),
    RuntimeState.SUSPENDED: ("Ⅱ", "#c58a16", "已暂停"),
    RuntimeState.DEGRADED: ("!", "#dc7518", "连接降级"),
    RuntimeState.UPDATE_REQUIRED: ("⇧", "#8956c6", "需要更新"),
    RuntimeState.FEATURE_UNAVAILABLE: ("×", "#d64545", "服务不可用"),
    RuntimeState.DISABLED: ("⏻", "#72777f", "已关闭"),
}

MODE_VISUALS: dict[ShareMode, tuple[str, str, str]] = {
    ShareMode.INHERIT: ("⑂", "#3478d4", "继承默认规则"),
    ShareMode.SHARE: ("✓", "#2e9d5b", "分享"),
    ShareMode.HIDE: ("⊘", "#d64545", "隐藏"),
}


def semantic_icon(glyph: str, color: str, size: int = 20) -> QIcon:
    """Paint a resolution-independent glyph into a large DPR-friendly icon source."""
    source_size = max(64, size * 4)
    pixmap = QPixmap(source_size, source_size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    margin = source_size * 0.06
    diameter = int(source_size - 2 * margin)
    painter.drawEllipse(int(margin), int(margin), diameter, diameter)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(int(source_size * 0.52))
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return QIcon(pixmap)


def status_icon(state: RuntimeState) -> QIcon:
    glyph, color, _ = STATUS_VISUALS[state]
    return semantic_icon(glyph, color)


def mode_icon(mode: ShareMode) -> QIcon:
    glyph, color, _ = MODE_VISUALS[mode]
    return semantic_icon(glyph, color)
