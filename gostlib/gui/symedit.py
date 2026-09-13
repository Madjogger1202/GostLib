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
import math
import os
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QKeySequence,
                           QPainter, QPen, QPolygonF)
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox,
                               QDialog, QSplitter, QTreeWidget,
                               QTreeWidgetItem,
                               QDialogButtonBox, QHBoxLayout, QLabel, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QSizePolicy, QSpinBox,
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


def _seg_dist(px: float, py: float, x1: float, y1: float,
              x2: float, y2: float) -> float:
    """Расстояние от точки до отрезка -- попадание мышью по контуру."""
    dx, dy = x2 - x1, y2 - y1
    d2 = dx * dx + dy * dy
    if d2 <= 1e-9:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / d2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


class PinPlanDialog(QDialog):
    """
    Раскладка выводов чужими руками: задание для ИИ и приём ответа.

    Окно устроено в два поля намеренно. Слева -- задание, целиком готовое
    к вставке в любой чат: правила ЕСКД, формат ответа и данные. Справа --
    место под ответ. Раньше поле было одно, и человек либо затирал
    задание ответом, либо вставлял ответ в конец задания.
    """

    def __init__(self, comp: Component, parent=None):
        super().__init__(parent)
        from ..pinplan import build_prompt

        self.comp = comp
        self.rows = None
        self.datasheet = ""
        self.original = build_prompt(comp)
        self.setWindowTitle(f"ИИ-раскладка выводов — {comp.name}")

        note = QLabel(
            "1. «Копировать задание» → вставьте в любой чат с ИИ.  "
            "2. Ответ вставьте справа.  3. «Проверить и применить».\n"
            "Ответ принимается в CSV; забор из ``` и болтовню вокруг "
            "таблицы разбор выбрасывает сам, колонки ищутся по именам в "
            "шапке — их порядок не важен.")
        note.setWordWrap(True)

        self.edit = QPlainTextEdit(self.original)
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText(
            "Сюда — ответ ИИ.\n\n"
            "number,name,etype,unit,side,group\n"
            "1,VDD,power,1,L,PWR\n"
            "2,GND,power,1,L,GND\n…")
        for w in (self.edit, self.answer):
            w.setFont(QFont("Consolas", 10))
            w.setLineWrapMode(QPlainTextEdit.NoWrap)
        panes = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("Задание для ИИ"))
        lv.addWidget(self.edit, 1)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("Ответ ИИ"))
        rv.addWidget(self.answer, 1)
        panes.addWidget(left)
        panes.addWidget(right)
        panes.setSizes([600, 560])

        copy_btn = QPushButton("Копировать задание")
        copy_btn.setToolTip("Весь текст задания уйдёт в буфер обмена")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self.edit.toPlainText()))
        ds_btn = QPushButton("Приложить datasheet…")
        ds_btn.setToolTip(
            "Из PDF берутся страницы с описанием выводов и добавляются в "
            "задание.\nБез документации ИИ судит по одним номерам и "
            "именам — с ней имена и группы получаются заметно точнее.")
        ds_btn.clicked.connect(self._pick_datasheet)
        paste_btn = QPushButton("Вставить ответ из буфера")
        paste_btn.clicked.connect(
            lambda: self.answer.setPlainText(QApplication.clipboard().text()))
        restore_btn = QPushButton("Пересобрать задание")
        restore_btn.clicked.connect(self._rebuild)
        apply_btn = QPushButton("Проверить и применить")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self._validate)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(self.reject)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#888;")
        self.cb_tidy = QCheckBox("привести в порядок после ИИ")
        self.cb_tidy.setChecked(True)
        self.cb_tidy.setToolTip(
            "Разнести питание и землю по своим секциям, выровнять стороны\n"
            "и разбить слишком длинные столбики. Модель этого не умеет: она\n"
            "не знает ни размера листа, ни того, что у BGA бывает двести\n"
            "пятьдесят земель. Классификация остаётся её, компоновка — наша.")

        buttons = QHBoxLayout()
        buttons.addWidget(self.cb_tidy)
        buttons.addWidget(copy_btn)
        buttons.addWidget(ds_btn)
        buttons.addWidget(paste_btn)
        buttons.addWidget(restore_btn)
        buttons.addStretch(1)
        buttons.addWidget(apply_btn)
        buttons.addWidget(cancel_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(panes, 1)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        from .widgets import allow_narrow, fit_to_screen
        allow_narrow(self)
        fit_to_screen(self, 1180, 760)

    def _rebuild(self):
        from ..pinplan import build_prompt
        self.edit.setPlainText(build_prompt(self.comp, self.datasheet))

    def _pick_datasheet(self):
        from PySide6.QtWidgets import QFileDialog
        from ..pinplan import datasheet_text
        path, _ = QFileDialog.getOpenFileName(
            self, "Документация на компонент", "", "PDF (*.pdf)")
        if not path:
            return
        try:
            self.datasheet = datasheet_text(path)
        except Exception as exc:                            # noqa: BLE001
            QMessageBox.warning(self, "Документация не прочиталась", str(exc))
            return
        self._rebuild()
        self.status.setText(
            f"К заданию добавлено {len(self.datasheet)} знаков из "
            f"{os.path.basename(path)}")

    def _validate(self):
        from ..pinplan import PinPlanError, parse_plan, tidy

        text = self.answer.toPlainText().strip()
        if not text:
            QMessageBox.information(
                self, "Пусто",
                "Вставьте ответ ИИ в правое поле.")
            return
        try:
            rows = parse_plan(text, self.comp.raw_pins or self.comp.symbol.pins)
        except PinPlanError as exc:
            QMessageBox.warning(self, "Раскладка не применена", str(exc))
            return
        # Классификацию сделала модель, компоновку доводим сами: ни одна
        # модель не знает, что двести пятьдесят земель в один столбик --
        # это два листа A3 в высоту.
        if self.cb_tidy.isChecked():
            rows, notes = tidy(rows)
            if notes:
                QMessageBox.information(
                    self, "Раскладка приведена в порядок",
                    "Что поправлено после ИИ:\n\n  • "
                    + "\n  • ".join(notes[:10]))
        self.rows = rows
        self.accept()


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
        # Инструмент рисования: '' -- обычная работа мышью, иначе
        # 'line' | 'rect' | 'circle' | 'arc' | 'poly'. Раньше это были два
        # отдельных флага; с пятью фигурами так уже нельзя.
        self.tool = ""
        self._line_start = None
        self._line_cur = None
        self._poly_pts: List[Tuple[float, float]] = []   # набор ломаной
        self._drag_from = (0.0, 0.0)      # точка нажатия при таскании фигур
        self._drag_orig: Dict[int, List[List[float]]] = {}
        self.sel_shapes: List[int] = []   # индексы выделенных фигур
        self.snap_ends = True             # прилипать к концам линий и выводам
        self.clip: List[List[int]] = []   # буфер обмена линий
        self.clip_shapes: List[Dict] = []  # буфер обмена фигур
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

    # --------------------------------------------------------- шрифт --------
    def _text_font(self, size_pt: float) -> QFont:
        """
        Шрифт для текста символа В МИРОВЫХ единицах.

        Раньше кегль здесь считался от масштаба экрана («Segoe UI» размером
        `size * 1.2 * zoom`), то есть к тому, что получится в Altium, не
        имел отношения вовсе: при разном зуме подписи занимали разную долю
        корпуса, и подогнать их на глаз было невозможно.

        Теперь высота считается так же, как в превью и в задании: кегль в
        пунктах -> милы -> пиксели экрана через текущий зум. Множитель
        `preview_font_scale` -- поправка на то, что Altium отмеряет текст
        по высоте прописной буквы, а Qt и SVG -- по полной высоте кегля.
        """
        from ..gost.style import PT2MIL
        k = float(getattr(self.st, "preview_font_scale", 0) or 0.72)
        px = float(size_pt) * PT2MIL * k * self.zoom
        f = QFont(self.st.font or "GOST type B")
        # ниже шести пикселей Qt рисует кашу -- на мелком зуме подпись
        # перестаёт быть пропорциональной, зато остаётся читаемой
        f.setPixelSize(max(6, int(round(px))))
        return f

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

    # ------------------------------------------------------- фигуры ---------
    @property
    def shapes(self) -> List[Dict]:
        """Фигуры текущей секции (дуги, окружности, полигоны)."""
        return self.g.user_shapes

    @property
    def line_mode(self) -> bool:
        return self.tool == "line"

    @property
    def rect_mode(self) -> bool:
        return self.tool == "rect"

    # Кому сообщить, что инструмент сменился (Esc на холсте тоже его
    # выключает, и кнопки в панели должны отжаться сами).
    on_tool_change = None

    def set_tool(self, name: str):
        """Включить инструмент рисования; повторное нажатие выключает."""
        self.tool = "" if self.tool == name else name
        self._line_start = self._line_cur = None
        self._poly_pts = []
        if callable(self.on_tool_change):
            self.on_tool_change(self.tool)
        self.update()

    def _snap_pt(self, wx: float, wy: float) -> Tuple[int, int]:
        """
        Точка с привязкой: сначала к концам уже нарисованного, потом к
        сетке. Без прилипания к концам фигуры не стыкуются: на глаз в
        милах не попасть, а щель в пару милов в Altium видно.
        """
        if self.snap_ends:
            tol = self._tol(10)
            best, bd = None, tol
            for ln in self.g.user_lines:
                for px, py in ((ln[0], ln[1]), (ln[2], ln[3])):
                    d = math.hypot(px - wx, py - wy)
                    if d < bd:
                        best, bd = (px, py), d
            for sh in self.shapes:
                for p_ in (sh.get("pts") or []):
                    d = math.hypot(p_[0] - wx, p_[1] - wy)
                    if d < bd:
                        best, bd = (p_[0], p_[1]), d
            for pin in self.visible_pins():
                d = math.hypot(pin.x - wx, pin.y - wy)
                if d < bd:
                    best, bd = (pin.x, pin.y), d
            if best:
                return int(round(best[0])), int(round(best[1]))
        return _snap(wx, self.grid), _snap(wy, self.grid)

    def shape_points(self, sh: Dict) -> List[Tuple[float, float]]:
        """Ломаная, которой фигура рисуется и проверяется на попадание."""
        kind = sh.get("kind")
        pts = [(float(p[0]), float(p[1])) for p in (sh.get("pts") or [])]
        if kind == "rect" and len(pts) >= 2:
            (x1, y1), (x2, y2) = pts[0], pts[1]
            return [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
        if kind in ("circle", "arc") and pts:
            cx, cy = pts[0]
            r = float(sh.get("r", 0) or 0)
            a1 = math.radians(float(sh.get("a1", 0.0)))
            a2 = math.radians(float(sh.get("a2", 360.0))
                              if kind == "arc" else 360.0)
            n = max(12, int(abs(a2 - a1) / math.pi * 24))
            return [(cx + r * math.cos(a1 + (a2 - a1) * i / n),
                     cy + r * math.sin(a1 + (a2 - a1) * i / n))
                    for i in range(n + 1)]
        if kind == "poly" and pts:
            return pts + ([pts[0]] if sh.get("close") and len(pts) > 2 else [])
        return pts

    def _shape_at(self, wx: float, wy: float) -> int:
        """Индекс фигуры под курсором (по контуру), иначе -1."""
        tol = self._tol(10)
        best, bd = -1, tol
        for i, sh in enumerate(self.shapes):
            pts = self.shape_points(sh)
            for a, b in zip(pts, pts[1:]):
                d = _seg_dist(wx, wy, a[0], a[1], b[0], b[1])
                if d < bd:
                    best, bd = i, d
        return best

    def move_shape(self, i: int, dx: float, dy: float):
        sh = self.shapes[i]
        sh["pts"] = [[p[0] + dx, p[1] + dy] for p in (sh.get("pts") or [])]

    def shape_box(self, sh: Dict):
        pts = self.shape_points(sh)
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def mousePressEvent(self, e):
        wx, wy = self.to_world(e.position())
        self._last = e.position()
        if self.tool == "poly" and e.button() == Qt.LeftButton:
            self._poly_pts.append(self._snap_pt(wx, wy))
            self.update()
            return
        if self.tool and self.tool != "poly" and e.button() == Qt.LeftButton:
            # нажал -- потянул -- отпустил; обе точки прилипают к сетке
            self._line_start = self._snap_pt(wx, wy)
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
        si = self._shape_at(wx, wy) if self.pick_lines else -1
        if si >= 0:
            if add:
                if si in self.sel_shapes:
                    self.sel_shapes.remove(si)
                else:
                    self.sel_shapes.append(si)
            elif si not in self.sel_shapes:
                self.sel_shapes = [si]
                self.sel, self.sel_lines = [], []
            self._drag_from = (wx, wy)
            self._drag_orig = {
                j: [list(p) for p in (self.shapes[j].get("pts") or [])]
                for j in (self.sel_shapes if si in self.sel_shapes else [si])
                if 0 <= j < len(self.shapes)}
            self.grab = ("shape", si)
            return
        li = self._line_at(wx, wy)
        if li >= 0:
            if add:
                if li in self.sel_lines:
                    self.sel_lines.remove(li)
                else:
                    self.sel_lines.append(li)
            elif li not in self.sel_lines:
                self.sel_lines = [li]
                self.sel, self.sel_shapes = [], []
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
            self._line_cur = self._snap_pt(wx, wy)
        elif kind == "shape":
            # Смещение считаем ОТ ТОЧКИ НАЖАТИЯ, а не от предыдущего
            # кадра: если округлять каждый шаг мыши к сетке, движения
            # меньше шага теряются и фигура ползёт медленнее курсора.
            i = self.grab[1]
            ox, oy = self._drag_from
            dx = _snap(wx - ox, self.grid)
            dy = _snap(wy - oy, self.grid)
            for j, orig in self._drag_orig.items():
                if 0 <= j < len(self.shapes):
                    self.shapes[j]["pts"] = [[p[0] + dx, p[1] + dy]
                                             for p in orig]
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

    def add_shape(self, kind: str, **kw) -> int:
        """Добавить фигуру текущей секции и вернуть её индекс."""
        sh = {"kind": kind, "w": self.new_w, "col": self.new_col}
        sh.update(kw)
        self.shapes.append(sh)
        return len(self.shapes) - 1

    def finish_poly(self):
        """Закончить набор ломаной (двойной клик, Enter или ПКМ)."""
        pts = [list(p) for p in self._poly_pts]
        self._poly_pts = []
        if len(pts) >= 2:
            i = self.add_shape("poly", pts=pts, close=0, fill=0)
            self.sel_shapes = [i]
        self.update()

    def mouseDoubleClickEvent(self, e):
        if self.tool == "poly":
            self.finish_poly()
            return
        super().mouseDoubleClickEvent(e)

    def mouseReleaseEvent(self, _e):
        if self.grab and self.grab[0] == "newline":
            a, b = self._line_start, self._line_cur
            if a and b and a != b:
                x1, y1, x2, y2 = a[0], a[1], b[0], b[1]
                if self.tool == "rect":
                    # прямоугольник -- четыре обычные линии: дальше их
                    # можно двигать и править поодиночке
                    for sg in ((x1, y1, x2, y1), (x2, y1, x2, y2),
                               (x2, y2, x1, y2), (x1, y2, x1, y1)):
                        self.g.user_lines.append(
                            [sg[0], sg[1], sg[2], sg[3],
                             self.new_w, self.new_col])
                elif self.tool in ("circle", "arc"):
                    # тянем от центра к краю: радиус -- длина протяжки,
                    # округлённая к сетке, иначе окружность не сядет на
                    # координаты выводов
                    r = _snap(math.hypot(x2 - x1, y2 - y1), self.grid)
                    if r >= self.grid:
                        if self.tool == "circle":
                            i = self.add_shape("circle", pts=[[x1, y1]], r=r,
                                               fill=0)
                        else:
                            # дуга по умолчанию верхняя половина: углы
                            # правятся в панели, там же их видно числом
                            i = self.add_shape("arc", pts=[[x1, y1]], r=r,
                                               a1=0.0, a2=180.0)
                        self.sel_shapes = [i]
                        self.sel, self.sel_lines = [], []
                else:
                    self.g.user_lines.append([x1, y1, x2, y2,
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
                self.sel_shapes = [] if not self.pick_lines else [
                    i for i, sh in enumerate(self.shapes)
                    if (lambda bx: bx and lo_x <= bx[0] and bx[2] <= hi_x
                        and lo_y <= bx[1] and bx[3] <= hi_y)(
                            self.shape_box(sh))]
            else:
                self.sel, self.sel_lines, self.sel_shapes = [], [], []
        self.rubber = None
        self.grab = None
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.tool = ""
            self._line_start = None
            self._poly_pts = []
            self.sel, self.sel_lines, self.sel_shapes = [], [], []
            if callable(self.on_tool_change):
                self.on_tool_change("")
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter) and self._poly_pts:
            self.finish_poly()
            return
        elif e.key() == Qt.Key_Delete:
            for j in sorted(self.sel_lines, reverse=True):
                if 0 <= j < len(self.g.user_lines):
                    del self.g.user_lines[j]
            self.sel_lines = []
            for j in sorted(self.sel_shapes, reverse=True):
                if 0 <= j < len(self.shapes):
                    del self.shapes[j]
            self.sel_shapes = []
        elif e.matches(QKeySequence.Copy):
            self.clip = [list(self.g.user_lines[j]) for j in self.sel_lines
                         if 0 <= j < len(self.g.user_lines)]
            self.clip_shapes = [copy.deepcopy(self.shapes[j])
                                for j in self.sel_shapes
                                if 0 <= j < len(self.shapes)]
        elif e.matches(QKeySequence.Paste):
            self._paste(GRID)
        elif e.key() == Qt.Key_D and (e.modifiers() & Qt.ControlModifier):
            # дубликат выделенного рядом -- быстрее, чем копировать и
            # вставлять двумя горячими клавишами
            self.clip = [list(self.g.user_lines[j]) for j in self.sel_lines
                         if 0 <= j < len(self.g.user_lines)]
            self.clip_shapes = [copy.deepcopy(self.shapes[j])
                                for j in self.sel_shapes
                                if 0 <= j < len(self.shapes)]
            self._paste(GRID)
        elif e.key() == Qt.Key_L:
            self.set_tool("line")
            return
        self.update()

    def _paste(self, off: int):
        """Вставить линии и фигуры из буфера со сдвигом на шаг сетки."""
        base = len(self.g.user_lines)
        for ln in self.clip:
            row = list(ln)
            while len(row) < 6:
                row.append(1 if len(row) == 4 else -1)
            row[1] -= off
            row[3] -= off
            self.g.user_lines.append(row)
        self.sel_lines = list(range(base, len(self.g.user_lines)))
        base_s = len(self.shapes)
        for sh in self.clip_shapes:
            cp = copy.deepcopy(sh)
            cp["pts"] = [[p[0], p[1] - off] for p in (cp.get("pts") or [])]
            self.shapes.append(cp)
        self.sel_shapes = list(range(base_s, len(self.shapes)))

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
                p.setFont(self._text_font(pr.size))
                x, y = pr.pts[0]
                pt = self.to_screen(x, y)
                w = p.fontMetrics().horizontalAdvance(pr.text or "")
                if int(pr.justify) == 1:
                    pt = QPointF(pt.x() - w / 2.0, pt.y())
                elif int(pr.justify) == 2:
                    pt = QPointF(pt.x() - w, pt.y())
                p.drawText(pt, pr.text or "")

    def _draw_shapes(self, p: QPainter):
        """Дуги, окружности и полигоны редактора."""
        for i, sh in enumerate(self.shapes):
            sel = i in self.sel_shapes
            w = int(sh.get("w", 1) or 1)
            pen = QPen(QColor("#c02020") if sel
                       else _qcolor(int(sh.get("col", -1))),
                       (3 if sel else 1) + w)
            p.setPen(pen)
            if sh.get("fill"):
                c = _qcolor(int(sh.get("col", -1)))
                c.setAlpha(60)
                p.setBrush(QBrush(c))
            else:
                p.setBrush(Qt.NoBrush)
            kind = sh.get("kind")
            pts = sh.get("pts") or []
            if kind in ("circle", "arc") and pts:
                cx, cy = pts[0][0], pts[0][1]
                r = float(sh.get("r", 0) or 0)
                box = QRectF(self.to_screen(cx - r, cy + r),
                             self.to_screen(cx + r, cy - r))
                if kind == "circle":
                    p.drawEllipse(box)
                else:
                    a1 = float(sh.get("a1", 0.0))
                    p.drawArc(box, int(a1 * 16),
                              int((float(sh.get("a2", 360.0)) - a1) * 16))
            elif kind == "rect" and len(pts) >= 2:
                p.drawRect(QRectF(self.to_screen(pts[0][0], pts[0][1]),
                                  self.to_screen(pts[1][0], pts[1][1])))
            elif kind == "poly" and len(pts) >= 2:
                poly = QPolygonF([self.to_screen(q[0], q[1])
                                  for q in self.shape_points(sh)])
                if sh.get("fill") and sh.get("close"):
                    p.drawPolygon(poly)
                else:
                    p.drawPolyline(poly)
            p.setBrush(Qt.NoBrush)

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

    # -------------------------------------------- выравнивание и стиль ------
    def _sel_objects(self):
        """
        Выделенное как список «объектов» с общим интерфейсом.

        Каждый элемент -- (габарит, функция сдвига). Так одна и та же
        математика выравнивания работает и для выводов, и для линий, и
        для фигур: иначе пришлось бы писать три почти одинаковых куска.
        """
        out = []
        for pin in self.sel:
            def mv(dx, dy, p=pin):
                p.manual = True
                p.x = int(round(p.x + dx))
                p.y = int(round(p.y + dy))
            out.append(((pin.x, pin.y, pin.x, pin.y), mv))
        for j in self.sel_lines:
            if not (0 <= j < len(self.g.user_lines)):
                continue
            ln = self.g.user_lines[j]

            def mvl(dx, dy, l=ln):
                l[0] += dx
                l[1] += dy
                l[2] += dx
                l[3] += dy
            out.append(((min(ln[0], ln[2]), min(ln[1], ln[3]),
                         max(ln[0], ln[2]), max(ln[1], ln[3])), mvl))
        for j in self.sel_shapes:
            if not (0 <= j < len(self.shapes)):
                continue
            box = self.shape_box(self.shapes[j])
            if not box:
                continue

            def mvs(dx, dy, k=j):
                self.move_shape(k, dx, dy)
            out.append((box, mvs))
        return out

    def align(self, how: str) -> int:
        """
        Выровнять выделенное: left|right|top|bottom|cx|cy.

        Ровняем по крайнему объекту выделения (как в любом векторном
        редакторе), координаты остаются на сетке.
        """
        objs = self._sel_objects()
        if len(objs) < 2:
            return 0
        xs1 = [o[0][0] for o in objs]
        ys1 = [o[0][1] for o in objs]
        xs2 = [o[0][2] for o in objs]
        ys2 = [o[0][3] for o in objs]
        for (x1, y1, x2, y2), mv in objs:
            dx = dy = 0.0
            if how == "left":
                dx = min(xs1) - x1
            elif how == "right":
                dx = max(xs2) - x2
            elif how == "top":
                dy = max(ys2) - y2
            elif how == "bottom":
                dy = min(ys1) - y1
            elif how == "cx":
                dx = (min(xs1) + max(xs2)) / 2.0 - (x1 + x2) / 2.0
            elif how == "cy":
                dy = (min(ys1) + max(ys2)) / 2.0 - (y1 + y2) / 2.0
            if dx or dy:
                mv(_snap(dx, self.grid) if how in ("left", "right") else dx,
                   _snap(dy, self.grid) if how in ("top", "bottom") else dy)
        self.update()
        return len(objs)

    def distribute(self, axis: str) -> int:
        """Разложить выделенное с равными промежутками по X или по Y."""
        objs = self._sel_objects()
        if len(objs) < 3:
            return 0
        k = 0 if axis == "x" else 1
        objs.sort(key=lambda o: (o[0][k] + o[0][k + 2]) / 2.0)
        first = (objs[0][0][k] + objs[0][0][k + 2]) / 2.0
        last = (objs[-1][0][k] + objs[-1][0][k + 2]) / 2.0
        step = (last - first) / (len(objs) - 1)
        for i, ((x1, y1, x2, y2), mv) in enumerate(objs):
            want = first + step * i
            cur = ((x1 + x2) / 2.0) if axis == "x" else ((y1 + y2) / 2.0)
            d = _snap(want - cur, self.grid)
            if d:
                mv(d, 0) if axis == "x" else mv(0, d)
        self.update()
        return len(objs)

    def copy_style(self) -> int:
        """
        Стиль первого выделенного объекта -- всем остальным выделенным.

        Первым считается объект, выбранный раньше: в списках выделения
        порядок как раз такой.
        """
        src = None
        if self.sel_lines:
            ln = self.g.user_lines[self.sel_lines[0]]
            src = (int(ln[4]) if len(ln) > 4 else 1,
                   int(ln[5]) if len(ln) > 5 else -1)
        elif self.sel_shapes:
            sh = self.shapes[self.sel_shapes[0]]
            src = (int(sh.get("w", 1) or 1), int(sh.get("col", -1)))
        if not src:
            return 0
        w, col = src
        n = 0
        for j in self.sel_lines[1:]:
            ln = self.g.user_lines[j]
            while len(ln) < 6:
                ln.append(1 if len(ln) == 4 else -1)
            ln[4], ln[5] = w, col
            n += 1
        for j in self.sel_shapes if self.sel_lines else self.sel_shapes[1:]:
            self.shapes[j]["w"] = w
            self.shapes[j]["col"] = col
            n += 1
        self.new_w, self.new_col = w, col
        self.update()
        return n

    def set_arc_angles(self, a1: float, a2: float) -> int:
        """Углы выделенных дуг (в градусах, против часовой)."""
        n = 0
        for j in self.sel_shapes:
            if 0 <= j < len(self.shapes) and self.shapes[j].get("kind") == "arc":
                self.shapes[j]["a1"] = float(a1)
                self.shapes[j]["a2"] = float(a2)
                n += 1
        self.update()
        return n

    def toggle_fill(self) -> int:
        """Заливка у выделенных фигур (у окружности и замкнутой ломаной)."""
        n = 0
        for j in self.sel_shapes:
            if 0 <= j < len(self.shapes):
                sh = self.shapes[j]
                sh["fill"] = 0 if sh.get("fill") else 1
                if sh.get("kind") == "poly":
                    sh["close"] = 1
                n += 1
        self.update()
        return n

    def rotate_selection(self, deg: float = 90.0) -> int:
        """Повернуть выделенные линии и фигуры вокруг центра выделения."""
        objs = self._sel_objects()
        if not objs:
            return 0
        xs = [v for o in objs for v in (o[0][0], o[0][2])]
        ys = [v for o in objs for v in (o[0][1], o[0][3])]
        cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
        a = math.radians(deg)
        ca, sa = math.cos(a), math.sin(a)

        def rot(x, y):
            dx, dy = x - cx, y - cy
            return (_snap(cx + dx * ca - dy * sa, self.grid),
                    _snap(cy + dx * sa + dy * ca, self.grid))

        n = 0
        for j in self.sel_lines:
            if not (0 <= j < len(self.g.user_lines)):
                continue
            ln = self.g.user_lines[j]
            ln[0], ln[1] = rot(ln[0], ln[1])
            ln[2], ln[3] = rot(ln[2], ln[3])
            n += 1
        for j in self.sel_shapes:
            if not (0 <= j < len(self.shapes)):
                continue
            sh = self.shapes[j]
            if sh.get("kind") == "rect" and len(sh.get("pts") or []) >= 2:
                # Повёрнутый прямоугольник Altium не умеет: разворачиваем
                # его в ломаную ДО поворота, иначе после поворота углы
                # снова сложились бы в осевой прямоугольник и поворот
                # молча пропал бы.
                (x1, y1), (x2, y2) = sh["pts"][0], sh["pts"][1]
                sh["kind"] = "poly"
                sh["close"] = 1
                sh["pts"] = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            sh["pts"] = [list(rot(p[0], p[1])) for p in (sh.get("pts") or [])]
            if sh.get("kind") == "arc":
                sh["a1"] = float(sh.get("a1", 0.0)) + deg
                sh["a2"] = float(sh.get("a2", 360.0)) + deg
            n += 1
        self.update()
        return n

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
        for j in self.sel_shapes:
            if not (0 <= j < len(self.shapes)):
                continue
            if width is not None:
                self.shapes[j]["w"] = int(width)
            if color is not None:
                self.shapes[j]["col"] = int(color)
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
        self._draw_shapes(p)

        # Выводы. Имена и номера редактор рисует сам (сгенерированные
        # подписи из графики отфильтрованы), поэтому кегль берём тот же,
        # что уйдёт в Altium, -- иначе в редакторе подписи выглядят одного
        # размера, а на схеме другого.
        f_num = self._text_font(self.st.size_pin_num)
        f_name = self._text_font(self.st.size_pin)
        for pin in self.visible_pins():
            out = -1 if pin.side == "L" else 1
            x2 = pin.x + out * (pin.length or self.st.pin_length)
            sel = pin in self.sel or pin is self.hover
            p.setPen(QPen(QColor("#c02020" if sel else "#000000"),
                          3 if sel else 1.5))
            p.drawLine(self.to_screen(pin.x, pin.y), self.to_screen(x2, pin.y))
            p.setPen(QColor("#606060"))
            p.setFont(f_num)
            n = self.to_screen((pin.x + x2) / 2.0, pin.y + 30)
            p.drawText(n, pin.number or "")
            p.setPen(QColor("#000000"))
            p.setFont(f_name)
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
            p.setBrush(Qt.NoBrush)
            a, b = self._line_start, self._line_cur
            if self.tool == "rect":
                p.drawRect(QRectF(self.to_screen(*a), self.to_screen(*b)))
            elif self.tool in ("circle", "arc"):
                r = math.hypot(b[0] - a[0], b[1] - a[1])
                box = QRectF(self.to_screen(a[0] - r, a[1] + r),
                             self.to_screen(a[0] + r, a[1] - r))
                if self.tool == "circle":
                    p.drawEllipse(box)
                else:
                    p.drawArc(box, 0, 180 * 16)
                p.drawLine(self.to_screen(*a), self.to_screen(*b))
            else:
                p.drawLine(self.to_screen(*a), self.to_screen(*b))
        if self._poly_pts:
            p.setPen(QPen(QColor("#c02020"), 2, Qt.DashLine))
            p.drawPolyline(QPolygonF([self.to_screen(q[0], q[1])
                                      for q in self._poly_pts]))
        if self.tool:
            hint = {"line": "линия", "rect": "прямоугольник",
                    "circle": "окружность: от центра к краю",
                    "arc": "дуга: от центра к краю, углы — в панели",
                    "poly": "ломаная: клики по вершинам, "
                            "двойной клик или Enter — закончить",
                    }.get(self.tool, self.tool)
            p.setPen(QColor("#c02020"))
            p.drawText(8, 18, f"{hint};  Esc — выйти")
        p.setPen(QColor("#888"))
        mm = 0.0254
        p.drawText(8, self.height() - 8,
                   f"выделено: выводов {len(self.sel)}, линий "
                   f"{len(self.sel_lines)}, фигур {len(self.sel_shapes)}   "
                   f"корпус {W * mm:.2f}×{H * mm:.2f} мм   "
                   "ЛКМ — выбор и перетаскивание, Ctrl — добавить, "
                   "рамка — группа, Del — удалить, Ctrl+C/V, Ctrl+D — дубль")


class SymbolEditor(QDialog):
    """Диалог редактора: холст плюс операции над раскладкой."""

    def __init__(self, comp: Component, st: Style, parent=None):
        super().__init__(parent)
        self._title = f"Редактирование УГО — {comp.name}"
        self.setWindowTitle(self._title)
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
        self.canvas.on_tool_change = lambda t: self._sync_tools(t)

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
        # Инструменты рисования. Кнопки взаимоисключающие: включённый
        # инструмент один, иначе клик по холсту непонятно что делает.
        self.tool_btns = {}
        for name, txt, tip in (
                ("line", "Линия (L)",
                 "Зажмите ЛКМ и протяните. Линии выделяются, таскаются, "
                 "копируются Ctrl+C / Ctrl+V, удаляются Del."),
                ("rect", "Прямоугольник",
                 "Рамка станет четырьмя обычными линиями — каждую можно "
                 "двигать и править отдельно."),
                ("circle", "Окружность",
                 "От центра к краю: радиус округляется к шагу сетки."),
                ("arc", "Дуга",
                 "От центра к краю. Углы правятся числом справа — "
                 "по умолчанию верхняя половина."),
                ("poly", "Ломаная",
                 "Клики по вершинам, двойной клик или Enter — закончить. "
                 "Кнопка «Заливка» замыкает её в полигон.")):
            b = QPushButton(txt)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, k=name: self._tool(k))
            self.tool_btns[name] = b
            bar.addWidget(b)
        self.btn_line = self.tool_btns["line"]
        self.btn_rect = self.tool_btns["rect"]
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

        # Третий ряд -- работа с выделенным: выравнивание, распределение,
        # заливка, поворот, углы дуги. Всё это одинаково относится и к
        # выводам, и к линиям, и к фигурам.
        bar3 = QHBoxLayout()
        bar3.addWidget(QLabel("выровнять:"))
        for txt, how, tip in (("◧", "left", "по левому краю"),
                              ("◨", "right", "по правому краю"),
                              ("⌶", "cx", "по вертикальной оси"),
                              ("⎺", "top", "по верхнему краю"),
                              ("⎽", "bottom", "по нижнему краю"),
                              ("⌷", "cy", "по горизонтальной оси")):
            b = QPushButton(txt)
            b.setFixedWidth(30)
            b.setToolTip(f"Выровнять выделенное {tip}")
            b.clicked.connect(lambda _=False, h=how: self._align(h))
            bar3.addWidget(b)
        bar3.addSpacing(10)
        bar3.addWidget(QLabel("разложить поровну:"))
        for txt, ax in (("по X", "x"), ("по Y", "y")):
            b = QPushButton(txt)
            b.setToolTip("Равные промежутки между выделенными объектами "
                         "(от трёх штук)")
            b.clicked.connect(lambda _=False, a=ax: self._distribute(a))
            bar3.addWidget(b)
        bar3.addSpacing(10)
        b_fill = QPushButton("Заливка")
        b_fill.setToolTip("Залить выделенные фигуры; ломаная при этом "
                          "замыкается в полигон")
        b_fill.clicked.connect(lambda: self.canvas.toggle_fill())
        bar3.addWidget(b_fill)
        b_rot = QPushButton("Повернуть 90°")
        b_rot.setToolTip("Повернуть выделенные линии и фигуры вокруг "
                         "центра выделения")
        b_rot.clicked.connect(lambda: self.canvas.rotate_selection(90.0))
        bar3.addWidget(b_rot)
        b_style = QPushButton("Стиль по образцу")
        b_style.setToolTip("Толщина и цвет первого выделенного объекта — "
                           "всем остальным выделенным")
        b_style.clicked.connect(self._copy_style)
        bar3.addWidget(b_style)
        bar3.addSpacing(10)
        bar3.addWidget(QLabel("дуга, °:"))
        self.sp_a1 = QSpinBox()
        self.sp_a1.setRange(-360, 360)
        self.sp_a1.setValue(0)
        self.sp_a2 = QSpinBox()
        self.sp_a2.setRange(-360, 720)
        self.sp_a2.setValue(180)
        for w_ in (self.sp_a1, self.sp_a2):
            w_.setFixedWidth(70)
            w_.setToolTip("Углы дуги против часовой стрелки: 0° — вправо, "
                          "90° — вверх")
        b_arc = QPushButton("Задать")
        b_arc.clicked.connect(self._apply_arc)
        bar3.addWidget(self.sp_a1)
        bar3.addWidget(self.sp_a2)
        bar3.addWidget(b_arc)
        bar3.addSpacing(10)
        self.cb_snap = QCheckBox("привязка к концам")
        self.cb_snap.setChecked(True)
        self.cb_snap.setToolTip(
            "Новая точка прилипает к концу линии, вершине фигуры или "
            "выводу рядом — иначе стык на глаз не поймать, а щель в "
            "пару милов в Altium видно.")
        self.cb_snap.toggled.connect(
            lambda v: setattr(self.canvas, "snap_ends", bool(v)))
        bar3.addWidget(self.cb_snap)
        bar3.addStretch(1)

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
        lay.addLayout(bar3)
        lay.addWidget(split, 1)
        lay.addLayout(row)
        # Три ряда кнопок и дерево секций дают минимум шире монитора --
        # снимаем его и открываемся по рабочей области того же экрана,
        # а не «как получится».
        from .widgets import allow_narrow, fit_to_screen
        allow_narrow(self, extra=(self.canvas,))
        self.canvas.setMinimumSize(320, 240)
        fit_to_screen(self, 1180, 780)
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

    def _tool(self, name: str):
        """Включить инструмент рисования, остальные кнопки отжать."""
        self.canvas.set_tool(name)
        self._sync_tools(self.canvas.tool)
        self.canvas.setFocus()

    def _sync_tools(self, active: str):
        for k, b in self.tool_btns.items():
            b.setChecked(k == active)

    def _align(self, how: str):
        n = self.canvas.align(how)
        if not n:
            self._say("Выделите хотя бы два объекта — выводы, линии "
                      "или фигуры")

    def _distribute(self, axis: str):
        n = self.canvas.distribute(axis)
        if not n:
            self._say("Разложить с равными промежутками можно от трёх "
                      "объектов")

    def _copy_style(self):
        n = self.canvas.copy_style()
        self._say(f"Стиль применён к {n} объектам" if n else
                  "Выделите сначала образец, потом (с Ctrl) остальные")

    def _apply_arc(self):
        n = self.canvas.set_arc_angles(self.sp_a1.value(), self.sp_a2.value())
        if not n:
            self._say("Выделите дугу — углы задаются ей")

    def _say(self, text: str):
        """Короткая подсказка в заголовке окна редактора."""
        self.setWindowTitle(f"{self._title} — {text}" if text else self._title)

    def _rect_mode(self):
        self._tool("rect")

    def _line_mode(self):
        self._tool("line")

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
