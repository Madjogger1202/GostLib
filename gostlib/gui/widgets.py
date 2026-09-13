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


# --------------------------------------------------------------- размеры ----
def relax(w, mw: int = 140, mh: int = 100):
    """
    Разрешить панели быть маленькой.

    Ключ -- политика Ignored: при ней Qt перестаёт считать минимумом
    подсказку раскладки и берёт явно заданный минимум. Одного
    setMinimumSize мало: подсказка всё равно побеждает, и окно, у
    которого внутри пара таблиц и три превью, требует под две тысячи
    пикселей ширины.
    """
    from PySide6.QtWidgets import QLayout

    if w is None:
        return
    lay = w.layout() if hasattr(w, "layout") else None
    if lay is not None:
        lay.setSizeConstraint(QLayout.SetNoConstraint)
    w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
    w.setMinimumSize(mw, mh)


def allow_narrow(root, extra=()):
    """
    Снять с окна накопленный минимум: пусть содержимое ужимается, а не
    диктует размер. Проходим по всем вложенным сплиттерам и вкладкам --
    именно они складывают минимумы в неподъёмное число.
    """
    from PySide6.QtWidgets import QSplitter, QTabWidget

    for tw in root.findChildren(QTabWidget):
        for i in range(tw.count()):
            relax(tw.widget(i))
        relax(tw, 200, 120)
    for sp in root.findChildren(QSplitter):
        sp.setChildrenCollapsible(True)
        for i in range(sp.count()):
            relax(sp.widget(i))
        relax(sp, 160, 100)
    for w in extra:
        relax(w, 160, 120)


def fit_to_screen(win, want_w: int, want_h: int, margin: int = 96):
    """
    Задать размер окна, не вылезая за экран, и поставить его по центру
    ТОГО ЖЕ экрана.

    Инструмент не должен разворачиваться на второй монитор и не должен
    открываться больше рабочей области: у Qt свободная геометрия -- это
    экран без панели задач, по ней и равняемся.
    """
    from PySide6.QtGui import QGuiApplication

    scr = None
    try:
        scr = win.screen()
    except Exception:
        scr = None
    if scr is None:
        parent = win.parent() if hasattr(win, "parent") else None
        scr = ((parent.screen() if parent is not None else None)
               or QGuiApplication.primaryScreen())
    if scr is None:
        win.resize(want_w, want_h)
        return
    g = scr.availableGeometry()
    w = max(560, min(int(want_w), g.width() - margin))
    h = max(420, min(int(want_h), g.height() - margin))
    win.resize(w, h)
    win.move(g.x() + (g.width() - w) // 2, g.y() + (g.height() - h) // 2)
