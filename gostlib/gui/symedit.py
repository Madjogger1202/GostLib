"""
Редактор расположения выводов.

Автоматическая раскладка угадывает верно не всегда: у одного компонента
питание нужно снизу, у другого шину надо разнести на две группы, у
третьего вообще своя логика. Здесь выводы двигаются мышью, а корпус
подстраивается сам, чтобы вплотную поставленный вывод не оказался
снаружи и его всегда можно было ухватить.

Единицы сцены — милы, как в остальном коде символа. Ось Y направлена
вверх, корпус занимает прямоугольник (0, 0)…(W, −H).
"""
from __future__ import annotations

import copy
from typing import List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox,
                               QDialog, QSplitter, QTreeWidget,
                               QTreeWidgetItem,
                               QDialogButtonBox, QHBoxLayout, QLabel, QMenu,
                               QMessageBox, QPushButton, QSizePolicy, QSpinBox,
                               QVBoxLayout, QWidget)

from ..gost import symbolgen
from ..gost.style import GRID, Style
from ..ir import Component, SymPin

HANDLE = 90          # радиус захвата в милах
MARGIN = 700         # поле вокруг корпуса на холсте


def _qcolor(col: int, default: str = "#000000") -> QColor:
    """Цвет Altium (целое BGR) -> QColor. -1 -- цвет по умолчанию."""
    if col is None or int(col) < 0:
        return QColor(default)
    v = int(col)
    return QColor(v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)


def _snap(v: float, g: int = GRID) -> int:
    return int(round(v / float(g)) * g)


