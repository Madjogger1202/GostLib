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
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox,
                               QDoubleSpinBox, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

Point = Tuple[float, float, float]

# Потолок граней у окна. Должен быть не ниже mesh3d.MAX_TRIS: сетку
# готовит mesh3d, и резать её ещё раз здесь -- значит портить то, что там
# бережно строили. Это предохранитель для чужих OBJ и STL.
MAX_FACES = 32000
MAX_EDGES = 6000

# Сколько граней рисовать, пока модель тащат мышью. Кадр стоит примерно
# линейно числу граней, поэтому в движении показываем только самые
# крупные: силуэт и корпус на месте, мелочь появляется, как только
# кнопку отпустили.
DRAG_FACES = 4000


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
        # контуры площадок в мм: список точек на каждую
        self.pads: List[List[Tuple[float, float]]] = []
        self.outline: List[Tuple[float, float, float, float]] = []
        # трансформация модели относительно посадочного места
        self.tr = dict(dx=0.0, dy=0.0, dz=0.0, rx=0.0, ry=0.0, rz=0.0)
        self.color = (0.62, 0.64, 0.68)   # цвет корпуса, доли 0..1
        self.override_color = None         # выбранный пользователем цвет
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
        self._pl_big = []         # самые крупные грани -- для вращения
        self._model_bounds = None  # min/max уже преобразованной модели
        self._busy = False        # идёт перетаскивание -- рисуем упрощённо
        self.setMinimumSize(240, 200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------ данные ----
    def set_mesh(self, mesh):
        """Настоящая треугольная сетка вместо приблизительных граней."""
        self.smooth = bool(mesh is not None and getattr(mesh, "ok", False)
                           and len(mesh.tris) > 400)
        if mesh is not None and getattr(mesh, "ok", False):
            tris = list(mesh.tris)
            cols = list(mesh.colors)
            # Последний предохранитель для чужих OBJ/STL. Обычно mesh3d
            # заранее упрощает поверхность без дыр, но GUI никогда не должен
            # получить десятки тысяч полигонов и подвиснуть при движении.
            if len(tris) > MAX_FACES:
                # Не «каждый k-й»: так в теле появляются дыры. Оставляем
                # самые крупные грани -- они и держат форму.
                take = sorted(_biggest(tris, MAX_FACES))
                tris = [tris[i] for i in take]
                cols = [cols[i] for i in take if i < len(cols)]
            self.faces = [list(t) for t in tris]
            self.tri_colors = cols
            self.edges = []
            self.points = []
            self._cache_key = None
        self.update()

    def set_color(self, value: str = ""):
        """Задать цвет корпуса для просмотра; пусто = материал модели."""
        q = QColor(value) if value else QColor()
        self.override_color = ((q.redF(), q.greenF(), q.blueF())
                               if q.isValid() else None)
        self._cache_key = None
        self.update()

    def set_model(self, model=None, fp=None):
        self.faces, self.edges, self.points = [], [], []
        self.tri_colors = []
        self.pads, self.outline = [], []
        if model is not None and getattr(model, "ok", False):
            fc = list(getattr(model, "faces", []) or [])
            if len(fc) > MAX_FACES:
                fc = [fc[i] for i in sorted(_biggest(fc, MAX_FACES))]
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
            # Площадка -- это её настоящий контур, а не габаритный
            # прямоугольник: овал, восьмиугольник и поворот раньше
            # терялись, и картинка расходилась с посадкой.
            from ..ir import pad_polygon
            for p in getattr(fp, "pads", []):
                try:
                    self.pads.append(pad_polygon(p))
                except Exception:
                    hw, hh = p.w / 2.0, p.h / 2.0
                    self.pads.append([(p.x - hw, p.y - hh), (p.x + hw, p.y - hh),
                                      (p.x + hw, p.y + hh), (p.x - hw, p.y + hh)])
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
                self.set_color(getattr(m, "color", ""))
            else:
                self.set_color("")
        else:
            self.set_color("")
        self._cache_key = None
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
               t["dx"], t["dy"], t["dz"], self.smooth,
               len(self.tri_colors), self.override_color)
        if key == self._cache_key:
            return
        self._cache_key = key
        faces, cents, cols, nrms = [], [], [], []
        base = self.override_color or self.color
        xs, ys, zs = [], [], []
        for i, f in enumerate(self.faces):
            pf = [self._place(v) for v in f]
            faces.append(pf)
            xs.extend(v[0] for v in pf)
            ys.extend(v[1] for v in pf)
            zs.extend(v[2] for v in pf)
            n = len(pf)
            cents.append((sum(v[0] for v in pf) / n,
                          sum(v[1] for v in pf) / n,
                          sum(v[2] for v in pf) / n))
            # Цвет части модели плюс диффузная подсветка сверху-сбоку:
            # грани разной ориентации получают разную яркость, и тело
            # читается как объём.
            bc = (base if self.override_color is not None else
                  (self.tri_colors[i]
                   if i < len(self.tri_colors) else base))
            nv = _normal(pf)
            # QPainter не умеет интерполировать нормали внутри полигона.
            # Непрерывная формула поэтому проявляла диагонали исходной OBJ
            # как «сетку». Для почти плоских граней используем один тон, а
            # промежуточный оставляем скруглениям и фаскам.
            ax, ay, az = abs(nv[0]), abs(nv[1]), abs(nv[2])
            if nv[2] > 0.15:
                lit = 0.96
            elif ax >= 0.82:
                lit = 0.76
            elif ay >= 0.82:
                lit = 0.84
            else:
                lit = 0.88
            cols.append(QColor(min(255, int(255 * bc[0] * lit)),
                               min(255, int(255 * bc[1] * lit)),
                               min(255, int(255 * bc[2] * lit))))
            nrms.append(nv)
        self._pl_faces, self._pl_cent = faces, cents
        self._pl_col, self._pl_nrm = cols, nrms
        # Кого рисовать в движении: считаем один раз здесь, а не на кадр.
        self._pl_big = (_biggest(faces, DRAG_FACES)
                        if len(faces) > DRAG_FACES else [])
        self._model_bounds = ((min(xs), min(ys), min(zs),
                               max(xs), max(ys), max(zs))
                              if xs else None)

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
        if self._model_bounds:
            x1, y1, z1, x2, y2, z2 = self._model_bounds
            xs += [x1, x2]; ys += [y1, y2]; zs += [z1, z2]
        for a, b in edges:
            xs += [a[0], b[0]]; ys += [a[1], b[1]]; zs += [a[2], b[2]]
        for v in pts:
            xs.append(v[0]); ys.append(v[1]); zs.append(v[2])
        for poly in self.pads:
            for (px, py) in poly:
                xs.append(px); ys.append(py)
            zs.append(0.0)
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
            for poly in self.pads:
                p.drawPolygon(QPolygonF([proj((px, py, 0.0))
                                         for px, py in poly]))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#8a6f2a"), 1))
            for (x1, y1, x2, y2) in self.outline:
                p.drawLine(proj((x1, y1, 0.0)), proj((x2, y2, 0.0)))

        # --- корпус
        if polys and not self.wire:
            # Антиалиасинг каждого треугольника отдельно оставляет между
            # соседями полупрозрачные швы — визуально получается сетка.
            # Контур всего тела при тысячах мелких граней и без него гладкий.
            if self.smooth:
                p.setRenderHint(QPainter.Antialiasing, False)
            cents = self._pl_cent
            cols = self._pl_col
            nrms = self._pl_nrm
            # Направление на зрителя: у функции depth больший результат --
            # ближе, значит её градиент и есть взгляд.
            vx, vy, vz = sa * ce, ca * ce, se
            # В движении крупной модели показываем только крупные грани:
            # иначе кадр упирается в число полигонов и вращение вязнет.
            idx = (self._pl_big if self._busy and self._pl_big
                   else range(len(polys)))
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


