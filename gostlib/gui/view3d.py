"""
Просмотр 3D-модели вместе с посадочным местом.

Смысл — глазами убедиться, что модель та самая: корпус садится на свои
площадки, ничего не провалилось под плату и не повёрнуто на 90°. Поэтому
рисуется не голая модель, а сцена: плата, площадки посадки и корпус
поверх них.

Модель показывается телом: из STEP берутся плоские грани и заливаются с
простым освещением, сортировка — по глубине. Там, где граней нет
(фасетные модели из OBJ), остаётся каркас по рёбрам.

Здесь же кнопки поворота и сдвига модели относительно посадочного места —
как в KiCad. Значения сохраняются в компоненте и уезжают в Altium.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

Point = Tuple[float, float, float]

MAX_FACES = 6000
MAX_EDGES = 6000


def _rot(p: Point, rx: float, ry: float, rz: float) -> Point:
    x, y, z = p
    if rx:
        a = math.radians(rx)
        y, z = y * math.cos(a) - z * math.sin(a), y * math.sin(a) + z * math.cos(a)
    if ry:
        a = math.radians(ry)
        x, z = x * math.cos(a) + z * math.sin(a), -x * math.sin(a) + z * math.cos(a)
    if rz:
        a = math.radians(rz)
        x, y = x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)
    return (x, y, z)


class Scene3D(QWidget):
    """Плата, площадки и корпус. ЛКМ — поворот вида, ПКМ — сдвиг, колесо — зум."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.faces: List[List[Point]] = []
        self.edges: List[Tuple[Point, Point]] = []
        self.points: List[Point] = []
        self.pads: List[Tuple[float, float, float, float]] = []
        self.outline: List[Tuple[float, float, float, float]] = []
        # трансформация модели относительно посадочного места
        self.tr = dict(dx=0.0, dy=0.0, dz=0.0, rx=0.0, ry=0.0, rz=0.0)
        self.color = (0.62, 0.64, 0.68)   # цвет корпуса, доли 0..1
        self.tri_colors: List[Tuple[float, float, float]] = []
        self.smooth = False               # сетка мелкая -- контур не рисуем
        self.board = True
        self.show_pads = True
        self.wire = False
        self.az, self.el = 35.0, 24.0
        self.zoom = 1.0
        self.pan = [0.0, 0.0]
        self._drag = None
        self._btn = None
        # Кеш подготовленной геометрии. Поворот модели и освещение от вида
        # не зависят, поэтому считаются один раз, а не на каждую перерисовку
        # (иначе при 8000 треугольников вращение заметно вязнет).
        self._cache_key = None
        self._pl_faces = []       # грани в координатах платы
        self._pl_cent = []        # центры граней -- для сортировки по глубине
        self._pl_col = []         # готовый QColor каждой грани
        self._pl_nrm = []         # нормали -- по ним отбрасываем изнанку
        self._busy = False        # идёт перетаскивание -- рисуем упрощённо
        self.setMinimumSize(240, 200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------ данные ----
    def set_mesh(self, mesh):
        """Настоящая треугольная сетка вместо приблизительных граней."""
        self.smooth = bool(mesh is not None and getattr(mesh, "ok", False)
                           and len(mesh.tris) > 400)
        if mesh is not None and getattr(mesh, "ok", False):
            self.faces = [list(t) for t in mesh.tris]
            self.tri_colors = list(mesh.colors)
            self.edges = []
            self.points = []
        self.update()

    def set_model(self, model=None, fp=None):
        self.faces, self.edges, self.points = [], [], []
        self.tri_colors = []
        self.pads, self.outline = [], []
        if model is not None and getattr(model, "ok", False):
            fc = list(getattr(model, "faces", []) or [])
            if len(fc) > MAX_FACES:
                step = len(fc) / float(MAX_FACES)
                fc = [fc[int(i * step)] for i in range(MAX_FACES)]
            self.faces = fc
            ed = list(model.edges or [])
            if len(ed) > MAX_EDGES:
                step = len(ed) / float(MAX_EDGES)
                ed = [ed[int(i * step)] for i in range(MAX_EDGES)]
            self.edges = ed
            if not fc and not ed:
                self.points = list(model.points or [])[:4000]
            col = getattr(model, "color", None)
            # слишком тёмный цвет из файла превращает модель в силуэт --
            # подсветим, но оттенок сохраним
            if col:
                mx = max(col)
                self.color = tuple(c / mx * 0.75 for c in col) if mx > 0.05 \
                    else (0.45, 0.47, 0.5)
            else:
                self.color = (0.62, 0.64, 0.68)
        if fp is not None:
            for p in getattr(fp, "pads", []):
                self.pads.append((p.x - p.w / 2, p.y - p.h / 2, p.w, p.h))
            for pr in getattr(fp, "prims", []):
                if pr.layer not in ("courtyard", "assy"):
                    continue
                pts = pr.pts or []
                if pr.kind == "rect" and len(pts) >= 2:
                    (x1, y1), (x2, y2) = pts[0], pts[1]
                    ring = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
                    self.outline += [(a[0], a[1], b[0], b[1])
                                     for a, b in zip(ring, ring[1:])]
                elif len(pts) >= 2:
                    self.outline += [(a[0], a[1], b[0], b[1])
                                     for a, b in zip(pts, pts[1:])]
            m = getattr(fp, "model", None)
            if m is not None:
                self.tr = dict(dx=m.dx, dy=m.dy, dz=m.dz,
                               rx=m.rx, ry=m.ry, rz=m.rz)
        self.reset_view()

    def reset_view(self):
        self.az, self.el = 35.0, 24.0
        self.zoom = 1.0
        self.pan = [0.0, 0.0]
        self.update()

    def top_view(self):
        self.az, self.el = 0.0, 89.9
        self.update()

    def side_view(self):
        self.az, self.el = 0.0, 0.0
        self.update()

    # ------------------------------------------------------------ мышь ------
    def wheelEvent(self, e):
        k = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.zoom = max(0.1, min(50.0, self.zoom * k))
        self.update()

    def mousePressEvent(self, e):
        self._drag = e.position()
        self._btn = e.button()
        self._busy = True

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        d = e.position() - self._drag
        self._drag = e.position()
        if self._btn == Qt.LeftButton:
            self.az = (self.az + d.x() * 0.5) % 360.0
            self.el = max(-89.9, min(89.9, self.el + d.y() * 0.5))
        else:
            self.pan[0] += d.x()
            self.pan[1] += d.y()
        self.update()

    def mouseReleaseEvent(self, _e):
        self._drag = None
        self._btn = None
        self._busy = False
        self.update()

    def mouseDoubleClickEvent(self, _e):
        self.reset_view()

    # ---------------------------------------------------------- геометрия ---
    def _place(self, p: Point) -> Point:
        t = self.tr
        x, y, z = _rot(p, t["rx"], t["ry"], t["rz"])
        return (x + t["dx"], y + t["dy"], z + t["dz"])

    def _prepare(self):
        """
        Пересчитать грани, их центры и цвета. Делается только когда
        поменялась модель или её трансформация.
        """
        t = self.tr
        key = (id(self.faces), len(self.faces), t["rx"], t["ry"], t["rz"],
               t["dx"], t["dy"], t["dz"], self.smooth, len(self.tri_colors))
        if key == self._cache_key:
            return
        self._cache_key = key
        faces, cents, cols, nrms = [], [], [], []
        base = self.color
        for i, f in enumerate(self.faces):
            pf = [self._place(v) for v in f]
            faces.append(pf)
            n = len(pf)
            cents.append((sum(v[0] for v in pf) / n,
                          sum(v[1] for v in pf) / n,
                          sum(v[2] for v in pf) / n))
            # Цвет части модели плюс диффузная подсветка сверху-сбоку:
            # грани разной ориентации получают разную яркость, и тело
            # читается как объём.
            bc = self.tri_colors[i] if i < len(self.tri_colors) else base
            nv = _normal(pf)
            lit = 0.34 + 0.44 * abs(nv[2]) + 0.22 * abs(nv[0])
            lit = max(0.18, min(1.0, lit))
            cols.append(QColor(min(255, int(255 * bc[0] * lit)),
                               min(255, int(255 * bc[1] * lit)),
                               min(255, int(255 * bc[2] * lit))))
            nrms.append(nv)
        self._pl_faces, self._pl_cent = faces, cents
        self._pl_col, self._pl_nrm = cols, nrms

    def _model_polys(self):
        self._prepare()
        return self._pl_faces

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0e0e16"))
        polys = self._model_polys()
        edges = [(self._place(a), self._place(b)) for a, b in self.edges]
        pts = [self._place(v) for v in self.points]

        xs, ys, zs = [], [], []
        for f in polys:
            for v in f:
                xs.append(v[0]); ys.append(v[1]); zs.append(v[2])
        for a, b in edges:
            xs += [a[0], b[0]]; ys += [a[1], b[1]]; zs += [a[2], b[2]]
        for v in pts:
            xs.append(v[0]); ys.append(v[1]); zs.append(v[2])
        for (x, y, w, h) in self.pads:
            xs += [x, x + w]; ys += [y, y + h]; zs.append(0.0)
        for (x1, y1, x2, y2) in self.outline:
            xs += [x1, x2]; ys += [y1, y2]
        if not xs:
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignCenter, "3D-модели нет")
            return

        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        cz = (min(zs + [0.0]) + max(zs + [0.0])) / 2.0
        # масштаб считаем по габариту в плане: иначе модель с «этажами» по Z
        # (а такие в STEP попадаются) внезапно сжимает всю картинку
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-3)

        a = math.radians(self.az)
        el = math.radians(self.el)
        ca, sa = math.cos(a), math.sin(a)
        ce, se = math.cos(el), math.sin(el)
        w, h = self.width(), self.height()
        k = min(w, h) / (span * 1.7) * self.zoom
        ox = w / 2.0 + self.pan[0]
        oy = h / 2.0 + self.pan[1]

        def proj(v):
            X = (v[0] - cx) * ca - (v[1] - cy) * sa
            Y = (v[0] - cx) * sa + (v[1] - cy) * ca
            sy = -((v[2] - cz) * ce - Y * se)
            return QPointF(ox + X * k, oy + sy * k)

        def depth(v):
            Y = (v[0] - cx) * sa + (v[1] - cy) * ca
            return Y * ce + (v[2] - cz) * se

        # при вращении сглаживание -- самая дорогая часть кадра; на глаз
        # разницы в движении нет, зато вращение перестаёт вязнуть
        p.setRenderHint(QPainter.Antialiasing,
                        not (self._busy and len(polys) > 1500))

        # --- плата
        if self.board:
            m = span * 0.8
            p.setPen(QPen(QColor("#2c6b3a"), 1))
            p.setBrush(QColor(26, 72, 44))
            p.drawPolygon(QPolygonF([proj((cx - m, cy - m, 0.0)),
                                     proj((cx + m, cy - m, 0.0)),
                                     proj((cx + m, cy + m, 0.0)),
                                     proj((cx - m, cy + m, 0.0))]))
            p.setBrush(Qt.NoBrush)

        # --- площадки
        if self.show_pads:
            p.setPen(QPen(QColor("#e0c060"), 1))
            p.setBrush(QColor(212, 176, 84))
            for (x, y, pw, ph) in self.pads:
                p.drawPolygon(QPolygonF([proj((x, y, 0.0)),
                                         proj((x + pw, y, 0.0)),
                                         proj((x + pw, y + ph, 0.0)),
                                         proj((x, y + ph, 0.0))]))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#8a6f2a"), 1))
            for (x1, y1, x2, y2) in self.outline:
                p.drawLine(proj((x1, y1, 0.0)), proj((x2, y2, 0.0)))

        # --- корпус
        if polys and not self.wire:
            cents = self._pl_cent
            cols = self._pl_col
            nrms = self._pl_nrm
            # Направление на зрителя: у функции depth больший результат --
            # ближе, значит её градиент и есть взгляд.
            vx, vy, vz = sa * ce, ca * ce, se
            idx = range(len(polys))
            if len(nrms) == len(polys):
                vis = [i for i in idx
                       if nrms[i][0] * vx + nrms[i][1] * vy
                       + nrms[i][2] * vz > 0]
                # если у модели нормали смотрят внутрь, отсечение съело бы
                # почти всё -- тогда рисуем как есть
                if len(vis) > len(polys) * 0.15:
                    idx = vis
            # сортируем по глубине центров: один пересчёт на грань вместо
            # трёх, и порядок тот же
            order = sorted(idx, key=lambda i: depth(cents[i]))
            npen = QPen(Qt.NoPen)
            for i in order:
                sp = [proj(v) for v in polys[i]]
                col = cols[i] if i < len(cols) else QColor(150, 150, 155)
                p.setBrush(col)
                # у мелких треугольников контур съедает форму -- рисуем
                # заливку тем же цветом, чтобы не было сетки поверх тела
                p.setPen(npen if self.smooth else QPen(col.darker(135), 1))
                p.drawPolygon(QPolygonF(sp))
            p.setBrush(Qt.NoBrush)
        else:
            p.setPen(QPen(QColor("#7fc7ff"), 1))
            for a1, b1 in edges:
                p.drawLine(proj(a1), proj(b1))
            if not edges and pts:
                p.setPen(QPen(QColor("#7fc7ff"), 2))
                for v in pts:
                    p.drawPoint(proj(v))

        p.setPen(QColor("#7d879b"))
        t = self.tr
        p.drawText(8, h - 8,
                   f"вид {self.az:.0f}°/{self.el:.0f}°   "
                   f"модель: поворот {t['rx']:.0f}/{t['ry']:.0f}/{t['rz']:.0f}°, "
                   f"сдвиг {t['dx']:.2f}/{t['dy']:.2f}/{t['dz']:.2f} мм")