class Canvas(QWidget):
    """Холст: корпус, поля, разделители и выводы. Всё таскается мышью."""

    def __init__(self, comp: Component, st: Style, parent=None):
        super().__init__(parent)
        self.comp = comp
        self.st = st
        self.sym = comp.symbol
        self.zoom = 0.25
        self.pan = [0.0, 0.0]
        self.grab = None          # ('pin', pin) | ('fl',) | ('fr',) | ('w',) | ('h',)
        self.hover = None
        self._last = None
        self.auto_body = True
        self.sel: List[SymPin] = []       # выделенные выводы
        self.sel_lines: List[int] = []    # индексы выделенных линий
        self.rubber = None                # рамка выделения (x1,y1,x2,y2) в милах
        self.line_mode = False            # режим рисования линии
        self.rect_mode = False            # режим рисования прямоугольника
        self._line_start = None
        self._line_cur = None
        self.clip: List[List[int]] = []   # буфер обмена линий
        # что разрешено выделять мышью: иначе при рисовании и рамке
        # цепляешь то выводы, то графику
        self.pick_pins = True
        self.pick_lines = True
        self.pick_body = True
        self.show_prims = True            # показывать графику символа
        self.part = 0                     # 0 -- все секции, иначе номер
        # Шаг привязки. 100 mil (2.54 мм) -- сетка ЕСКД, но родные
        # обозначения KiCad живут на 50 и 25 mil, и на крупной сетке их
        # форма разъезжается.
        self.grid = GRID
        self.new_w = 1                    # толщина новой линии
        self.new_col = -1                 # цвет новой линии (-1 -- общий)
        self.setMinimumSize(520, 420)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    # ------------------------------------------------------- геометрия ------
    @property
    def g(self):
        """
        Габариты, поля, разделители и линии ТЕКУЩЕЙ секции.

        Раньше редактор правил поля самого символа, общие на всё, и
        растянутый корпус первой секции растягивал вторую и третью.
        Теперь всё, что рисует и тянет редактор, идёт через эту секцию.
        """
        return self.sym.live(self.part or 1)

    # ------------------------------------------------------- координаты -----
    def to_screen(self, x: float, y: float) -> QPointF:
        return QPointF(self.pan[0] + x * self.zoom, self.pan[1] - y * self.zoom)

    def to_world(self, p) -> Tuple[float, float]:
        return ((p.x() - self.pan[0]) / self.zoom,
                -(p.y() - self.pan[1]) / self.zoom)

    def content_box(self) -> Tuple[float, float, float, float]:
        """(x0, y0, x1, y1) всего, что нарисовано: корпус, выводы, графика."""
        xs = [0.0, float(self.g.body_w or 0)]
        ys = [0.0, -float(self.g.body_h or 0)]
        for p in self.visible_pins():
            out = -1 if p.side == "L" else 1
            xs += [p.x, p.x + out * (p.length or self.st.pin_length)]
            ys.append(p.y)
        for pr in self.sym.prims or []:
            # только графика показываемой секции, иначе «по окну» ужимает
            # символ под все секции сразу
            if self.part and int(pr.unit or 1) not in (self.part, 0):
                continue
            for pt in pr.pts:
                xs.append(pt[0])
                ys.append(pt[1])
            if pr.kind in ("arc", "ellipse") and pr.pts:
                cx, cy = pr.pts[0]
                r = float(pr.radius or 0)
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
        for ln in self.g.user_lines or []:
            xs += [ln[0], ln[2]]
            ys += [ln[1], ln[3]]
        return (min(xs), min(ys), max(xs), max(ys))

    def fit(self):
        """
        Показать символ целиком и по центру окна. Считаем по фактическому
        содержимому, а не по корпусу: у пассивок корпуса как такового нет,
        и по нему символ уезжал за край.
        """
        x0, y0, x1, y1 = self.content_box()
        W = max(x1 - x0, 400.0)
        H = max(y1 - y0, 400.0)
        pad = MARGIN * 0.6
        kx = (self.width() - 40) / (W + 2 * pad)
        ky = (self.height() - 40) / (H + 2 * pad)
        self.zoom = max(0.02, min(4.0, min(kx, ky)))
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        self.pan = [self.width() / 2.0 - cx * self.zoom,
                    self.height() / 2.0 + cy * self.zoom]
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        self.fit()

    # ----------------------------------------------------------- ввод -------
    def wheelEvent(self, e):
        k = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        wx, wy = self.to_world(e.position())
        self.zoom = max(0.02, min(4.0, self.zoom * k))
        p = self.to_screen(wx, wy)
        self.pan[0] += e.position().x() - p.x()
        self.pan[1] += e.position().y() - p.y()
        self.update()

    def _tol(self, px: int = 14) -> float:
        """Радиус захвата в милах, соответствующий px пикселям экрана."""
        return max(HANDLE, px / max(self.zoom, 1e-6))

    def visible_pins(self) -> List[SymPin]:
        """Выводы показываемой секции (или все, если секция не выбрана)."""
        if not self.part:
            return list(self.sym.pins)
        return [p for p in self.sym.pins if int(p.unit) == self.part]

    def set_part(self, part: int):
        self.part = int(part or 0)
        self.sel = [p for p in self.sel if p in self.visible_pins()]
        self.fit()

    def assign_part(self, part: int) -> int:
        """Перенести выделенные выводы в секцию. Вернуть сколько перенесено."""
        target = self.sel or []
        for p in target:
            p.unit = max(1, int(part))
            p.manual = True
        n = len(target)
        if n:
            self.sym.part_count = max(int(self.sym.part_count or 1),
                                      max(int(p.unit) for p in self.sym.pins))
        self.update()
        return n

    def _pin_at(self, wx: float, wy: float) -> Optional[SymPin]:
        """
        Ближайший вывод. Ловим по всей длине линии вывода, а не только по
        точке у корпуса -- иначе на мелком масштабе в него не попасть.
        """
        if not self.pick_pins:
            return None
        tol = self._tol()
        best, bd = None, tol
        for p in self.visible_pins():
            out = -1 if p.side == "L" else 1
            x2 = p.x + out * (p.length or self.st.pin_length)
            lo, hi = min(p.x, x2), max(p.x, x2)
            dx = 0.0 if lo - tol <= wx <= hi + tol else min(abs(wx - lo),
                                                            abs(wx - hi))
            d = abs(p.y - wy) + dx * 0.35
            if d < bd:
                best, bd = p, d
        return best

    def _line_at(self, wx: float, wy: float) -> int:
        """Индекс линии под курсором или -1."""
        if not self.pick_lines:
            return -1
        best, bd = -1, self._tol()
        for i, ln in enumerate(self.g.user_lines):
            x1, y1, x2, y2 = ln[:4]
            dx, dy = x2 - x1, y2 - y1
            L2 = dx * dx + dy * dy
            if L2 <= 1e-9:
                continue
            t = max(0.0, min(1.0, ((wx - x1) * dx + (wy - y1) * dy) / L2))
            px, py = x1 + t * dx, y1 + t * dy
            d = ((wx - px) ** 2 + (wy - py) ** 2) ** 0.5
            if d < bd:
                best, bd = i, d
        return best

    def mousePressEvent(self, e):
        wx, wy = self.to_world(e.position())
        self._last = e.position()
        if (self.line_mode or self.rect_mode) and e.button() == Qt.LeftButton:
            # нажал -- потянул -- отпустил; обе точки прилипают к сетке
            self._line_start = (_snap(wx, self.grid), _snap(wy, self.grid))
            self._line_cur = self._line_start
            self.grab = ("newline",)
            self.update()
            return
        if e.button() == Qt.MiddleButton:
            self.grab = ("pan",)
            return
        if e.button() != Qt.LeftButton:
            return
        W, H = self.g.body_w, self.g.body_h
        tol = self._tol(8)
        # ручки границ полей и корпуса
        if not self.pick_body:
            pass
        elif abs(wx - self.g.field_l) < tol and -H < wy < 0 and self.g.field_l:
            self.grab = ("fl",)
            return
        if self.pick_body and self.g.field_r \
                and abs(wx - (W - self.g.field_r)) < tol and -H < wy < 0:
            self.grab = ("fr",)
            return
        if self.pick_body and abs(wx - W) < tol and -H < wy < 0:
            self.grab = ("w",)
            return
        if self.pick_body and abs(wy + H) < tol and 0 < wx < W:
            self.grab = ("h",)
            return
        add = bool(e.modifiers() & Qt.ControlModifier)
        li = self._line_at(wx, wy)
        if li >= 0:
            if add:
                if li in self.sel_lines:
                    self.sel_lines.remove(li)
                else:
                    self.sel_lines.append(li)
            elif li not in self.sel_lines:
                self.sel_lines = [li]
                self.sel = []
            self.grab = ("line", li)
            return
        p = self._pin_at(wx, wy)
        if p is not None:
            if add:
                if p in self.sel:
                    self.sel.remove(p)
                else:
                    self.sel.append(p)
            elif p not in self.sel:
                self.sel = [p]
                self.sel_lines = []
            self.grab = ("pin", p)
        elif e.modifiers() & Qt.ShiftModifier or not add:
            # рамка выделения
            self.rubber = (wx, wy, wx, wy)
            self.grab = ("rubber",)
        else:
            self.grab = ("pan",)

    def mouseMoveEvent(self, e):
        wx, wy = self.to_world(e.position())
        if self.grab is None:
            self.hover = self._pin_at(wx, wy)
            self.update()
            return
        kind = self.grab[0]
        if kind == "newline":
            self._line_cur = (_snap(wx, self.grid), _snap(wy, self.grid))
        elif kind == "pan":
            d = e.position() - self._last
            self.pan[0] += d.x()
            self.pan[1] += d.y()
            self._last = e.position()
        elif kind == "pin":
            lead = self.grab[1]
            group = self.sel if lead in self.sel and len(self.sel) > 1 else [lead]
            ny = _snap(wy, self.grid)
            dy = ny - lead.y
            W = self.g.body_w
            side_left = wx <= W / 2.0
            want = "L" if side_left else "R"
            # группу тоже можно перекинуть целиком: сторону берём по курсору
            flip = want != lead.side
            for p in group:
                p.manual = True
                p.y = p.y + dy if p is not lead else ny
                if flip or len(group) == 1:
                    p.side = want
                p.x = 0 if p.side == "L" else W
                p.rotation = 180 if p.side == "L" else 0
        elif kind == "rubber":
            x1, y1, _x, _y = self.rubber
            self.rubber = (x1, y1, wx, wy)
        elif kind == "line":
            i = self.grab[1]
            d = e.position() - self._last if self._last else None
            self._last = e.position()
            if d is not None:
                dxw = d.x() / self.zoom
                dyw = -d.y() / self.zoom
                idxs = self.sel_lines if i in self.sel_lines else [i]
                for j in idxs:
                    ln = self.g.user_lines[j]
                    g = self.grid
                    ln[0] = _snap(ln[0] + dxw, g)
                    ln[1] = _snap(ln[1] + dyw, g)
                    ln[2] = _snap(ln[2] + dxw, g)
                    ln[3] = _snap(ln[3] + dyw, g)
        elif kind == "fl":
            self.g.field_l = max(0, min(_snap(wx, self.grid),
                                        self.g.body_w // 2))
        elif kind == "fr":
            self.g.field_r = max(0, min(_snap(self.g.body_w - wx, self.grid),
                                        self.g.body_w // 2))
        elif kind == "w":
            self.g.body_w = max(300, _snap(wx, self.grid))
            # двигаем выводы только этой секции: у соседней свой корпус
            for p in self.visible_pins():
                if p.side == "R":
                    p.x = self.g.body_w
        elif kind == "h":
            self.g.body_h = max(200, _snap(-wy, self.grid))
        self.update()

    def mouseReleaseEvent(self, _e):
        if self.grab and self.grab[0] == "newline":
            a, b = self._line_start, self._line_cur
            if a and b and a != b:
                if self.rect_mode:
                    # прямоугольник -- четыре обычные линии: дальше их
                    # можно двигать и править поодиночке
                    x1, y1, x2, y2 = a[0], a[1], b[0], b[1]
                    for sg in ((x1, y1, x2, y1), (x2, y1, x2, y2),
                               (x2, y2, x1, y2), (x1, y2, x1, y1)):
                        self.g.user_lines.append(
                            [sg[0], sg[1], sg[2], sg[3],
                             self.new_w, self.new_col])
                else:
                    self.g.user_lines.append([a[0], a[1], b[0], b[1],
                                                self.new_w, self.new_col])
            self._line_start = self._line_cur = None
            self.grab = None
            self.update()
            return
        if self.grab and self.grab[0] == "pin" and self.auto_body:
            self.grow_body()
        if self.grab and self.grab[0] == "rubber" and self.rubber:
            x1, y1, x2, y2 = self.rubber
            lo_x, hi_x = min(x1, x2), max(x1, x2)
            lo_y, hi_y = min(y1, y2), max(y1, y2)
            if abs(hi_x - lo_x) > 40 or abs(hi_y - lo_y) > 40:
                self.sel = [p for p in self.visible_pins()
                            if lo_x - HANDLE <= p.x <= hi_x + HANDLE
                            and lo_y <= p.y <= hi_y] if self.pick_pins else []
                self.sel_lines = [
                    i for i, ln in enumerate(self.g.user_lines)
                    if lo_y <= ln[1] <= hi_y and lo_y <= ln[3] <= hi_y
                ] if self.pick_lines else []
            else:
                self.sel, self.sel_lines = [], []
        self.rubber = None
        self.grab = None
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.line_mode = False
            self.rect_mode = False
            self._line_start = None
            self.sel, self.sel_lines = [], []
        elif e.key() == Qt.Key_Delete:
            for j in sorted(self.sel_lines, reverse=True):
                if 0 <= j < len(self.g.user_lines):
                    del self.g.user_lines[j]
            self.sel_lines = []
        elif e.matches(QKeySequence.Copy):
            self.clip = [list(self.g.user_lines[j]) for j in self.sel_lines
                         if 0 <= j < len(self.g.user_lines)]
        elif e.matches(QKeySequence.Paste):
            base = len(self.g.user_lines)
            for ln in self.clip:
                self.g.user_lines.append([ln[0], ln[1] - GRID,
                                            ln[2], ln[3] - GRID])
            self.sel_lines = list(range(base, len(self.g.user_lines)))
        elif e.key() == Qt.Key_L:
            self.line_mode = not self.line_mode
            self._line_start = None
        self.update()

    def _menu(self, pos):
        wx, wy = self.to_world(QPointF(pos))
        m = QMenu(self)
        near = [y for y in self.g.dividers if abs(y - wy) < HANDLE]
        if near:
            m.addAction("Убрать разделитель",
                        lambda: self._del_divider(near[0]))
        else:
            m.addAction("Добавить разделитель здесь",
                        lambda: self._add_divider(_snap(wy, 50)))
        m.addSeparator()
        m.addAction("Подогнать корпус под выводы", self.grow_body)
        m.addAction("Ширина полей по именам", self.auto_fields)
        m.exec(self.mapToGlobal(pos))

    def _add_divider(self, y: int):
        if -self.g.body_h < y < 0 and y not in self.g.dividers:
            self.g.dividers.append(int(y))
            self.g.dividers.sort(reverse=True)
            self.update()

    def _del_divider(self, y: int):
        self.g.dividers = [d for d in self.g.dividers if d != y]
        self.update()

    # -------------------------------------------------------- операции ------
    def grow_body(self):
        """Растянуть корпус текущей секции по её же выводам."""
        pins = self.visible_pins()
        if not pins:
            return
        low = min(p.y for p in pins)
        self.g.body_h = max(self.g.body_h,
                            _snap(-low + self.st.body_margin))
        self.g.body_w = max(self.g.body_w, self.st.min_main_field)
        for p in pins:
            p.x = 0 if p.side == "L" else self.g.body_w
        self.update()

    def auto_fields(self):
        fl, fr = symbolgen.auto_fields(self.visible_pins(), self.st)
        self.g.field_l = min(fl, self.g.body_w // 3)
        self.g.field_r = min(fr, self.g.body_w // 3)
        self.update()

    def stack(self, step: int = GRID):
        """
        Собрать выделенные выводы подряд с шагом в одну клетку.

        Порядок берём тот, в котором они сейчас идут сверху вниз, начало --
        от самого верхнего. Это то, ради чего иначе пришлось бы тащить
        полсотни выводов по одному.
        """
        target = [p for p in self.sel] or list(self.sym.pins)
        if not target:
            return
        target.sort(key=lambda p: (-p.y, p.side))
        y = _snap(target[0].y)
        for p in target:
            p.y = y
            p.manual = True
            y -= step
        if self.auto_body:
            self.grow_body()
        self.update()

    def mirror_h(self):
        """Отразить выделенные (или все) выводы на другую сторону."""
        W = self.g.body_w
        target = self.sel or self.sym.pins
        for p in target:
            p.side = "R" if p.side == "L" else "L"
            p.x = 0 if p.side == "L" else W
            p.rotation = 180 if p.side == "L" else 0
            p.manual = True
        self.update()

    def mirror_v(self):
        """Отразить выделенные (или все) выводы по вертикали."""
        target = self.sel or self.sym.pins
        if not target:
            return
        ys = [p.y for p in target]
        lo, hi = min(ys), max(ys)
        for p in target:
            p.y = _snap(lo + hi - p.y)
            p.manual = True
        for ln in (self.g.user_lines if not self.sel else []):
            ln[1] = _snap(lo + hi - ln[1], 50)
            ln[3] = _snap(lo + hi - ln[3], 50)
        self.update()

    def spread(self, side: str, step: int = GRID):
        """Разложить выводы одной стороны подряд с заданным шагом."""
        pins = sorted([p for p in self.sym.pins if p.side == side],
                      key=lambda p: -p.y)
        y = -self.st.body_margin
        for p in pins:
            p.y = y
            p.manual = True
            y -= step
        if self.auto_body:
            self.grow_body()
        self.update()

    # ------------------------------------------------------- отрисовка ------
    def _pin_label_texts(self):
        """
        Тексты, которые редактор и так рисует сам, — имена и номера выводов.

        Генератор кладёт их в графику символа, но в редакторе они уже
        нарисованы рядом с самим выводом и ездят вместе с ним. Если
        показать ещё и графику, каждая подпись задваивается, причём
        вторая копия неподвижна.
        """
        names = {(p.name or "") for p in self.sym.pins if p.name}
        nums = {(p.number or "") for p in self.sym.pins if p.number}
        return names, nums

    def _is_pin_label(self, pr, names, nums) -> bool:
        if pr.kind != "text":
            return False
        t = (pr.text or "").strip()
        sz = int(pr.size or 0)
        return ((t in names and sz == int(self.st.size_pin))
                or (t in nums and sz == int(self.st.size_pin_num)))

    def _draw_prims(self, p: QPainter):
        """Сгенерированная графика символа: линии, дуги, окружности, текст."""
        names, nums = self._pin_label_texts()
        for pr in self.sym.prims or []:
            if self._is_pin_label(pr, names, nums):
                continue
            if self.part and int(pr.unit) not in (self.part, 0):
                continue
            col = _qcolor(getattr(pr, "color", -1), "#7a7a7a")
            p.setPen(QPen(col, 1 + int(pr.width or 1)))
            if pr.kind == "line" and len(pr.pts) >= 2:
                p.drawLine(self.to_screen(*pr.pts[0]),
                           self.to_screen(*pr.pts[1]))
            elif pr.kind == "rect" and len(pr.pts) >= 2:
                p.drawRect(QRectF(self.to_screen(*pr.pts[0]),
                                  self.to_screen(*pr.pts[1])))
            elif pr.kind == "poly" and len(pr.pts) >= 2:
                pts = list(pr.pts) + ([pr.pts[0]] if pr.filled else [])
                for a, b in zip(pts, pts[1:]):
                    p.drawLine(self.to_screen(*a), self.to_screen(*b))
            elif pr.kind in ("arc", "ellipse") and pr.pts:
                cx, cy = pr.pts[0]
                r = float(pr.radius or 0)
                box = QRectF(self.to_screen(cx - r, cy + r),
                             self.to_screen(cx + r, cy - r))
                if pr.kind == "ellipse":
                    p.drawEllipse(box)
                else:
                    a1 = float(pr.a1)
                    span = float(pr.a2) - a1
                    p.drawArc(box, int(a1 * 16), int(span * 16))
            elif pr.kind == "text" and pr.pts:
                f = QFont("Segoe UI",
                          max(5, int(pr.size * 1.2 * min(2.5, self.zoom * 4))))
                p.setFont(f)
                x, y = pr.pts[0]
                pt = self.to_screen(x, y)
                w = p.fontMetrics().horizontalAdvance(pr.text or "")
                if int(pr.justify) == 1:
                    pt = QPointF(pt.x() - w / 2.0, pt.y())
                elif int(pr.justify) == 2:
                    pt = QPointF(pt.x() - w, pt.y())
                p.drawText(pt, pr.text or "")

    def explode_prims(self):
        """
        Перевести сгенерированную графику в редактируемые линии.

        Прямые и ломаные переносятся один-в-один, дуги и окружности
        разбиваются на отрезки -- иначе править их мышью нечем. Текст и
        подписи выводов не трогаем: они рисуются генератором заново.
        """
        import math
        added = 0
        for pr in list(self.sym.prims or []):
            w = int(pr.width or 1)
            col = int(getattr(pr, "color", -1))
            segs: List[List[int]] = []
            if pr.kind == "line" and len(pr.pts) >= 2:
                (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
                segs.append([x1, y1, x2, y2])
            elif pr.kind == "rect" and len(pr.pts) >= 2:
                (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
                segs += [[x1, y1, x2, y1], [x2, y1, x2, y2],
                         [x2, y2, x1, y2], [x1, y2, x1, y1]]
            elif pr.kind == "poly" and len(pr.pts) >= 2:
                pts = list(pr.pts) + ([pr.pts[0]] if pr.filled else [])
                segs += [[a[0], a[1], b[0], b[1]] for a, b in zip(pts, pts[1:])]
            elif pr.kind in ("arc", "ellipse") and pr.pts:
                cx, cy = pr.pts[0]
                r = float(pr.radius or 0)
                a1 = math.radians(float(pr.a1))
                a2 = math.radians(float(pr.a2) if pr.kind == "arc" else 360.0)
                n = max(8, int(abs(a2 - a1) / math.pi * 16))
                prev = None
                for i in range(n + 1):
                    a = a1 + (a2 - a1) * i / n
                    cur = (cx + r * math.cos(a), cy + r * math.sin(a))
                    if prev:
                        segs.append([prev[0], prev[1], cur[0], cur[1]])
                    prev = cur
            for sg in segs:
                self.g.user_lines.append(
                    [int(round(sg[0])), int(round(sg[1])),
                     int(round(sg[2])), int(round(sg[3])), w, col])
                added += 1
        self.show_prims = False
        self.update()
        return added

    def set_line_style(self, width: Optional[int] = None,
                       color: Optional[int] = None):
        """Толщина и цвет выделенных линий (или будущих, если ничего нет)."""
        if width is not None:
            self.new_w = int(width)
        if color is not None:
            self.new_col = int(color)
        for j in self.sel_lines:
            if not (0 <= j < len(self.g.user_lines)):
                continue
            ln = self.g.user_lines[j]
            while len(ln) < 6:
                ln.append(1 if len(ln) == 4 else -1)
            if width is not None:
                ln[4] = int(width)
            if color is not None:
                ln[5] = int(color)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#ffffff"))
        p.setRenderHint(QPainter.Antialiasing, True)
        W, H = self.g.body_w, self.g.body_h

        # сетка
        if self.zoom > 0.06:
            p.setPen(QPen(QColor("#e8e8ee"), 1))
            g = max(self.grid, GRID if self.zoom < 0.35 else self.grid)
            x0 = int((-MARGIN) // g) * g
            while x0 < W + MARGIN:
                a = self.to_screen(x0, MARGIN)
                b = self.to_screen(x0, -H - MARGIN)
                p.drawLine(a, b)
                x0 += g
            y0 = int((-H - MARGIN) // g) * g
            while y0 < MARGIN:
                a = self.to_screen(-MARGIN, y0)
                b = self.to_screen(W + MARGIN, y0)
                p.drawLine(a, b)
                y0 += g

        # Графика символа как она есть: у пассивки это её обозначение по
        # ГОСТ, а не прямоугольник с двумя выводами. Рисуется бледнее
        # редактируемых линий -- сразу видно, что тут правится, а что нет.
        if self.show_prims:
            self._draw_prims(p)

        # корпус
        p.setPen(QPen(QColor("#000000"), 2))
        if self.pick_body or not self.sym.prims:
            p.drawRect(QRectF(self.to_screen(0, 0), self.to_screen(W, -H)))
        p.setPen(QPen(QColor("#3355cc"), 1.5))
        if self.g.field_l:
            p.drawLine(self.to_screen(self.g.field_l, 0),
                       self.to_screen(self.g.field_l, -H))
        if self.g.field_r:
            p.drawLine(self.to_screen(W - self.g.field_r, 0),
                       self.to_screen(W - self.g.field_r, -H))
        p.setPen(QPen(QColor("#3355cc"), 1.5, Qt.DashLine))
        for y in self.g.dividers:
            p.drawLine(self.to_screen(0, y), self.to_screen(W, y))
        for i, ln in enumerate(self.g.user_lines):
            w = int(ln[4]) if len(ln) > 4 else 1
            col = int(ln[5]) if len(ln) > 5 else -1
            p.setPen(QPen(QColor("#c02020") if i in self.sel_lines
                          else _qcolor(col),
                          (3 if i in self.sel_lines else 1) + w))
            p.drawLine(self.to_screen(ln[0], ln[1]),
                       self.to_screen(ln[2], ln[3]))

        # выводы
        f = QFont("Segoe UI", max(6, int(9 * min(2.0, self.zoom * 4))))
        p.setFont(f)
        for pin in self.visible_pins():
            out = -1 if pin.side == "L" else 1
            x2 = pin.x + out * (pin.length or self.st.pin_length)
            sel = pin in self.sel or pin is self.hover
            p.setPen(QPen(QColor("#c02020" if sel else "#000000"),
                          3 if sel else 1.5))
            p.drawLine(self.to_screen(pin.x, pin.y), self.to_screen(x2, pin.y))
            p.setPen(QColor("#606060"))
            n = self.to_screen((pin.x + x2) / 2.0, pin.y + 30)
            p.drawText(n, pin.number or "")
            p.setPen(QColor("#000000"))
            tx = self.to_screen(pin.x + out * -1 * 60, pin.y - 30)
            if pin.side == "L":
                p.drawText(tx, pin.name or "")
            else:
                w = p.fontMetrics().horizontalAdvance(pin.name or "")
                p.drawText(QPointF(tx.x() - w, tx.y()), pin.name or "")

        if self.rubber:
            x1, y1, x2, y2 = self.rubber
            p.setPen(QPen(QColor("#3355cc"), 1, Qt.DashLine))
            p.setBrush(QColor(51, 85, 204, 30))
            p.drawRect(QRectF(self.to_screen(x1, y1), self.to_screen(x2, y2)))
            p.setBrush(Qt.NoBrush)
        if self._line_start and self._line_cur:
            p.setPen(QPen(QColor("#c02020"), 2, Qt.DashLine))
            if self.rect_mode:
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(self.to_screen(*self._line_start),
                                  self.to_screen(*self._line_cur)))
            else:
                p.drawLine(self.to_screen(*self._line_start),
                           self.to_screen(*self._line_cur))
        if self.line_mode or self.rect_mode:
            p.setPen(QColor("#c02020"))
            p.drawText(8, 18,
                       ("режим прямоугольника" if self.rect_mode
                        else "режим линии")
                       + ": зажмите ЛКМ и протяните, Esc — выйти")
        p.setPen(QColor("#888"))
        mm = 0.0254
        p.drawText(8, self.height() - 8,
                   f"выделено выводов {len(self.sel)}, линий "
                   f"{len(self.sel_lines)}   корпус "
                   f"{W * mm:.2f}×{H * mm:.2f} мм   "
                   "ЛКМ — выбор и перетаскивание, Ctrl — добавить, "
                   "рамка — группа, L — линия, Del — удалить, Ctrl+C/V")


class SymbolEditor(QDialog):
    """Диалог редактора: холст плюс операции над раскладкой."""

    def __init__(self, comp: Component, st: Style, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Редактирование УГО — {comp.name}")
        self.resize(1000, 720)
        self.comp = copy.deepcopy(comp)
        self.st = st
        sym = self.comp.symbol
        # Габариты заводим каждой секции отдельно и по её собственным
        # выводам: общие размеры и были причиной того, что растянутый
        # корпус одной секции растягивал остальные.
        nparts = max(int(sym.part_count or 1),
                     max([int(p.unit or 1) for p in sym.pins] or [1]))
        sym.part_count = nparts
        for u in range(1, nparts + 1):
            g = sym.live(u)
            mine = [p for p in sym.pins if int(p.unit or 1) == u]
            if not g.body_w or not g.body_h:
                xs = [p.x for p in mine] or [0]
                ys = [p.y for p in mine] or [0]
                g.body_w = max(max(xs), st.min_main_field)
                g.body_h = max(-min(ys) + st.body_margin, st.pin_pitch)
            if not (g.field_l or g.field_r):
                fl, fr = symbolgen.auto_fields(mine, st)
                g.field_l, g.field_r = fl, fr

        self.canvas = Canvas(self.comp, st)

        self.cb_auto = QCheckBox("подгонять корпус под выводы")
        self.cb_auto.setChecked(True)
        self.cb_auto.stateChanged.connect(
            lambda: setattr(self.canvas, "auto_body", self.cb_auto.isChecked()))

        # шаг задаём в миллиметрах: сетка ЕСКД -- 2.54 мм
        self.sp_pitch = QSpinBox()
        self.sp_pitch.setRange(100, 1000)
        self.sp_pitch.setSingleStep(100)
        self.sp_pitch.setValue(st.eff_pitch())
        self.sp_pitch.setSuffix(" mil")
        self.sp_pitch.setToolTip("Шаг раскладки. Утилита уже подняла его до "
                                 f"{st.eff_pitch()} mil "
                                 f"({st.eff_pitch() * 0.0254:.2f} мм), чтобы "
                                 "подписи не наезжали.")

        # Что разрешено цеплять мышью. При массовом выделении легко задеть
        # не то: рисуешь линию -- уезжает вывод, тянешь рамку -- уезжает
        # корпус.
        self.cb_pick_pins = QCheckBox("выводы")
        self.cb_pick_lines = QCheckBox("линии")
        self.cb_pick_body = QCheckBox("корпус")
        for cb, attr in ((self.cb_pick_pins, "pick_pins"),
                         (self.cb_pick_lines, "pick_lines"),
                         (self.cb_pick_body, "pick_body")):
            cb.setChecked(True)
            cb.setToolTip("Снимите, чтобы это не выделялось и не таскалось")
            cb.stateChanged.connect(
                lambda _v, a=attr, c=cb: setattr(self.canvas, a, c.isChecked()))

        # толщина и цвет линий
        self.sp_lw = QSpinBox()
        self.sp_lw.setRange(1, 3)
        self.sp_lw.setValue(1)
        self.sp_lw.setToolTip("Толщина линии: 1 — тонкая (по ГОСТ), 3 — жирная")
        self.sp_lw.valueChanged.connect(
            lambda v: self.canvas.set_line_style(width=v))
        self.btn_col = QPushButton("Цвет линии")
        self.btn_col.setToolTip("Цвет выделенных линий; по умолчанию — общий "
                                "цвет графики из настроек")
        self.btn_col.clicked.connect(self._pick_color)
        self.btn_explode = QPushButton("Разобрать графику")
        self.btn_explode.setToolTip(
            "Перевести обозначение элемента в обычные линии, чтобы править "
            "их мышью. Дуги при этом станут ломаными.")
        self.btn_explode.clicked.connect(self._explode)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("шаг:"))
        bar.addWidget(self.sp_pitch)
        self.btn_line = QPushButton("Линия (L)")
        self.btn_line.setCheckable(True)
        self.btn_line.setToolTip("Нарисовать свою линию: клик — начало, "
                                 "клик — конец. Линии выделяются, таскаются, "
                                 "копируются Ctrl+C / Ctrl+V, удаляются Del.")
        self.btn_line.clicked.connect(self._line_mode)
        bar.addWidget(self.btn_line)
        self.btn_rect = QPushButton("Прямоугольник")
        self.btn_rect.setCheckable(True)
        self.btn_rect.setToolTip(
            "Нарисовать рамку: она станет четырьмя обычными линиями, "
            "каждую можно двигать и править отдельно.")
        self.btn_rect.clicked.connect(self._rect_mode)
        bar.addWidget(self.btn_rect)
        for txt, fn in (
                ("Разложить слева", lambda: self.canvas.spread("L", self.sp_pitch.value())),
                ("Разложить справа", lambda: self.canvas.spread("R", self.sp_pitch.value())),
                ("Собрать в столбик", lambda: self.canvas.stack(GRID)),
                ("Отразить ↔", self.canvas.mirror_h),
                ("Отразить ↕", self.canvas.mirror_v),
                ("Подогнать корпус", self.canvas.grow_body),
                ("Поля по именам", self.canvas.auto_fields),
                ("По окну", self.canvas.fit)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(self.cb_auto)

        # Секции (Part в Altium): у сборок вроде LM358 логично разнести
        # каналы по секциям, а питание держать в отдельной.
        self.cb_part = QComboBox()
        self.cb_part.setToolTip("Показать одну секцию символа")
        self._fill_parts()
        self.cb_part.currentIndexChanged.connect(
            lambda: self.canvas.set_part(self.cb_part.currentData() or 0))
        self.sp_part = QSpinBox()
        self.sp_part.setRange(1, 26)
        self.sp_part.setPrefix("в секцию ")
        self.b_part = QPushButton("Перенести выделенные")
        self.b_part.setToolTip("Выделенные выводы уйдут в указанную секцию")
        self.b_part.clicked.connect(self._assign_part)

        self.cb_grid = QComboBox()
        for mil_, label in ((100, "2.54 мм (ЕСКД)"), (50, "1.27 мм"),
                            (25, "0.635 мм"), (10, "0.254 мм")):
            self.cb_grid.addItem(label, mil_)
        self.cb_grid.setToolTip(
            "Шаг привязки. Обозначения из KiCad часто нарисованы на 50 и "
            "25 mil — на крупной сетке их форма разъезжается.")
        self.cb_grid.currentIndexChanged.connect(
            lambda: setattr(self.canvas, "grid",
                            self.cb_grid.currentData() or GRID))
        # для родного обозначения сразу мелкая сетка
        if getattr(comp, "symbol_source", "gost") == "native":
            self.cb_grid.setCurrentIndex(2)
            self.canvas.grid = 25

        bar2 = QHBoxLayout()
        bar2.addWidget(QLabel("шаг:"))
        bar2.addWidget(self.cb_grid)
        bar2.addSpacing(12)
        bar2.addWidget(QLabel("секция:"))
        bar2.addWidget(self.cb_part)
        bar2.addWidget(self.sp_part)
        bar2.addWidget(self.b_part)
        bar2.addSpacing(16)
        bar2.addWidget(QLabel("выделять:"))
        bar2.addWidget(self.cb_pick_pins)
        bar2.addWidget(self.cb_pick_lines)
        bar2.addWidget(self.cb_pick_body)
        bar2.addSpacing(16)
        bar2.addWidget(QLabel("толщина:"))
        bar2.addWidget(self.sp_lw)
        bar2.addWidget(self.btn_col)
        bar2.addSpacing(16)
        bar2.addWidget(self.btn_explode)
        bar2.addStretch(1)

        self.btn_reset = QPushButton("Вернуть автоматическую раскладку")
        self.btn_reset.setToolTip(
            "Символ снова будет собираться автоматом по ГОСТ, "
            "ручные позиции выводов при этом теряются")

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.btn_reset)
        row.addStretch(1)
        row.addWidget(bb)
        self.btn_reset.clicked.connect(self._reset)
        self._do_reset = False

        # Дерево секций слева. Разносить выводы по частям вслепую нельзя:
        # надо видеть, что в какой секции лежит, и переключаться между
        # ними одним щелчком.
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Секции и выводы", "№"])
        self.tree.setColumnWidth(0, 150)
        self.tree.setMinimumWidth(210)
        self.tree.itemSelectionChanged.connect(self._tree_pick)
        self.sp_nparts = QSpinBox()
        self.sp_nparts.setRange(1, 26)
        self.sp_nparts.setPrefix("секций: ")
        self.sp_nparts.setValue(max(1, int(self.comp.symbol.part_count or 1)))
        self.sp_nparts.setToolTip(
            "Сколько частей у символа. Сначала задайте число, потом "
            "разложите по ним выводы.")
        self.sp_nparts.setKeyboardTracking(False)
        self.sp_nparts.valueChanged.connect(self._set_nparts)

        side = QWidget()
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.addWidget(self.sp_nparts)
        sv.addWidget(self.tree, 1)
        b_tosec = QPushButton("Выделенные → в выбранную секцию")
        b_tosec.clicked.connect(self._assign_to_tree_part)
        sv.addWidget(b_tosec)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(side)
        split.addWidget(self.canvas)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 800])

        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addLayout(bar2)
        lay.addWidget(split, 1)
        lay.addLayout(row)
        self._fill_tree()

    def _fill_tree(self):
        """Перечитать дерево секций."""
        self.tree.blockSignals(True)
        self.tree.clear()
        pins = self.comp.symbol.pins
        units = sorted({int(p.unit) for p in pins}
                       | set(range(1, int(self.sp_nparts.value()) + 1)))
        for u in units:
            mine = sorted([p for p in pins if int(p.unit) == u],
                          key=lambda q: (-q.y, q.x))
            node = QTreeWidgetItem([f"Секция {u}", str(len(mine))])
            node.setData(0, Qt.UserRole, ("part", u))
            for p in mine:
                it = QTreeWidgetItem([p.name or "(без имени)", p.number])
                it.setData(0, Qt.UserRole, ("pin", p.number))
                node.addChild(it)
            self.tree.addTopLevelItem(node)
            node.setExpanded(len(units) <= 3)
        self.tree.blockSignals(False)
        self._fill_parts()

    def _tree_part(self) -> int:
        """Секция, выбранная в дереве (или та, чей вывод выбран)."""
        it = self.tree.currentItem()
        while it is not None:
            data = it.data(0, Qt.UserRole)
            if data and data[0] == "part":
                return int(data[1])
            it = it.parent()
        return 0

    def _tree_pick(self):
        it = self.tree.currentItem()
        if it is None:
            return
        data = it.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == "part":
            self.canvas.set_part(int(data[1]))
            i = self.cb_part.findData(int(data[1]))
            if i >= 0:
                self.cb_part.blockSignals(True)
                self.cb_part.setCurrentIndex(i)
                self.cb_part.blockSignals(False)
        else:
            pin = next((p for p in self.comp.symbol.pins
                        if p.number == data[1]), None)
            if pin is not None:
                self.canvas.sel = [pin]
                self.canvas.update()

    def _set_nparts(self, n: int):
        """
        Задать число секций.

        Выводы из исчезнувших секций возвращаются в первую -- потерять их
        молча нельзя: в Altium это выводы, которых просто не станет.
        """
        n = max(1, int(n))
        moved = 0
        for p in self.comp.symbol.pins:
            if int(p.unit) > n:
                p.unit = 1
                moved += 1
        self.comp.symbol.part_count = n
        # геометрия исчезнувших секций больше не нужна -- иначе она
        # всплывёт, если секций снова станет больше
        self.comp.symbol.drop_parts_above(n)
        self._fill_tree()
        self._fill_parts()
        self.canvas.update()
        if moved:
            QMessageBox.information(
                self, "GostLib",
                f"Секций стало {n}. Выводов возвращено в первую: {moved}.")

    def _assign_to_tree_part(self):
        u = self._tree_part()
        if not u:
            QMessageBox.information(self, "GostLib",
                                    "Выберите секцию в дереве слева.")
            return
        n = self.canvas.assign_part(u)
        if not n:
            QMessageBox.information(self, "GostLib",
                                    "Сначала выделите выводы на холсте.")
            return
        self.sp_nparts.blockSignals(True)
        self.sp_nparts.setValue(max(self.sp_nparts.value(),
                                    int(self.comp.symbol.part_count or 1)))
        self.sp_nparts.blockSignals(False)
        self._fill_tree()

    def _fill_parts(self):
        """
        Список секций. Секции перечисляем по `part_count`, а не по тем, где
        уже есть выводы: пустая секция должна быть видна, иначе в неё
        нечего перетаскивать.

        Пункт «все» есть только у односекционного символа. У многосекционного
        он вреден: габариты у каждой секции свои, и правка «во всех сразу»
        означала бы растянуть корпус одной секции размером другой -- ровно
        то, из-за чего секции и слипались.
        """
        cur = self.cb_part.currentData() if self.cb_part.count() else 0
        sym = self.comp.symbol
        n_parts = max(int(sym.part_count or 1),
                      max([int(p.unit or 1) for p in sym.pins] or [1]))
        self.cb_part.blockSignals(True)
        self.cb_part.clear()
        if n_parts <= 1:
            self.cb_part.addItem("все", 0)
        for u in range(1, n_parts + 1):
            n = sum(1 for p in sym.pins if int(p.unit or 1) == u)
            self.cb_part.addItem(f"{u} ({n} выв.)", u)
        want = cur if (cur and cur <= n_parts) else (0 if n_parts <= 1 else 1)
        i = self.cb_part.findData(want)
        self.cb_part.setCurrentIndex(i if i >= 0 else 0)
        self.cb_part.blockSignals(False)
        want = self.cb_part.currentData() or 0
        if int(getattr(self.canvas, "part", 0)) != int(want):
            self.canvas.set_part(want)

    def _assign_part(self):
        n = self.canvas.assign_part(self.sp_part.value())
        if not n:
            QMessageBox.information(self, "GostLib",
                                    "Сначала выделите выводы.")
            return
        self._fill_tree()
        QMessageBox.information(
            self, "GostLib",
            f"Выводов перенесено в секцию {self.sp_part.value()}: {n}.\n"
            f"Всего секций у символа: {self.comp.symbol.part_count}.")

    def _pick_color(self):
        cur = _qcolor(self.canvas.new_col)
        c = QColorDialog.getColor(cur, self, "Цвет линии")
        if not c.isValid():
            return
        # Altium держит цвет целым BGR
        self.canvas.set_line_style(
            color=c.red() | (c.green() << 8) | (c.blue() << 16))

    def _explode(self):
        n = self.canvas.explode_prims()
        if not n:
            QMessageBox.information(self, "GostLib",
                                    "Разбирать нечего: графики нет.")
            return
        QMessageBox.information(
            self, "GostLib",
            f"Графика разобрана на {n} линий — теперь их можно двигать, "
            f"менять толщину и цвет.")

    def _rect_mode(self):
        self.canvas.rect_mode = self.btn_rect.isChecked()
        if self.canvas.rect_mode:
            self.btn_line.setChecked(False)
            self.canvas.line_mode = False
        self.canvas._line_start = None
        self.canvas.setFocus()
        self.canvas.update()

    def _line_mode(self):
        self.canvas.line_mode = self.btn_line.isChecked()
        if self.canvas.line_mode:
            self.btn_rect.setChecked(False)
            self.canvas.rect_mode = False
        self.canvas._line_start = None
        self.canvas.setFocus()
        self.canvas.update()

    def _reset(self):
        self._do_reset = True
        self.accept()

    def result_symbol(self):
        """(manual_layout, символ) — что применить к компоненту."""
        if self._do_reset:
            return (False, None)
        sym = self.comp.symbol
        sym.manual_layout = True
        for p in sym.pins:
            p.manual = True
        return (True, sym)