def _area(f: Sequence[Point]) -> float:
    """Удвоенная площадь грани -- сравнивать между собой этого хватает."""
    if len(f) < 3:
        return 0.0
    ax, ay, az = f[0]
    bx, by, bz = f[1]
    cx, cy, cz = f[2]
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    return math.sqrt(nx * nx + ny * ny + nz * nz)


def _biggest(faces, limit: int) -> List[int]:
    """Номера limit самых крупных граней."""
    order = sorted(range(len(faces)), key=lambda i: -_area(faces[i]))
    return order[:limit]


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
    color_changed = Signal(str)

    COLORS = (
        ("цвет модели", ""),
        ("серый", "#9EA3AD"),
        ("чёрный", "#34383F"),
        ("зелёный", "#4C805D"),
        ("синий", "#4B72A8"),
        ("керамика", "#D0C3A0"),
    )

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
        self.color_pick = QComboBox()
        self.color_pick.setToolTip(
            "Цвет корпуса. Пишется и в сам файл модели, поэтому в Altium\n"
            "корпус будет такого же цвета. «родной» — вернуть цвета,\n"
            "которые были в исходной модели. Файл, выбранный руками,\n"
            "не трогается.")
        for name, value in self.COLORS:
            self.color_pick.addItem(name, value)
        self.color_pick.currentIndexChanged.connect(self._pick_color)
        bar.addWidget(self.color_pick)
        rgb = QPushButton("RGB…")
        rgb.setToolTip("Выбрать произвольный цвет корпуса")
        rgb.setFixedHeight(22)
        rgb.clicked.connect(self._choose_rgb)
        bar.addWidget(rgb)
        for txt, fn in (("сверху", self.scene.top_view),
                        ("сбоку", self.scene.side_view),
                        ("3D", self.scene.reset_view)):
            b = QPushButton(txt)
            b.setFixedHeight(22)
            b.setMaximumWidth(64)
            b.clicked.connect(fn)
            bar.addWidget(b)

        # --- правка положения модели
        # Поля, а не кнопки «+0,1»: сдвиг на 3,2 мм кнопками -- это
        # тридцать два нажатия. В поле число вводится сразу, а стрелки и
        # колесо мыши по-прежнему шагают понемногу.
        ops = QHBoxLayout()
        ops.addWidget(QLabel("поворот, °:"))
        self.spins = {}
        for key in ("rx", "ry", "rz"):
            sp = QDoubleSpinBox()
            sp.setRange(-360.0, 360.0)
            sp.setDecimals(1)
            sp.setSingleStep(90.0)
            sp.setWrapping(True)
            sp.setPrefix(key[1].upper() + " ")
            sp.setKeyboardTracking(False)
            sp.setMaximumWidth(92)
            sp.setToolTip("Шаг стрелками — 90°. Можно вписать любой угол.")
            sp.valueChanged.connect(self._spin_changed)
            self.spins[key] = sp
            ops.addWidget(sp)
        ops.addSpacing(10)
        ops.addWidget(QLabel("сдвиг, мм:"))
        for key in ("dx", "dy", "dz"):
            sp = QDoubleSpinBox()
            sp.setRange(-200.0, 200.0)
            sp.setDecimals(3)
            sp.setSingleStep(0.05)
            sp.setPrefix(key[1].upper() + " ")
            sp.setKeyboardTracking(False)
            sp.setMaximumWidth(104)
            sp.setToolTip("Впишите число или крутите колесом: шаг 0,05 мм.\n"
                          "Z — высота над платой (standoff).")
            sp.valueChanged.connect(self._spin_changed)
            self.spins[key] = sp
            ops.addWidget(sp)
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

    def _pick_color(self, index: int):
        value = self.color_pick.itemData(index) or ""
        self.scene.set_color(value)
        self.color_changed.emit(value)

    def _choose_rgb(self):
        current = (QColor.fromRgbF(*self.scene.override_color)
                   if self.scene.override_color else QColor("#9EA3AD"))
        q = QColorDialog.getColor(current, self, "Цвет 3D-модели")
        if not q.isValid():
            return
        value = q.name(QColor.HexRgb).upper()
        self.color_pick.blockSignals(True)
        while self.color_pick.count() > len(self.COLORS):
            self.color_pick.removeItem(self.color_pick.count() - 1)
        self.color_pick.addItem(f"свой {value}", value)
        self.color_pick.setCurrentIndex(self.color_pick.count() - 1)
        self.color_pick.blockSignals(False)
        self.scene.set_color(value)
        self.color_changed.emit(value)

    def _bump(self, key: str, delta: float):
        t = self.scene.tr
        if key.startswith("r"):
            t[key] = (t[key] + delta) % 360.0
        else:
            t[key] = round(t[key] + delta, 3)
        self._sync_spins()
        self.scene.update()
        self.transform_changed.emit(dict(t))

    def _sync_spins(self):
        """Показать в полях то, что сейчас стоит у модели."""
        for key, sp in getattr(self, "spins", {}).items():
            sp.blockSignals(True)
            sp.setValue(float(self.scene.tr.get(key, 0.0) or 0.0))
            sp.blockSignals(False)

    def _spin_changed(self, _value=None):
        t = self.scene.tr
        for key, sp in self.spins.items():
            v = float(sp.value())
            t[key] = (v % 360.0) if key.startswith("r") else round(v, 3)
        self.scene._cache_key = None
        self.scene.update()
        self.transform_changed.emit(dict(t))

    def _reset_tr(self):
        self.scene.tr = dict(dx=0.0, dy=0.0, dz=0.0, rx=0.0, ry=0.0, rz=0.0)
        self._sync_spins()
        self.scene.update()
        self.transform_changed.emit(dict(self.scene.tr))

    def set_model(self, model=None, fp=None, subtitle: str = ""):
        self.scene.set_model(model, fp)
        self._sync_spins()
        self.title.setText(subtitle or "3D-модель")
        value = ""
        if fp is not None and getattr(fp, "model", None) is not None:
            value = getattr(fp.model, "color", "") or ""
        found = next((i for i, (_name, col) in enumerate(self.COLORS)
                      if col.upper() == value.upper()), -1)
        self.color_pick.blockSignals(True)
        while self.color_pick.count() > len(self.COLORS):
            self.color_pick.removeItem(self.color_pick.count() - 1)
        if value and found < 0:
            self.color_pick.addItem(f"свой {value}", value)
            found = self.color_pick.count() - 1
        self.color_pick.setCurrentIndex(max(0, found))
        self.color_pick.setToolTip(
            "Цвет корпуса. Пишется и в сам файл модели, поэтому в Altium\n"
            "корпус будет такого же цвета. «родной» — вернуть цвета,\n"
            "которые были в исходной модели. Файл, выбранный руками,\n"
            "не трогается.")
        self.color_pick.blockSignals(False)