def _normal(f: Sequence[Point]) -> Point:
    if len(f) < 3:
        return (0.0, 0.0, 1.0)
    ax, ay, az = f[0]
    bx, by, bz = f[1]
    cx, cy, cz = f[2]
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    d = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return (nx / d, ny / d, nz / d)


class Model3DPane(QWidget):
    """Сцена, виды и правка положения модели относительно посадки."""

    transform_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene = Scene3D()
        self.title = QLabel("3D-модель")
        self.title.setStyleSheet("color:#888;")

        self.cb_board = QCheckBox("плата")
        self.cb_board.setChecked(True)
        self.cb_pads = QCheckBox("площадки")
        self.cb_pads.setChecked(True)
        self.cb_wire = QCheckBox("каркас")
        for cb in (self.cb_board, self.cb_pads, self.cb_wire):
            cb.stateChanged.connect(self._toggle)

        bar = QHBoxLayout()
        bar.addWidget(self.title, 1)
        bar.addWidget(self.cb_board)
        bar.addWidget(self.cb_pads)
        bar.addWidget(self.cb_wire)
        for txt, fn in (("сверху", self.scene.top_view),
                        ("сбоку", self.scene.side_view),
                        ("3D", self.scene.reset_view)):
            b = QPushButton(txt)
            b.setFixedHeight(22)
            b.setMaximumWidth(64)
            b.clicked.connect(fn)
            bar.addWidget(b)

        # --- правка положения модели
        ops = QHBoxLayout()
        ops.addWidget(QLabel("поворот:"))
        for txt, kw in (("X +90", ("rx", 90)), ("Y +90", ("ry", 90)),
                        ("Z +90", ("rz", 90)), ("Z −90", ("rz", -90))):
            b = QPushButton(txt)
            b.setFixedHeight(22)
            b.setMaximumWidth(64)
            b.clicked.connect(lambda _=False, k=kw: self._bump(k[0], k[1]))
            ops.addWidget(b)
        ops.addSpacing(10)
        ops.addWidget(QLabel("сдвиг, мм:"))
        for txt, kw in (("X−", ("dx", -0.1)), ("X+", ("dx", 0.1)),
                        ("Y−", ("dy", -0.1)), ("Y+", ("dy", 0.1)),
                        ("Z−", ("dz", -0.1)), ("Z+", ("dz", 0.1))):
            b = QPushButton(txt)
            b.setFixedHeight(22)
            b.setMaximumWidth(40)
            b.clicked.connect(lambda _=False, k=kw: self._bump(k[0], k[1]))
            ops.addWidget(b)
        b = QPushButton("сбросить")
        b.setFixedHeight(22)
        b.clicked.connect(self._reset_tr)
        ops.addWidget(b)
        ops.addStretch(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        lay.addLayout(bar)
        lay.addWidget(self.scene, 1)
        lay.addLayout(ops)

    def _toggle(self):
        self.scene.board = self.cb_board.isChecked()
        self.scene.show_pads = self.cb_pads.isChecked()
        self.scene.wire = self.cb_wire.isChecked()
        self.scene.update()

    def _bump(self, key: str, delta: float):
        t = self.scene.tr
        if key.startswith("r"):
            t[key] = (t[key] + delta) % 360.0
        else:
            t[key] = round(t[key] + delta, 3)
        self.scene.update()
        self.transform_changed.emit(dict(t))

    def _reset_tr(self):
        self.scene.tr = dict(dx=0.0, dy=0.0, dz=0.0, rx=0.0, ry=0.0, rz=0.0)
        self.scene.update()
        self.transform_changed.emit(dict(self.scene.tr))

    def set_model(self, model=None, fp=None, subtitle: str = ""):
        self.scene.set_model(model, fp)
        self.title.setText(subtitle or "3D-модель")
