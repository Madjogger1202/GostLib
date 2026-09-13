"""
Генератор схемных символов по ЕСКД/ГОСТ.

build(component) -> Symbol
    Символ строится заново по списку выводов, графика источника игнорируется.
    Микросхемы  -- прямоугольник с основным и дополнительными полями
                   (ГОСТ 2.743), имена выводов внутри, номера снаружи.
    Резисторы   -- прямоугольник 10x4 мм (ГОСТ 2.728).
    Разъёмы     -- таблица «Цепь | Конт.» (ГОСТ 2.702, п. «таблица выводов»).
    Дискретные  -- по ГОСТ 2.728/2.730.
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

from .. import classify
from ..ir import Component, SymPin, SymPrim, Symbol
from .style import (CTRL_NAMES, DEFAULT, GND_NAMES, GRID, POWER_MARKS,
                    PWR_NAMES, Style, snap_up)

_NUM_SUFFIX = re.compile(r"^(.*?)(\d+)$")
_PIN_NUM = re.compile(r"^([A-Za-z]*)(\d+)$")


# ------------------------------------------------------------- утилиты -------

def _line(x1, y1, x2, y2, w=1, unit=1) -> SymPrim:
    return SymPrim(kind="line", pts=[[x1, y1], [x2, y2]], width=w, unit=unit)


def _rect(x1, y1, x2, y2, w=1, filled=False, unit=1) -> SymPrim:
    return SymPrim(kind="rect", pts=[[x1, y1], [x2, y2]], width=w,
                   filled=filled, unit=unit)


def _poly(pts, w=1, filled=False, unit=1) -> SymPrim:
    return SymPrim(kind="poly", pts=[list(p) for p in pts], width=w,
                   filled=filled, unit=unit)


def shape_prims(shapes, lw: int = 1, unit: int = 1) -> List[SymPrim]:
    """
    Фигуры из редактора символа -> примитивы.

    Общая точка для обеих веток генератора (односекционной и
    посекционной) и для превью: иначе дуга, нарисованная в редакторе,
    жила бы только на холсте, а в Altium не доезжала.
    """
    out: List[SymPrim] = []
    for sh in (shapes or []):
        if not isinstance(sh, dict):
            continue
        kind = str(sh.get("kind") or "")
        pts = [[float(p[0]), float(p[1])] for p in (sh.get("pts") or [])
               if len(p) >= 2]
        w = int(sh.get("w", lw) or lw)
        col = int(sh.get("col", -1))
        fill = bool(sh.get("fill"))
        pr: Optional[SymPrim] = None
        if kind == "circle" and pts:
            pr = _ellipse(pts[0][0], pts[0][1], float(sh.get("r", 0) or 0),
                          w, fill, unit=unit)
        elif kind == "arc" and pts:
            pr = _arc(pts[0][0], pts[0][1], float(sh.get("r", 0) or 0),
                      float(sh.get("a1", 0.0)), float(sh.get("a2", 360.0)),
                      w, unit=unit)
        elif kind == "rect" and len(pts) >= 2:
            pr = _rect(pts[0][0], pts[0][1], pts[1][0], pts[1][1], w,
                       fill, unit=unit)
        elif kind == "poly" and len(pts) >= 2:
            p2 = list(pts)
            if sh.get("close") and len(p2) > 2:
                p2.append(list(p2[0]))
            pr = _poly(p2, w, fill, unit=unit)
        if pr is None:
            continue
        if col != -1:
            pr.color = col
        out.append(pr)
    return out


def _arc(cx, cy, r, a1, a2, w=1, unit=1) -> SymPrim:
    return SymPrim(kind="arc", pts=[[cx, cy]], radius=r, a1=a1, a2=a2,
                   width=w, unit=unit)


def _ellipse(cx, cy, r, w=1, filled=False, unit=1) -> SymPrim:
    return SymPrim(kind="ellipse", pts=[[cx, cy]], radius=r, width=w,
                   filled=filled, unit=unit)


def _text(x, y, s, size, just=0, vjust=1, rot=0, unit=1) -> SymPrim:
    return SymPrim(kind="text", pts=[[x, y]], text=s, size=size,
                   justify=just, vjustify=vjust, rotation=rot, unit=unit)


def _norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9_+\-]", "", (name or "").upper())


def is_power(p: SymPin) -> bool:
    n = _norm(p.name)
    return p.etype == "power" and not is_gnd(p) or n in PWR_NAMES


def is_gnd(p: SymPin) -> bool:
    n = _norm(p.name)
    if n in GND_NAMES:
        return True
    return n.startswith("GND") or n.startswith("VSS") or n in ("EP", "EPAD")


def _pin_index(name: str) -> int:
    """Первое число в имени вывода: 'GPIO26/ADC0' -> 26, 'SDA' -> -1."""
    m = re.search(r"\d+", name or "")
    return int(m.group(0)) if m else -1


def _group_of(name: str) -> Tuple[str, int]:
    """
    Логическая группа вывода и его номер внутри группы.
    'GPIO26/ADC0' -> ('GPIO', 26);  'QSPI_SD0' -> ('QSPI', 0);
    'USB_DM' -> ('USB', -1);        'SWCLK' -> ('SWCLK', -1)
    """
    n = (name or "").strip()
    if not n:
        return ("", -1)
    idx = _pin_index(n)
    m = re.match(r"^([^0-9]*)", n)
    base = (m.group(1) if m else n).strip("_/-.")
    if "_" in base:
        base = base.split("_")[0]
    if not base:
        base = n
    return (base, idx)


def _pin_sort_key(p: SymPin):
    m = _PIN_NUM.match((p.number or "").strip())
    if m:
        return (m.group(1), int(m.group(2)))
    return (p.number or "", 0)


# -------------------------------------------------- автораскладка выводов ----

def assign_sides(pins: List[SymPin], rules=None) -> None:
    """
    Проставить side/group там, где они не заданы вручную.

    Сначала работают правила из настроек (их правит человек), потом --
    встроенная эвристика для всего, что под правила не подошло.
    """
    if rules:
        from . import pingroups
        pingroups.apply(pins, rules)
    for p in pins:
        if p.group:
            continue
        n = _norm(p.name)
        base = _group_of(p.name)[0] or "MISC"
        if is_gnd(p):
            p.side, p.group = "L", "90_GND"
        elif p.etype == "power" or n in PWR_NAMES:
            p.side, p.group = "L", "00_PWR"
        elif n in CTRL_NAMES or any(n.startswith(c) for c in
                                    ("NRST", "RESET", "SWD", "JTAG", "OSC", "XTAL")):
            p.side, p.group = "L", "10_" + base
        elif p.etype == "input":
            p.side, p.group = "L", "20_" + base
        elif p.etype in ("output", "open_collector", "open_emitter"):
            p.side, p.group = "R", "50_" + base
        else:
            p.side, p.group = "R", "60_" + base
    _balance(pins)


_FIXED_SIDE_RANKS = ("00", "10", "20", "90")


def _balance(pins: List[SymPin], max_skew: int = 3) -> None:
    """
    Выровнять число выводов слева и справа: свободные группы (выходы и
    двунаправленные) переносятся на менее загруженную сторону, а очень
    большая группа делится пополам.
    """
    # Сторону, заданную правилом («!» в начале группы), не пересматриваем:
    # человек написал её явно.
    fixed = [p for p in pins
             if (p.group or "").startswith("!")
             or (p.group or "")[:2] in _FIXED_SIDE_RANKS]
    fixed_ids = {id(p) for p in fixed}
    free = [p for p in pins if id(p) not in fixed_ids]
    if not free:
        return
    groups: Dict[str, List[SymPin]] = {}
    for p in free:
        groups.setdefault(p.group, []).append(p)

    nl = sum(1 for p in fixed if p.side == "L")
    nr = sum(1 for p in fixed if p.side == "R")
    total = nl + nr + len(free)

    # очень большую группу делим пополам
    for g, items in list(groups.items()):
        if len(items) > total * 0.45 and len(items) >= 8:
            items.sort(key=lambda p: (_group_of(p.name)[1], _pin_sort_key(p)))
            half = len(items) // 2
            groups[g] = items[:half]
            groups[g + "b"] = items[half:]
            for p in items[half:]:
                p.group = g + "b"

    for g, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        k = len(items)
        # сторона выбирается так, чтобы перекос стал минимальным
        # (при равенстве -- справа, как принято для выходов)
        side = "R" if abs((nr + k) - nl) <= abs((nl + k) - nr) else "L"
        for p in items:
            p.side = side
        if side == "L":
            nl += k
        else:
            nr += k

    # если половинки разделённой группы оказались на одной стороне -- склеить
    by_group: Dict[str, List[SymPin]] = {}
    for p in free:
        by_group.setdefault(p.group, []).append(p)
    for g in list(by_group):
        if g.endswith("b") and g[:-1] in by_group:
            a, bb = by_group[g[:-1]], by_group[g]
            if a and bb and a[0].side == bb[0].side:
                for p in bb:
                    p.group = g[:-1]


def _ordered_rows(pins: List[SymPin], st: Style) -> List[Optional[SymPin]]:
    """Строки одной стороны: пины, разделённые None (зазор между группами)."""
    if not pins:
        return []
    groups: Dict[str, List[SymPin]] = {}
    order: List[str] = []
    for p in pins:
        g = p.group or ""
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(p)
    order.sort()
    rows: List[Optional[SymPin]] = []
    prev_big = None
    prev_rank = None
    gap = max(0, int(getattr(st, "group_gap_rows", 1)))
    for i, g in enumerate(order):
        items = groups[g]
        items.sort(key=lambda p: (p.order, _group_of(p.name)[1], _pin_sort_key(p)))
        big = len(items) > 1
        rank = g[:2] if g[:2].isdigit() else ""
        # Зазор ставим вокруг «настоящих» групп, а ещё обязательно на смене
        # ранга: питание, управление, ввод, вывод и земля должны читаться
        # отдельными блоками, даже если в каждом по одному выводу.
        need = i and (big or prev_big or (rank and rank != prev_rank))
        if need:
            for _ in range(gap):
                rows.append(None)
        rows.extend(items)
        prev_big = big
        prev_rank = rank
    return rows


# ---------------------------------------------------------- микросхема -------

def build_box(comp: Component, st: Style, unit: int = 1,
              label: str = "") -> Tuple[List[SymPrim], List[SymPin], int, int]:
    pins = [p for p in comp.symbol.pins if p.unit == unit]
    assign_sides(pins, getattr(st, "_pin_rules", None))
    left = [p for p in pins if p.side == "L"]
    right = [p for p in pins if p.side == "R"]
    by_rule = any((p.group or "").startswith("!") for p in pins)
    if not left and right and not by_rule:
        # односторонний символ обычно получается случайно -- разносим
        # пополам. Но если стороны заданы правилами, это осознанный выбор.
        left, right = right[:len(right) // 2], right[len(right) // 2:]
        for p in left:
            p.side = "L"
    rows_l = _ordered_rows(left, st)
    rows_r = _ordered_rows(right, st)
    nrows = max(len(rows_l), len(rows_r), 1)

    lw = snap_up(max([st.text_w(p.name, st.size_pin) for p in left] or [0])
                 + 2 * st.text_pad)
    rw = snap_up(max([st.text_w(p.name, st.size_pin) for p in right] or [0])
                 + 2 * st.text_pad)
    lw = max(lw, st.min_side_field if left else 0)
    rw = max(rw, st.min_side_field if right else 0)
    if st.compact_symbols and left and right:
        # Поля остаются одинаковыми, но их ширина выше уже рассчитана по
        # реальной узкой метрике Altium (0.32 em, а не прежние 0.48 em).
        # Поэтому партномер находится строго по центру без прежнего
        # полуторакратного раздувания обеих сторон.
        lw = rw = max(lw, rw)
    label = label or classify.label_for(comp)
    label_pad = GRID if st.compact_symbols else 2 * st.text_pad
    mw = max(st.min_main_field,
             snap_up(st.text_w(label, st.size_type) + label_pad))
    if not st.ic_fields:
        lw = lw or st.min_side_field
        rw = rw or st.min_side_field
    W = lw + mw + rw
    pitch = st.eff_pitch()
    H = 2 * st.body_margin + (nrows - 1) * pitch

    prims: List[SymPrim] = [_rect(0, 0, W, -H, st.lw_body, unit=unit)]
    if st.ic_fields:
        if lw:
            prims.append(_line(lw, 0, lw, -H, st.lw_inner, unit=unit))
        if rw:
            prims.append(_line(lw + mw, 0, lw + mw, -H, st.lw_inner, unit=unit))

    out_pins: List[SymPin] = []
    for i, p in enumerate(rows_l):
        y = -st.body_margin - i * pitch
        if p is None:
            continue
        p.x, p.y, p.rotation = 0, y, 180
        p.length = st.pin_length
        p.show_name = False          # имя рисуем текстом внутри поля
        p.show_number = st.show_pin_numbers
        out_pins.append(p)
        if p.name:
            prims.append(_text(st.text_pad, y, p.name, st.size_pin, just=0,
                               unit=unit))
    for i, p in enumerate(rows_r):
        y = -st.body_margin - i * pitch
        if p is None:
            continue
        p.x, p.y, p.rotation = W, y, 0
        p.length = st.pin_length
        p.show_name = False
        p.show_number = st.show_pin_numbers
        out_pins.append(p)
        if p.name:
            prims.append(_text(W - st.text_pad, y, p.name, st.size_pin, just=2,
                               unit=unit))
    return prims, out_pins, W, H


# ------------------------------------------------------ разъём таблицей ------

def build_connector(comp: Component, st: Style, unit: int = 1
                    ) -> Tuple[List[SymPrim], List[SymPin], int, int]:
    pins = [p for p in comp.symbol.pins if p.unit == unit]
    # Порядок, заданный руками в таблице выводов («!» в группе), важнее
    # общей настройки сортировки: у разъёмов он и есть смысл правки.
    # Достаточно ОДНОГО вывода, помеченного вручную: раньше требовалось,
    # чтобы «!» стояло у всех, и у разъёма (где сторона в таблице пустая)
    # условие не выполнялось -- порядок откатывался к сортировке по
    # номеру, и правка «применялась» только со второго раза.
    manual = [p for p in pins if getattr(p, "manual", False)
              or (p.group or "").startswith("!")]
    if manual:
        pins = sorted(pins, key=lambda q: (q.order, _pin_sort_key(q)))
    elif st.table_sort == "number":
        pins = sorted(pins, key=_pin_sort_key)
    n = max(1, len(pins))
    cells = [(p.name if st.table_cell_source == "name" and p.name else p.number)
             for p in pins]
    cw_pin = max(st.table_min_col_pin,
                 snap_up(max([st.text_w(c, st.size_table) for c in cells] or [0])
                         + 2 * st.text_pad))
    cw_net = st.table_col_net
    W = cw_net + cw_pin
    H = st.table_header + n * st.table_row

    prims: List[SymPrim] = [
        _rect(0, 0, W, -H, st.lw_body, unit=unit),
        _line(cw_net, 0, cw_net, -H, st.lw_inner, unit=unit),
        _line(0, -st.table_header, W, -st.table_header, st.lw_inner, unit=unit),
        _text(cw_net / 2, -st.table_header / 2, st.table_head_net,
              st.size_table, just=1, unit=unit),
        _text(cw_net + cw_pin / 2, -st.table_header / 2, st.table_head_pin,
              st.size_table, just=1, unit=unit),
    ]
    out_pins: List[SymPin] = []
    for i, p in enumerate(pins):
        y = -st.table_header - i * st.table_row - st.table_row // 2
        p.x, p.y, p.rotation = W, y, 0
        p.length = st.pin_length
        p.show_name = False
        p.show_number = st.show_pin_numbers
        p.side = "R"
        out_pins.append(p)
        prims.append(_text(cw_net + cw_pin / 2, y, cells[i], st.size_table,
                           just=1, unit=unit))
        if st.table_row_lines and i:
            yy = -st.table_header - i * st.table_row
            prims.append(_line(0, yy, W, yy, st.lw_inner, unit=unit))
    return prims, out_pins, W, H


# ------------------------------------------------------ дискретные -----------

def _two_pin(comp: Component, st: Style, body_w: int, unit: int,
             draw, vertical: bool = False):
    """Каркас двухвыводного элемента. draw(x0,x1,prims) рисует тело."""
    pins = sorted([p for p in comp.symbol.pins if p.unit == unit],
                  key=_pin_sort_key)[:2]
    lead = st.s_lead
    x0, x1 = lead, lead + body_w
    total = body_w + 2 * lead
    prims: List[SymPrim] = []
    draw(x0, x1, prims)
    out: List[SymPin] = []
    if len(pins) >= 1:
        p = pins[0]
        p.x, p.y, p.rotation, p.length = x0, 0, 180, lead
        p.show_name, p.show_number = False, st.show_pin_numbers
        out.append(p)
    if len(pins) >= 2:
        p = pins[1]
        p.x, p.y, p.rotation, p.length = x1, 0, 0, lead
        p.show_name, p.show_number = False, st.show_pin_numbers
        out.append(p)
    return prims, out, total, 0


def _draw_resistor(comp, st, unit):
    def draw(x0, x1, prims):
        h = st.s_res_h // 2
        prims.append(_rect(x0, h, x1, -h, st.lw_body, unit=unit))
        if st.show_power_marks:
            mark = _power_mark(comp)
            if mark:
                prims.extend(_power_glyph(mark, (x0 + x1) / 2, 0, h, unit))
    return _two_pin(comp, st, st.s_res_w, unit, draw)


def _power_mark(comp: Component) -> str:
    raw = (comp.params.get("Power") or "").replace(",", ".")
    m = re.search(r"([\d.]+)", raw)
    if not m:
        return ""
    try:
        w = float(m.group(1))
    except ValueError:
        return ""
    best = min(POWER_MARKS, key=lambda k: abs(k - w))
    return POWER_MARKS[best] if abs(best - w) < 1e-6 else ""


def _power_glyph(mark: str, cx: float, cy: float, h: float, unit: int
                 ) -> List[SymPrim]:
    """Обозначение мощности внутри резистора: '/', '//', '///', 'I', 'II', 'V', 'X'."""
    prims: List[SymPrim] = []
    hh = h * 0.55
    if mark in ("/", "//", "///"):
        k = len(mark)
        step = 40
        x = cx - step * (k - 1) / 2
        for i in range(k):
            prims.append(_line(x + i * step - 25, cy - hh, x + i * step + 25,
                               cy + hh, 1, unit))
    elif mark in ("I", "II"):
        k = len(mark)
        step = 60
        x = cx - step * (k - 1) / 2
        for i in range(k):
            prims.append(_line(x + i * step, cy - hh, x + i * step, cy + hh, 1, unit))
    elif mark == "V":
        prims.append(_line(cx - 45, cy + hh, cx, cy - hh, 1, unit))
        prims.append(_line(cx, cy - hh, cx + 45, cy + hh, 1, unit))
    elif mark == "X":
        prims.append(_line(cx - 45, cy + hh, cx + 45, cy - hh, 1, unit))
        prims.append(_line(cx - 45, cy - hh, cx + 45, cy + hh, 1, unit))
    return prims


def _draw_resistor_var(comp, st, unit):
    def draw(x0, x1, prims):
        h = st.s_res_h // 2
        prims.append(_rect(x0, h, x1, -h, st.lw_body, unit=unit))
        cx = (x0 + x1) / 2
        prims.append(_line(cx, h + 160, cx, h + 30, 1, unit))
        prims.append(_poly([[cx - 35, h + 90], [cx + 35, h + 90], [cx, h + 20]],
                           1, True, unit))
    return _two_pin(comp, st, st.s_res_w, unit, draw)


def _draw_capacitor(comp, st, unit, polarized=False):
    body = st.s_cap_gap

    def draw(x0, x1, prims):
        h = st.s_cap_plate // 2
        prims.append(_line(x0, h, x0, -h, st.lw_bar, unit))
        prims.append(_line(x1, h, x1, -h, st.lw_bar, unit))
        if polarized:
            prims.append(_line(x0 - 120, h + 60, x0 - 40, h + 60, 1, unit))
            prims.append(_line(x0 - 80, h + 20, x0 - 80, h + 100, 1, unit))
    return _two_pin(comp, st, body, unit, draw)


def _draw_inductor(comp, st, unit):
    body = 400

    def draw(x0, x1, prims):
        r = (x1 - x0) / 8
        for i in range(4):
            cx = x0 + r + 2 * r * i
            prims.append(_arc(cx, 0, r, 0, 180, 1, unit))
    return _two_pin(comp, st, body, unit, draw)


def _diode_body(x0, x1, unit, kind="diode", lw_bar=1):
    prims = []
    h = 100
    prims.append(_poly([[x0, h], [x0, -h], [x1, 0]], 1, True, unit))
    if kind == "zener":
        prims.append(_line(x1, h, x1, -h, 1, unit))
        prims.append(_line(x1, h, x1 - 50, h, 1, unit))
        prims.append(_line(x1, -h, x1 + 50, -h, 1, unit))
    elif kind == "schottky":
        prims.append(_line(x1, h, x1, -h, 1, unit))
        prims.append(_line(x1, h, x1 - 50, h, 1, unit))
        prims.append(_line(x1 - 50, h, x1 - 50, h - 50, 1, unit))
        prims.append(_line(x1, -h, x1 + 50, -h, 1, unit))
        prims.append(_line(x1 + 50, -h, x1 + 50, -h + 50, 1, unit))
    elif kind == "tvs":
        prims.append(_line(x1, h, x1, -h, 1, unit))
        prims.append(_poly([[x1 + (x1 - x0), h], [x1 + (x1 - x0), -h], [x1, 0]],
                           1, True, unit))
    else:
        prims.append(_line(x1, h, x1, -h, lw_bar, unit))
    if kind == "led":
        for k in (0, 1):
            bx = x0 + 60 + k * 90
            prims.append(_line(bx, h + 40, bx + 100, h + 140, 1, unit))
            prims.append(_poly([[bx + 100, h + 140], [bx + 60, h + 130],
                                [bx + 90, h + 100]], 1, True, unit))
    return prims


def _draw_diode(comp, st, unit, kind="diode"):
    body = 200 if kind != "tvs" else 400

    def draw(x0, x1, prims):
        prims.extend(_diode_body(x0, x0 + 200, unit, kind, st.lw_bar))
    return _two_pin(comp, st, body, unit, draw)


def _draw_crystal(comp, st, unit):
    def draw(x0, x1, prims):
        h = 150
        prims.append(_line(x0, h, x0, -h, st.lw_bar, unit))
        prims.append(_line(x1, h, x1, -h, st.lw_bar, unit))
        prims.append(_rect(x0 + 60, h - 40, x1 - 60, -h + 40, 1, unit=unit))
    return _two_pin(comp, st, 300, unit, draw)


def _draw_fuse(comp, st, unit):
    def draw(x0, x1, prims):
        h = st.s_res_h // 2
        prims.append(_rect(x0, h, x1, -h, st.lw_body, unit=unit))
        prims.append(_line(x0, 0, x1, 0, st.lw_bar, unit))
    return _two_pin(comp, st, st.s_res_w, unit, draw)


def _draw_varistor(comp, st, unit):
    def draw(x0, x1, prims):
        h = st.s_res_h // 2
        prims.append(_rect(x0, h, x1, -h, st.lw_body, unit=unit))
        prims.append(_line(x0 + 60, -h + 30, x1 - 60, h - 30, 1, unit))
        prims.append(_text(x1 - 90, h - 55, "U", 6, just=1, unit=unit))
    return _two_pin(comp, st, st.s_res_w, unit, draw)


def _draw_battery(comp, st, unit):
    def draw(x0, x1, prims):
        prims.append(_line(x0, 150, x0, -150, st.lw_bar, unit))
        prims.append(_line(x0 + 80, 80, x0 + 80, -80, 1, unit))
        prims.append(_line(x1 - 80, 150, x1 - 80, -150, st.lw_bar, unit))
        prims.append(_line(x1, 80, x1, -80, 1, unit))
    return _two_pin(comp, st, 300, unit, draw)


def _draw_buzzer(comp, st, unit):
    def draw(x0, x1, prims):
        cx = (x0 + x1) / 2
        prims.append(_arc(cx, 0, 150, 270, 90, 1, unit))
        prims.append(_line(cx, 150, cx, -150, 1, unit))
    return _two_pin(comp, st, 300, unit, draw)


def _draw_antenna(comp, st, unit):
    pins = sorted([p for p in comp.symbol.pins if p.unit == unit],
                  key=_pin_sort_key)[:1]
    prims = [
        _line(0, 0, 0, 200, 1, unit),
        _line(-150, 350, 0, 200, 1, unit),
        _line(150, 350, 0, 200, 1, unit),
        _line(-150, 350, 150, 350, 1, unit),
    ]
    out = []
    if pins:
        p = pins[0]
        p.x, p.y, p.rotation, p.length = 0, 0, 270, st.s_lead
        p.show_name, p.show_number = False, st.show_pin_numbers
        out.append(p)
    return prims, out, 300, 350


def _draw_testpoint(comp, st, unit):
    pins = [p for p in comp.symbol.pins if p.unit == unit][:1]
    prims = [_ellipse(0, 100, 50, 1, False, unit)]
    out = []
    if pins:
        p = pins[0]
        p.x, p.y, p.rotation, p.length = 0, 50, 270, 50
        p.show_name, p.show_number = False, st.show_pin_numbers
        out.append(p)
    return prims, out, 100, 150


def _draw_mount(comp, st, unit):
    prims = [_ellipse(0, 0, 100, 1, False, unit),
             _ellipse(0, 0, 50, 1, False, unit)]
    out = []
    for p in [p for p in comp.symbol.pins if p.unit == unit]:
        p.x, p.y, p.rotation, p.length = 100, 0, 0, 100
        p.show_name, p.show_number = False, st.show_pin_numbers
        out.append(p)
    return prims, out, 200, 200


def _pick(pins: List[SymPin], *names) -> Optional[SymPin]:
    for nm in names:
        for p in pins:
            if _norm(p.name) == nm:
                return p
    return None


def _draw_bjt(comp, st, unit):
    pins = sorted([p for p in comp.symbol.pins if p.unit == unit],
                  key=_pin_sort_key)
    npn = "PNP" not in (comp.params.get("Polarity", "") or "").upper()
    b = _pick(pins, "B", "BASE") or (pins[0] if len(pins) > 0 else None)
    c = _pick(pins, "C", "COLLECTOR") or (pins[1] if len(pins) > 1 else None)
    e = _pick(pins, "E", "EMITTER") or (pins[2] if len(pins) > 2 else None)
    bx, by = 200, 0
    prims = [
        _line(bx, 150, bx, -150, st.lw_bar, unit),          # база
        _line(bx, 60, bx + 200, 200, 1, unit),      # к коллектору
        _line(bx, -60, bx + 200, -200, 1, unit),    # к эмиттеру
        _line(bx + 200, 200, bx + 200, 300, 1, unit),
        _line(bx + 200, -200, bx + 200, -300, 1, unit),
    ]
    ax, ay = bx + 130, (-130 if npn else -95)
    if npn:
        prims.append(_poly([[bx + 200, -200], [bx + 120, -170], [bx + 155, -110]],
                           1, True, unit))
    else:
        prims.append(_poly([[bx + 60, -60], [bx + 140, -90], [bx + 105, -150]],
                           1, True, unit))
    out = []
    for p, (px, py, rot) in ((b, (bx, 0, 180)), (c, (bx + 200, 300, 90)),
                             (e, (bx + 200, -300, 270))):
        if p is None:
            continue
        p.x, p.y, p.rotation, p.length = px, py, rot, 200
        p.show_name, p.show_number = False, st.show_pin_numbers
        out.append(p)
    return prims, out, bx + 400, 600


def _draw_mosfet(comp, st, unit):
    pins = sorted([p for p in comp.symbol.pins if p.unit == unit],
                  key=_pin_sort_key)
    nch = "P" not in (comp.params.get("Channel", "N") or "N").upper()[:1]
    g = _pick(pins, "G", "GATE") or (pins[0] if pins else None)
    d = _pick(pins, "D", "DRAIN") or (pins[1] if len(pins) > 1 else None)
    s = _pick(pins, "S", "SOURCE") or (pins[2] if len(pins) > 2 else None)
    gx = 200
    chx = gx + 100
    prims = [
        _line(gx, 0, gx, 0, 1, unit),
        _line(gx, 150, gx, -150, 1, unit),            # затвор
        _line(chx, 200, chx, 120, st.lw_bar, unit),           # канал (3 сегмента)
        _line(chx, 40, chx, -40, st.lw_bar, unit),
        _line(chx, -120, chx, -200, st.lw_bar, unit),
        _line(chx, 160, chx + 200, 160, 1, unit),
        _line(chx, -160, chx + 200, -160, 1, unit),
        _line(chx, 0, chx + 200, 0, 1, unit),
        _line(chx + 200, 300, chx + 200, 160, 1, unit),
        _line(chx + 200, -300, chx + 200, -160, 1, unit),
        _line(chx + 200, 0, chx + 200, -160, 1, unit),
    ]
    if nch:
        prims.append(_poly([[chx, 0], [chx + 90, 35], [chx + 90, -35]], 1, True, unit))
    else:
        prims.append(_poly([[chx + 90, 0], [chx, 35], [chx, -35]], 1, True, unit))
    out = []
    for p, (px, py, rot) in ((g, (gx, 0, 180)), (d, (chx + 200, 300, 90)),
                             (s, (chx + 200, -300, 270))):
        if p is None:
            continue
        p.x, p.y, p.rotation, p.length = px, py, rot, 200
        p.show_name, p.show_number = False, st.show_pin_numbers
        out.append(p)
    return prims, out, chx + 400, 600


def _draw_opto(comp, st, unit):
    prims, pins, W, H = build_box(comp, st, unit)
    return prims, pins, W, H


def _draw_switch(comp, st, unit):
    pins = sorted([p for p in comp.symbol.pins if p.unit == unit],
                  key=_pin_sort_key)[:2]
    prims = [
        _line(200, 0, 260, 0, 1, unit),
        _line(260, 0, 500, 120, 1, unit),
        _line(540, 0, 600, 0, 1, unit),
    ]
    out = []
    if len(pins) >= 1:
        p = pins[0]
        p.x, p.y, p.rotation, p.length = 200, 0, 180, 200
        p.show_name, p.show_number = False, st.show_pin_numbers
        out.append(p)
    if len(pins) >= 2:
        p = pins[1]
        p.x, p.y, p.rotation, p.length = 600, 0, 0, 200
        p.show_name, p.show_number = False, st.show_pin_numbers
        out.append(p)
    return prims, out, 800, 200


def _draw_button(comp, st, unit):
    pins = sorted([p for p in comp.symbol.pins if p.unit == unit],
                  key=_pin_sort_key)
    prims = [
        _line(200, 0, 300, 0, 1, unit),
        _line(500, 0, 600, 0, 1, unit),
        _line(300, 100, 500, 100, 1, unit),
        _line(400, 100, 400, 220, 1, unit),
        _line(300, 220, 500, 220, st.lw_bar, unit),
        _line(300, 0, 300, 100, 1, unit),
        _line(500, 0, 500, 100, 1, unit),
    ]
    out = []
    for i, p in enumerate(pins[:4]):
        if i % 2 == 0:
            p.x, p.y, p.rotation, p.length = 200, -i * 0, 180, 200
        else:
            p.x, p.y, p.rotation, p.length = 600, 0, 0, 200
        p.show_name, p.show_number = False, st.show_pin_numbers
        out.append(p)
    return prims, out, 800, 220


_SHAPES = {
    "resistor": _draw_resistor,
    "resistor_var": _draw_resistor_var,
    "capacitor": lambda c, s, u: _draw_capacitor(c, s, u, False),
    "capacitor_pol": lambda c, s, u: _draw_capacitor(c, s, u, True),
    "inductor": _draw_inductor,
    "diode": lambda c, s, u: _draw_diode(c, s, u, "diode"),
    "zener": lambda c, s, u: _draw_diode(c, s, u, "zener"),
    "schottky": lambda c, s, u: _draw_diode(c, s, u, "schottky"),
    "tvs": lambda c, s, u: _draw_diode(c, s, u, "tvs"),
    "led": lambda c, s, u: _draw_diode(c, s, u, "led"),
    "crystal": _draw_crystal,
    "fuse": _draw_fuse,
    "varistor": _draw_varistor,
    "battery": _draw_battery,
    "buzzer": _draw_buzzer,
    "antenna": _draw_antenna,
    "testpoint": _draw_testpoint,
    "mount": _draw_mount,
    "bjt": _draw_bjt,
    "mosfet": _draw_mosfet,
    "opto": _draw_opto,
    "switch": _draw_switch,
    "button": _draw_button,
}

# сколько выводов должно быть, чтобы форма имела смысл
_SHAPE_PINS = {
    "resistor": (2, 2), "resistor_var": (2, 3), "capacitor": (2, 2),
    "capacitor_pol": (2, 2), "inductor": (2, 2), "diode": (2, 2),
    "zener": (2, 2), "schottky": (2, 2), "tvs": (2, 3), "led": (2, 2),
    "crystal": (2, 2), "fuse": (2, 2), "varistor": (2, 2),
    "battery": (2, 2), "buzzer": (2, 2), "antenna": (1, 2),
    "testpoint": (1, 1), "mount": (1, 4), "bjt": (3, 4), "mosfet": (3, 4),
    "switch": (2, 2), "button": (2, 4),
}


# --------------------------------------------------------------- сборка ------

def auto_fields(pins: List[SymPin], st: Style) -> Tuple[int, int]:
    """Ширина левого и правого дополнительных полей по самым длинным именам."""
    left = [p for p in pins if p.side == "L"]
    right = [p for p in pins if p.side == "R"]
    lw = snap_up(max([st.text_w(p.name, st.size_pin) for p in left] or [0])
                 + 2 * st.text_pad) if left else 0
    rw = snap_up(max([st.text_w(p.name, st.size_pin) for p in right] or [0])
                 + 2 * st.text_pad) if right else 0
    return (max(lw, st.min_side_field if left else 0),
            max(rw, st.min_side_field if right else 0))


def _shape_prims(comp: Component, st: Style) -> List[SymPrim]:
    """
    Графика формы элемента (резистор, конденсатор, диод…) без расстановки
    выводов. Рисовалка формы попутно двигает выводы, поэтому зовём её на
    копии компонента и берём только примитивы.
    """
    import copy as _copy
    shape = classify.SYMBOL_SHAPE.get(comp.ctype, "box")
    fn = _SHAPES.get(shape)
    if fn is None:
        return []
    try:
        tmp = _copy.deepcopy(comp)
        out: List[SymPrim] = []
        for u in sorted({p.unit for p in tmp.symbol.pins} or {1}):
            prims, _pins, _w, _h = fn(tmp, st, u)
            out.extend(prims)
        return out
    except Exception:
        return []


def build_manual(comp: Component, st: Style) -> Symbol:
    """
    Собрать символ по ручной раскладке из редактора.

    Выводы стоят там, где их поставил пользователь; наша забота -- корпус,
    поля, разделители и подписи. Корпус при необходимости растягивается по
    крайним выводам, чтобы вплотную поставленный вывод не оказался снаружи.
    """
    st = st.for_component(comp)
    sym = comp.symbol
    pins = [p for p in sym.pins]
    # KiCad помечает продублированные выводы питания как скрытые: там они
    # соединяются с цепью по имени, поэтому рисовать их незачем. В Altium
    # такого нет -- скрытый вывод просто не рисуется и подключить к нему
    # ничего нельзя, и кажется, что выводов не хватает. В ЕСКД каждый вывод
    # питания разводится руками, так что признак «скрытый» не переносим.
    for p in pins:
        p.hidden = False
    # Секции строятся по очереди и каждая со своими габаритами: раньше
    # корпус был один на весь символ, и растянутая первая секция тянула за
    # собой вторую и третью. Геометрия лежит в sym.geom(номер секции).
    nparts = max(1, int(sym.part_count or 1))
    sym.drop_parts_above(nparts)
    if nparts > 1:
        prims: List[SymPrim] = []
        cmt = [0, 0]
        for part in range(1, nparts + 1):
            sub = [p for p in pins if int(p.unit or 1) == part]
            prims.extend(_manual_part(comp, st, sub, part))
            if part == 1:
                g = sym.geom(1)
                w, h = int(g["body_w"]), int(g["body_h"])
                cmt = [(int(g["field_l"]) + (w - int(g["field_r"]))) // 2,
                       -st.body_margin] if not st.draw_type_label \
                    else [w // 2, -h - 100]
        sym.prims = prims
        sym.designator_pos = [0, 100]
        sym.comment_pos = cmt
        return sym

    W = int(sym.body_w or 0)
    H = int(sym.body_h or 0)
    if pins:
        xs = [p.x for p in pins]
        ys = [p.y for p in pins]
        W = max(W, max(xs) if xs else 0)
        H = max(H, -min(ys) + st.body_margin if ys else 0)
    W = max(W, st.min_main_field)
    H = max(H, st.eff_pitch())

    # У пассивки корпуса нет -- есть её обозначение по ГОСТ. Раньше ручная
    # раскладка подменяла его прямоугольником, и резистор превращался в
    # коробку с двумя выводами. Пока графику не разобрали на линии в
    # редакторе, рисуем настоящую форму, без корпуса, полей и имён выводов.
    # Родное обозначение (KiCad) при ручной раскладке остаётся родным:
    # раньше сюда подставлялась ГОСТ-форма, и кварц после первой же
    # правки превращался в чужой символ.
    native = (getattr(comp, "symbol_source", "gost") == "native"
              and getattr(comp, "native_prims", None) and not sym.user_lines)
    shape = classify.SYMBOL_SHAPE.get(comp.ctype, "box")
    keep_shape = native or (shape not in ("box", "connector")
                            and shape in _SHAPES and not sym.user_lines)

    fl, fr = int(sym.field_l or 0), int(sym.field_r or 0)
    if keep_shape:
        fl = fr = 0
    elif fl == 0 and fr == 0 and st.ic_fields:
        fl, fr = auto_fields(pins, st)
        fl = min(fl, max(0, W // 3))
        fr = min(fr, max(0, W // 3))

    prims: List[SymPrim] = []
    if fl:
        prims.append(_line(fl, 0, fl, -H, st.lw_inner))
    if fr:
        prims.append(_line(W - fr, 0, W - fr, -H, st.lw_inner))
    for y in sym.dividers or []:
        y = int(y)
        if -H < y < 0:
            prims.append(_line(0, y, W, y, st.lw_inner))
    for ln in sym.user_lines or []:
        if len(ln) >= 4:
            pr = _line(ln[0], ln[1], ln[2], ln[3],
                       int(ln[4]) if len(ln) > 4 else st.lw_inner)
            if len(ln) > 5:
                pr.color = int(ln[5])
            prims.append(pr)
    prims += shape_prims(getattr(sym, "user_shapes", None), st.lw_inner)

    if native:
        prims = [SymPrim(**{k: v for k, v in pr.__dict__.items()
                            if k in SymPrim.__dataclass_fields__})
                 for pr in comp.native_prims] + prims
    elif keep_shape:
        prims = _shape_prims(comp, st) + prims
    else:
        prims.insert(0, _rect(0, 0, W, -H, st.lw_body))

    # Подпись типа/номинала графикой НЕ рисуем: она приезжает в Altium
    # штатным Comment, который стоит там же (sym.comment_pos), но его можно
    # подвинуть и поправить прямо на листе.
    label = classify.label_for(comp)
    if st.draw_type_label and label:
        prims.append(_text(W / 2.0, -st.body_margin, label, st.size_type,
                           just=1, vjust=1))

    # Два вывода в одной точке Altium показывает как один -- остальные
    # словно пропадают. Раздвигаем по сетке и пишем об этом в журнал.
    seen = {}
    for p in sorted(pins, key=lambda q: (q.side, -q.y)):
        key = (p.unit, p.side, p.y)
        while key in seen:
            p.y -= st.eff_pitch()
            key = (p.unit, p.side, p.y)
        seen[key] = p
        H = max(H, -p.y + st.body_margin)

    for p in pins:
        p.length = p.length or st.pin_length
        p.show_name = False
        p.show_number = st.show_pin_numbers
        if not p.name or keep_shape:
            continue      # у пассивок имена выводов не подписывают
        if p.side == "R" or p.rotation % 360 == 0:
            prims.append(_text(W - st.text_pad, p.y, p.name, st.size_pin,
                               just=2, unit=p.unit))
        elif p.side == "L" or p.rotation % 360 == 180:
            prims.append(_text(st.text_pad, p.y, p.name, st.size_pin,
                               just=0, unit=p.unit))
        else:
            prims.append(_text(p.x, p.y, p.name, st.size_pin, just=1,
                               unit=p.unit))

    if st.pin_numbers_as_text:
        prims.extend(_number_texts(pins, st))

    sym.prims = prims
    sym.body_w, sym.body_h = W, H
    sym.field_l, sym.field_r = fl, fr
    sym.designator_pos = [0, 100]
    # Comment -- это и есть подпись типа/номинала: ставим её в основное
    # поле, между дополнительными, а не под корпус.
    if not st.draw_type_label:
        sym.comment_pos = [(fl + (W - fr)) // 2, -st.body_margin]
    else:
        sym.comment_pos = [W // 2, -H - 100]
    return sym


def _manual_part(comp: Component, st: Style, pins: List[SymPin],
                 part: int) -> List[SymPrim]:
    """
    Одна секция многосекционного символа при ручной раскладке.

    Габариты, поля, разделители и свои линии берутся у этой секции и ей же
    записываются обратно. Все примитивы помечаются `unit=part`, иначе
    Altium покажет графику первой секции во всех остальных.
    """
    sym = comp.symbol
    g = sym.geom(part)
    W = int(g["body_w"] or 0)
    H = int(g["body_h"] or 0)
    if pins:
        W = max(W, max(p.x for p in pins))
        H = max(H, -min(p.y for p in pins) + st.body_margin)
    W = max(W, st.min_main_field)
    H = max(H, st.eff_pitch())

    fl, fr = int(g["field_l"] or 0), int(g["field_r"] or 0)
    if fl == 0 and fr == 0 and st.ic_fields:
        fl, fr = auto_fields(pins, st)
        fl = min(fl, max(0, W // 3))
        fr = min(fr, max(0, W // 3))

    prims: List[SymPrim] = [_rect(0, 0, W, -H, st.lw_body, unit=part)]
    if fl:
        prims.append(_line(fl, 0, fl, -H, st.lw_inner, unit=part))
    if fr:
        prims.append(_line(W - fr, 0, W - fr, -H, st.lw_inner, unit=part))
    for y in g["dividers"] or []:
        y = int(y)
        if -H < y < 0:
            prims.append(_line(0, y, W, y, st.lw_inner, unit=part))
    for ln in g["user_lines"] or []:
        if len(ln) >= 4:
            pr = _line(ln[0], ln[1], ln[2], ln[3],
                       int(ln[4]) if len(ln) > 4 else st.lw_inner, unit=part)
            if len(ln) > 5:
                pr.color = int(ln[5])
            prims.append(pr)
    prims += shape_prims(g.get("user_shapes"), st.lw_inner, unit=part)

    # Два вывода в одной точке Altium показывает как один. Считаем это
    # отдельно по каждой секции: у разных секций координаты совпадают
    # законно.
    seen = {}
    for p in sorted(pins, key=lambda q: (q.side, -q.y)):
        key = (p.side, p.y)
        while key in seen:
            p.y -= st.eff_pitch()
            key = (p.side, p.y)
        seen[key] = p
        H = max(H, -p.y + st.body_margin)

    for p in pins:
        p.length = p.length or st.pin_length
        p.show_name = False
        p.show_number = st.show_pin_numbers
        if not p.name:
            continue
        if p.side == "R" or p.rotation % 360 == 0:
            prims.append(_text(W - st.text_pad, p.y, p.name, st.size_pin,
                               just=2, unit=part))
        elif p.side == "L" or p.rotation % 360 == 180:
            prims.append(_text(st.text_pad, p.y, p.name, st.size_pin,
                               just=0, unit=part))
        else:
            prims.append(_text(p.x, p.y, p.name, st.size_pin, just=1,
                               unit=part))

    if st.pin_numbers_as_text:
        prims.extend(_number_texts(pins, st))

    sym.set_geom(part, body_w=W, body_h=H, field_l=fl, field_r=fr)
    return prims


def _discrete(comp: Component) -> bool:
    """
    Дискретный элемент: рисуется своей формой, а не прямоугольником.

    У таких имена выводов не подписывают -- по ГОСТ 2.728/2.730 смысл
    вывода читается по самому обозначению.
    """
    shape = classify.SYMBOL_SHAPE.get(getattr(comp, "ctype", ""), "box")
    return shape not in ("box", "connector", "rnet")


def _spread_overlaps(pins: List[SymPin], st: Style) -> int:
    """
    Развести выводы, попавшие в одну точку.

    В KiCad так принято: у кварца оба GND-вывода стоят один на другом,
    у микросхем дубли питания складывают в стопку. Altium рисует такие
    выводы друг поверх друга -- виден один, а к остальным не подключиться.

    Разводим СИММЕТРИЧНО относительно исходной точки и поперёк вывода:
    два земляных вывода кварца встают по краям, как на настоящем
    четырёхногом резонаторе, а не убегают под соседний вывод.
    """
    groups: Dict[tuple, List[SymPin]] = {}
    for p in pins:
        groups.setdefault((p.unit, p.x, p.y, p.rotation), []).append(p)
    step = max(50, st.eff_pitch() // 2)
    moved = 0
    for (_u, x, y, rot), items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=_pin_sort_key)
        k = len(items)
        for i, p in enumerate(items):
            off = int((i - (k - 1) / 2.0) * step)
            if off == 0:
                continue
            if rot % 180 == 0:          # вывод идёт вбок -- разводим по Y
                p.y = y + off
            else:                        # вверх/вниз -- разводим по X
                p.x = x + off
            moved += 1

    # после раздвижки точки всё ещё могут совпасть с чужими -- дожимаем
    seen = {}
    for p in sorted(pins, key=lambda q: (q.unit, q.rotation, -q.y, q.x)):
        key = (p.unit, p.x, p.y)
        while key in seen:
            if p.rotation % 180 == 0:
                p.y -= step
            else:
                p.x += step
            key = (p.unit, p.x, p.y)
            moved += 1
        seen[key] = p
    return moved


def build_native(comp: Component, st: Style) -> Symbol:
    """
    Обозначение как в источнике (KiCad), без перерисовки.

    Резистор, конденсатор, диод в KiCad нарисованы по тем же ГОСТ-формам,
    что и у нас, и трогать их незачем: перерисовка только вносит
    расхождения с тем, что человек видел в KiCad. А вот номера выводов
    рисуем своим текстом -- ради шрифта и настраиваемого зазора.
    """
    src_pins = comp.native_pins or comp.raw_pins or comp.symbol.pins
    pins = [SymPin(**{k: v for k, v in p.__dict__.items()
                      if k in SymPin.__dataclass_fields__}) for p in src_pins]
    # Имена и типы берём из raw_pins: там лежит то, что человек правил в
    # таблице выводов. native_pins хранят только геометрию из источника,
    # и без этого правки имён просто пропадали.
    edited = {p.number: p for p in (comp.raw_pins or [])}
    for p in pins:
        src = edited.get(p.number)
        if src is not None:
            p.name = src.name
            p.etype = src.etype
            p.inverted = src.inverted
            p.clock = src.clock
    prims = [SymPrim(**{k: v for k, v in pr.__dict__.items()
                        if k in SymPrim.__dataclass_fields__})
             for pr in comp.native_prims]

    units = sorted({p.unit for p in pins}) or [1]
    sym = Symbol(part_count=max(units), style="native")
    sym.pins = pins
    comp.symbol = sym

    for p in pins:
        p.hidden = False              # см. build_manual
        # У пассивок KiCad имя вывода часто совпадает с номером ("1", "2")
        # или стоит заглушкой "~". Показывать и то и другое -- получить две
        # надписи друг на друге, что и было видно на кварце.
        nm = (p.name or "").strip()
        # У дискретных элементов имена выводов условны: A и K у диода,
        # G/D/S у полевика, G у земли кварца. Форма обозначения и так
        # говорит, где что, а подписи только мусорят -- по ЕСКД их не
        # ставят. Показываем имя только там, где оно несёт смысл.
        p.show_name = (bool(nm and nm not in ("~", "") and nm != p.number)
                       and not _discrete(comp))
        p.show_number = False         # номера рисуем текстом сами
        p.length = p.length or st.pin_length

    # запоминаем, кто стоял в стопке: после раздвижки одинаковое имя
    # («G» у обоих земляных выводов кварца) писать дважды незачем
    stacks: Dict[tuple, List[SymPin]] = {}
    for p in pins:
        stacks.setdefault((p.unit, p.x, p.y, p.rotation), []).append(p)
    _spread_overlaps(pins, st)
    for items in stacks.values():
        if len(items) < 2:
            continue
        names = {(q.name or "").strip() for q in items}
        if len(names) == 1:
            for q in items[1:]:
                q.show_name = False

    if st.pin_numbers_as_text and st.show_pin_numbers:
        for p in pins:
            p.show_number = True
        prims.extend(_number_texts(pins, st))

    sym.prims = prims
    x0, y0, x1, y1 = sym.bbox()
    sym.body_w, sym.body_h = int(x1 - x0), int(y1 - y0)
    # Позиционное обозначение ставим ОТ ЛЕВОГО КРАЯ, а не от середины:
    # текст пишется слева направо, и от середины «R?» наезжало на корпус
    # у любого узкого элемента -- у резистора он всего 80 mil шириной.
    sym.designator_pos = [int(x0), int(y1 + 60)]
    sym.comment_pos = [int((x0 + x1) / 2), int(y0 - 100)]
    return sym


def build(comp: Component, st: Optional[Style] = None) -> Symbol:
    """Пересобрать символ компонента по ГОСТ. Изменяет comp.symbol."""
    st = (st or DEFAULT).for_component(comp)
    if getattr(comp.symbol, "manual_layout", False) and comp.symbol.pins:
        # раскладку задал пользователь -- автомат сюда не лезет
        return build_manual(comp, st)
    if getattr(comp, "symbol_source", "gost") == "native" \
            and getattr(comp, "native_prims", None):
        return build_native(comp, st)
    src_pins = comp.raw_pins or comp.symbol.pins
    pins = [SymPin(**{k: v for k, v in p.__dict__.items()
                      if k in SymPin.__dataclass_fields__}) for p in src_pins]
    for p in pins:
        p.group = p.group if _looks_manual(p.group) else ""
        p.hidden = False   # см. build_manual: скрытых выводов в ЕСКД не бывает
    units = sorted({p.unit for p in pins}) or [1]
    sym = Symbol(part_count=max(units), style="gost")
    sym.pins = pins
    comp.symbol = sym

    shape = classify.SYMBOL_SHAPE.get(comp.ctype, "box")
    if comp.ctype == "connector":
        shape = "connector"
    npins = len(pins)
    lim = _SHAPE_PINS.get(shape)
    if lim and not (lim[0] <= npins <= lim[1]):
        shape = "box"

    all_prims: List[SymPrim] = []
    all_pins: List[SymPin] = []
    W = H = 0
    for u in units:
        if shape == "connector":
            prims, upins, w, h = build_connector(comp, st, u)
        elif shape in _SHAPES:
            prims, upins, w, h = _SHAPES[shape](comp, st, u)
        else:
            prims, upins, w, h = build_box(comp, st, u)
        all_prims.extend(prims)
        all_pins.extend(upins)
        W, H = max(W, w), max(H, h)

    # пины, которые не попали в раскладку (например, лишние у дискретов)
    placed = {id(p) for p in all_pins}
    extra = [p for p in pins if id(p) not in placed]
    if extra:
        y = -H - st.eff_pitch()
        for p in extra:
            p.x, p.y, p.rotation, p.length = 0, y, 180, st.pin_length
            p.show_name, p.show_number = True, True
            y -= st.eff_pitch()

    if st.pin_numbers_as_text:
        all_prims.extend(_number_texts(all_pins, st))

    sym.prims = all_prims
    sym.designator_pos = [0, 100]
    if shape in ("box", "connector") or comp.ctype in ("ic", "mcu", "memory",
                                                       "logic", "connector"):
        sym.comment_pos = [W // 2, -H - 100] if shape == "connector" else \
            [W // 2, -st.body_margin]
    else:
        sym.comment_pos = [W // 2, -200]
    return sym


def verify(comp: Component, st: Style) -> List[str]:
    """
    Проверить готовый символ: сетка, наложения, выводы на кромке корпуса.

    Проверяется то, что видно только на схеме: номер вывода, наехавший на
    соседний, или вывод, стоящий не по сетке -- к такому потом не
    подцепится провод.
    """
    problems: List[str] = []
    sym = comp.symbol
    pins = sym.pins or []
    grid = GRID

    off = [p for p in pins
           if not st.on_grid(p.x, grid) or not st.on_grid(p.y, grid)]
    if off:
        names = ", ".join(f"{p.number}({p.x},{p.y})" for p in off[:5])
        problems.append(f"выводы не по сетке {grid} mil: {names}")

    bad_len = [p for p in pins if not st.on_grid(p.length, grid)]
    if bad_len:
        problems.append("длина вывода не кратна сетке: "
                        + ", ".join(p.number for p in bad_len[:5]))

    # наложение номеров и имён по вертикали на каждой стороне
    hn = st.text_h(st.size_pin_num)
    hname = st.text_h(st.size_pin)
    # Разные секции никогда не показываются одновременно. Сравнивать их
    # координаты между собой нельзя: у каждой секции закономерно есть,
    # например, первый левый вывод на одной и той же высоте.
    units = sorted({int(p.unit or 1) for p in pins}) or [1]
    for unit in units:
        for side in ("L", "R"):
            ys = sorted(p.y for p in pins
                        if int(p.unit or 1) == unit and p.side == side)
            for a, b in zip(ys, ys[1:]):
                d = abs(b - a)
                where = f"секции {unit}, сторона {side}"
                if d < 1e-6:
                    problems.append(f"в {where} два вывода в одной точке y={a}")
                    break
                if d < max(hn, hname) * 0.95:
                    problems.append(
                        f"в {where} шаг {int(d)} mil меньше высоты текста "
                        f"({int(max(hn, hname))} mil) -- подписи наедут")
                    break

    nums = {}
    for p in pins:
        k = (p.unit, (p.number or "").strip())
        if k[1]:
            nums.setdefault(k, []).append(p)
    dup = [k[1] for k, v in nums.items() if len(v) > 1]
    if dup:
        problems.append("повторяются номера выводов: " + ", ".join(dup[:6])
                        + " -- Altium покажет такие выводы как один")

    # Габариты у каждой секции свои, поэтому и проверяем посекционно:
    # общий корпус второй секции ни о чём не говорит.
    outside: List[SymPin] = []
    for part in sorted({int(p.unit or 1) for p in pins}):
        g = sym.geom(part)
        W, H = int(g["body_w"] or 0), int(g["body_h"] or 0)
        if not (W and H):
            continue
        outside += [p for p in pins if int(p.unit or 1) == part
                    and not (-H - 1 <= p.y <= 1)]
    if outside:
        problems.append("выводы за пределами корпуса: "
                        + ", ".join(p.number for p in outside[:5]))
    return problems


def _number_texts(pins: List[SymPin], st: Style) -> List[SymPrim]:
    """
    Номера выводов рисуются собственным текстом (а не средствами Altium),
    чтобы гарантированно получить шрифт ГОСТ тип Б.
    """
    out: List[SymPrim] = []
    for p in pins:
        if not p.number or not p.show_number:
            continue
        p.show_number = False
        r = p.rotation % 360
        half = p.length / 2.0
        if r == 180:      # вывод уходит влево
            out.append(_text(p.x - half, p.y + st.num_offset, p.number,
                             st.size_pin_num, just=1, vjust=0, unit=p.unit))
        elif r == 0:      # вправо
            out.append(_text(p.x + half, p.y + st.num_offset, p.number,
                             st.size_pin_num, just=1, vjust=0, unit=p.unit))
        elif r == 90:     # вверх
            out.append(_text(p.x - st.num_offset, p.y + half, p.number,
                             st.size_pin_num, just=1, vjust=0, rot=90,
                             unit=p.unit))
        else:             # вниз
            out.append(_text(p.x - st.num_offset, p.y - half, p.number,
                             st.size_pin_num, just=1, vjust=0, rot=90,
                             unit=p.unit))
    return out


def _looks_manual(g: str) -> bool:
    """Группы, заданные пользователем в GUI, начинаются с '!'."""
    return bool(g) and g.startswith("!")
