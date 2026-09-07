"""Вспомогательные виджеты GUI."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QPainter, QColor
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)


class SvgView(QWidget):
    """Просмотр SVG с масштабированием колесом и подгонкой по окну."""

    def __init__(self, parent=None, bg="#ffffff"):
        super().__init__(parent)
        self._r: Optional[QSvgRenderer] = None
        self._zoom = 1.0
        self._fit = True
        self._pan = [0.0, 0.0]
        self._drag = None
        self._bg = QColor(bg)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

    def set_svg(self, text: str):
        if not text:
            self._r = None
        else:
            self._r = QSvgRenderer(QByteArray(text.encode("utf-8")))
        self._fit = True
        self._zoom = 1.0
        self._pan = [0.0, 0.0]
        self.update()

    def set_background(self, color: str):
        self._bg = QColor(color)
        self.update()

    def fit(self):
        self._fit = True
        self._zoom = 1.0
        self._pan = [0.0, 0.0]
        self.update()

    def zoom_by(self, k: float):
        self._fit = False
        self._zoom = max(0.05, min(40.0, self._zoom * k))
        self.update()

    def wheelEvent(self, e):
        self.zoom_by(1.15 if e.angleDelta().y() > 0 else 1 / 1.15)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.position()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            d = e.position() - self._drag
            self._pan[0] += d.x()
            self._pan[1] += d.y()
            self._drag = e.position()
            self._fit = False
            self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        self.fit()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), self._bg)
        if self._r is None or not self._r.isValid():
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignCenter, "нет изображения")
            return
        vb = self._r.viewBoxF()
        if vb.width() <= 0 or vb.height() <= 0:
            vb = QRectF(0, 0, 100, 100)
        w, h = self.width(), self.height()
        k = min(w / vb.width(), h / vb.height()) * 0.96
        if not self._fit:
            k *= self._zoom
        tw, th = vb.width() * k, vb.height() * k
        x = (w - tw) / 2 + (0 if self._fit else self._pan[0])
        y = (h - th) / 2 + (0 if self._fit else self._pan[1])
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        self._r.render(p, QRectF(x, y, tw, th))


class PreviewPane(QWidget):
    """SvgView + панель кнопок масштаба."""

    def __init__(self, title: str = "", bg="#ffffff", parent=None):
        super().__init__(parent)
        self.view = SvgView(bg=bg)
        bar = QHBoxLayout()
        self.label = QLabel(title)
        self.label.setStyleSheet("color:#888;")
        bar.addWidget(self.label)
        bar.addStretch(1)
        for txt, fn in (("−", lambda: self.view.zoom_by(1 / 1.3)),
                        ("+", lambda: self.view.zoom_by(1.3)),
                        ("по окну", self.view.fit)):
            b = QPushButton(txt)
            b.setFixedHeight(22)
            b.setMaximumWidth(70)
            b.clicked.connect(fn)
            bar.addWidget(b)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)

    def set_svg(self, text: str, subtitle: str = ""):
        self.view.set_svg(text)
        if subtitle:
            self.label.setText(subtitle)
