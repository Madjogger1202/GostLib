"""
Задание для скрипта GostLibBuilder внутри Altium.

Библиотеки строит САМ Altium штатным API (IPCB_LibComponent / ISch_Lib),
а это задание -- полное описание того, что построить. Отсюда требования
к формату:

* кодировка CP1251 без BOM -- ровно то, что читает TStringList внутри
  Altium на русской Windows. Символы, которых в CP1251 нет, уходят в
  \\uXXXX и раскрываются скриптом;
* только целые числа. Все координаты -- во внутренних единицах Altium
  (1/10000 мила), углы -- в десятых долях градуса. Дробей нет вообще,
  поэтому не зависим от десятичного разделителя локали (на русской
  Windows StrToFloat ждёт запятую и на '1.27' падает);
* строки -- поля, разделённые табуляцией. Разбор в DelphiScript --
  два вызова Pos/Copy, ошибиться негде.

Библиотека -- полная проекция каталога: скрипт стирает содержимое и
строит заново, поэтому пересборка идемпотентна и дублей не бывает.
"""
from __future__ import annotations

import os
from typing import Dict, Iterable, List, Optional, Sequence

from .. import classify
from ..classify import CTYPE_NAME
from ..gost.style import DEFAULT, Style
from ..ir import ETYPES, Component, Footprint, Pad, Symbol, SymPin, SymPrim

VERSION = 3

# 1 внутренняя единица Altium = 1/10000 мила
UNITS_PER_MIL = 10000.0
UNITS_PER_MM = 10000.0 / 0.0254

# крупные группы для параметра Category
GROUPS = {
    "Пассивные": ("resistor", "resistor_var", "resistor_net", "capacitor",
                  "capacitor_pol", "inductor", "ferrite", "transformer",
                  "varistor", "fuse"),
    "Полупроводники": ("diode", "zener", "schottky", "tvs", "led", "bridge",
                       "bjt", "mosfet", "igbt", "thyristor", "opto"),
    "Микросхемы": ("ic", "mcu", "memory", "logic", "opamp", "comparator",
                   "adc_dac", "driver", "rf"),
    "Питание": ("ldo", "dcdc", "battery"),
    "Соединители": ("connector", "testpoint", "mount"),
    "Электромеханика": ("switch", "button", "relay", "buzzer", "motor",
                        "display"),
    "Частотозадающие": ("crystal", "oscillator"),
    "Прочее": ("sensor", "module", "antenna", "other"),
}

CTYPE_GROUP: Dict[str, str] = {}
for _g, _codes in GROUPS.items():
    for _c in _codes:
        CTYPE_GROUP[_c] = _g


def group_of(ctype: str) -> str:
    return CTYPE_GROUP.get(ctype, "Прочее")


# --------------------------------------------------------------- служебное --
ENCODING = "cp1251"
_ASCII_ONLY = False


def _fits(ch: str) -> bool:
    if _ASCII_ONLY:
        return False
    try:
        ch.encode(ENCODING)
        return True
    except UnicodeEncodeError:
        return False


def esc(s: object) -> str:
    """
    Экранирование для задания: обратный слэш, табуляция и перевод строки --
    служебные символы формата, поэтому всегда уходят в \\-последовательности.
    Всё, чего нет в целевой кодировке, пишется как \\uXXXX.
    """
    out: List[str] = []
    for ch in str(s if s is not None else ""):
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\t":
            out.append("\\t")
        elif ch in ("\r", "\n"):
            out.append("\\n")
        elif 32 <= o <= 126 or _fits(ch):
            out.append(ch)
        else:
            out.append("\\u%04X" % o)
    return "".join(out)


def mil(v: float) -> int:
    return int(round(float(v) * UNITS_PER_MIL))


def mm(v: float) -> int:
    return int(round(float(v) * UNITS_PER_MM))


def deg10(v: float) -> int:
    return int(round(float(v) * 10.0))


def _rot4(v: float) -> int:
    r = int(round(float(v))) % 360
    return {0: 0, 90: 90, 180: 180, 270: 270}.get(r, 0)


class _Job:
    def __init__(self):
        self.lines: List[str] = []

    def add(self, *fields):
        self.lines.append("\t".join(str(f) for f in fields))

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# ------------------------------------------------------------------ символ --
def _pin_flags(p: SymPin) -> int:
    f = 0
    if p.show_name:
        f |= 1
    if p.show_number:
        f |= 2
    if p.hidden:
        f |= 4
    if p.inverted:
        f |= 8
    if p.clock:
        f |= 16
    return f


_PIN_DX = {0: 1, 90: 0, 180: -1, 270: 0}
_PIN_DY = {0: 0, 90: 1, 180: 0, 270: -1}


def _emit_symbol(j: _Job, sym: Symbol, st: Style):
    for p in sym.pins:
        et = ETYPES.index(p.etype) if p.etype in ETYPES else 4
        rot = _rot4(p.rotation)
        # (x, y) в IR -- конец вывода у корпуса; Altium'у может понадобиться
        # электрический («горячий») конец, поэтому пишем оба, а какой брать
        # решает заголовок PINLOC.
        hx = p.x + p.length * _PIN_DX[rot]
        hy = p.y + p.length * _PIN_DY[rot]
        j.add("PIN", max(1, int(p.unit)), esc(p.number), esc(p.name), et,
              mil(p.x), mil(p.y), mil(p.length), rot,
              _pin_flags(p), mil(hx), mil(hy))
    for pr in sym.prims:
        unit = max(1, int(pr.unit))
        w = max(1, min(3, int(pr.width or 1)))
        col = int(getattr(pr, "color", -1))
        if pr.kind == "line" and len(pr.pts) >= 2:
            (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
            j.add("SLINE", unit, w, mil(x1), mil(y1), mil(x2), mil(y2), col)
        elif pr.kind == "rect" and len(pr.pts) >= 2:
            (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
            j.add("SRECT", unit, w, mil(min(x1, x2)), mil(min(y1, y2)),
                  mil(max(x1, x2)), mil(max(y1, y2)), 1 if pr.filled else 0,
                  col)
        elif pr.kind == "arc" and pr.pts:
            cx, cy = pr.pts[0]
            j.add("SARC", unit, w, mil(cx), mil(cy), mil(pr.radius),
                  deg10(pr.a1), deg10(pr.a2))
        elif pr.kind == "ellipse" and pr.pts:
            # окружность пишем дугой: у ISch_Ellipse пришлось бы задавать
            # SecondaryRadius, а этого свойства в DelphiScript может не быть
            cx, cy = pr.pts[0]
            j.add("SARC", unit, w, mil(cx), mil(cy), mil(pr.radius), 0, 3600)
        elif pr.kind == "poly" and len(pr.pts) >= 2:
            # ломаную выкладываем отрезками: ISch_Polygon требует Vertex[],
            # а он не подтверждён рабочими скриптами. Теряется только
            # заливка, а она у ГОСТ-символов декоративная.
            for a, b in zip(pr.pts, pr.pts[1:]):
                j.add("SLINE", unit, w, mil(a[0]), mil(a[1]),
                      mil(b[0]), mil(b[1]), col)
            if pr.filled and len(pr.pts) > 2:
                a, b = pr.pts[-1], pr.pts[0]
                j.add("SLINE", unit, w, mil(a[0]), mil(a[1]),
                      mil(b[0]), mil(b[1]), col)
        elif pr.kind == "text" and pr.pts:
            x, y = pr.pts[0]
            just = int(pr.justify) + 3 * int(pr.vjustify)   # 0..8
            # номера выводов рисуются тем же кеглем, что задан в стиле --
            # по нему и различаем, каким цветом их красить
            col = (st.color_pin_num if int(pr.size or 0) == st.size_pin_num
                   else st.color_text)
            j.add("STEXT", unit, mil(x), mil(y), int(pr.size or 10),
                  _rot4(pr.rotation), just, 1 if pr.bold else 0, esc(pr.text),
                  int(col))


# ---------------------------------------------------------- посадочное место --
PAD_SHAPE = {"round": 1, "oval": 1, "rect": 2, "roundrect": 3, "octagon": 4}
FP_LAYER = {
    "silk": 1, "silk_bot": 2, "assy": 3, "courtyard": 4, "keepout": 5,
    "mech": 6, "copper_top": 7, "copper_bot": 8, "paste": 9, "mask": 10,
}


def _emit_footprint(j: _Job, fp: Footprint):
    j.add("FP", esc(fp.name), esc(fp.description), mm(fp.height or 0.0))
    for p in fp.pads:
        layer = {"top": 1, "copper_top": 1, "bottom": 2, "copper_bot": 2,
                 "multi": 3}.get(p.layer, 1)
        if p.hole > 0:
            layer = 3
        j.add("PAD", esc(p.number), mm(p.x), mm(p.y), mm(p.w), mm(p.h),
              PAD_SHAPE.get(p.shape, 2), deg10(p.rot), layer, mm(p.hole),
              1 if p.plated else 0, mm(p.hole_len),
              deg10(getattr(p, "hole_rot", 0.0)))
    for pr in fp.prims:
        lay = FP_LAYER.get(pr.layer, 1)
        w = mm(pr.width or 0.15)
        if pr.kind == "line" and len(pr.pts) >= 2:
            for a, b in zip(pr.pts, pr.pts[1:]):
                j.add("TRK", lay, w, mm(a[0]), mm(a[1]), mm(b[0]), mm(b[1]))
        elif pr.kind in ("arc", "circle") and pr.pts:
            cx, cy = pr.pts[0]
            j.add("FARC", lay, w, mm(cx), mm(cy), mm(pr.radius),
                  deg10(pr.a1), deg10(pr.a2))
        elif pr.kind == "rect" and len(pr.pts) >= 2:
            (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
            pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
            if pr.filled:
                j.add("FILL", lay, mm(min(x1, x2)), mm(min(y1, y2)),
                      mm(max(x1, x2)), mm(max(y1, y2)))
            else:
                for a, b in zip(pts, pts[1:]):
                    j.add("TRK", lay, w, mm(a[0]), mm(a[1]), mm(b[0]), mm(b[1]))
        elif pr.kind == "poly" and len(pr.pts) >= 2:
            for a, b in zip(pr.pts, pr.pts[1:]):
                j.add("TRK", lay, w, mm(a[0]), mm(a[1]), mm(b[0]), mm(b[1]))
        elif pr.kind == "text" and pr.pts:
            x, y = pr.pts[0]
            j.add("FTXT", lay, mm(x), mm(y), mm(pr.height or 1.0),
                  deg10(pr.rot), w, 1 if pr.mirror else 0, esc(pr.text))
    m = fp.model
    if m and m.path and os.path.isfile(m.path):
        j.add("BODY", esc(m.path), mm(m.dx), mm(m.dy), mm(m.dz),
              deg10(m.rx), deg10(m.ry), deg10(m.rz))
    j.add("ENDFP")


# --------------------------------------------------------------- компонент --
def _params_of(c: Component) -> List[tuple]:
    """(имя, значение, скрытый) -- то, что уйдёт в параметры компонента."""
    out: List[tuple] = []
    seen = set()

    def put(name, value, hidden=1):
        if not name or value in (None, ""):
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        out.append((name, str(value), 1 if hidden else 0))

    put("Category", group_of(c.ctype), 1)
    put("ТипЭлемента", CTYPE_NAME.get(c.ctype, c.ctype), 1)
    put("Manufacturer", c.manufacturer)
    put("MPN", c.mpn)
    # Номинал всегда скрытым параметром. Видимая подпись на схеме одна --
    # Comment (см. classify.label_for): её можно двигать мышью, а параметр
    # без заданного положения садится в начало координат компонента, и
    # получается вторая надпись, которую не сдвинуть.
    if classify.shows_value(c.ctype):
        put("Value", c.value or (c.params or {}).get("Value")
            or classify.default_value(c.ctype))
    else:
        put("Value", c.value)
    put("Datasheet", c.datasheet)
    for k, v in (c.params or {}).items():
        put(k, v)
    for fp in c.footprints:
        if fp.model and getattr(fp.model, "tape_rot", 0):
            put("УголВЛенте", f"{float(fp.model.tape_rot):g}")
            break
    put("GostLibUID", c.uid)
    put("GostLibSource", c.source)
    return out


def _emit_component(j: _Job, c: Component, st: Style):
    st = st.for_component(c)      # личные настройки этого компонента
    sym = c.symbol
    # Подпись на схеме -- штатный Comment Altium, а не наша графика: его
    # можно двигать и править прямо на листе. У пассивок это номинал, у
    # остального -- парт-номер.
    comment = classify.label_for(c)
    j.add("COMP", esc(c.name), esc(c.designator or "U?"),
          esc(c.description), esc(comment), max(1, int(sym.part_count)))
    dx, dy = (sym.designator_pos or [0, 100])[:2]
    cx, cy = (sym.comment_pos or [0, -100])[:2]
    j.add("DESIG", mil(dx), mil(dy))
    j.add("CMTPOS", mil(cx), mil(cy))
    for name, value, hidden in _params_of(c):
        j.add("PARAM", esc(name), esc(value), hidden)
    for fp in c.footprints:
        ref = fp.source_name or fp.name
        if not ref:
            continue
        j.add("FPREF", esc(ref), esc(os.path.basename(fp.source_pcblib)
                                     if fp.source_pcblib else ""))
    _emit_symbol(j, sym, st)
    j.add("ENDCOMP")


# ------------------------------------------------------------------ сборка --
def build_job(components: Sequence[Component], schlib: str, pcblib: str,
              st: Optional[Style] = None, log_path: str = "",
              font: str = "", install: bool = True,
              vendor_libs: Optional[Iterable[str]] = None,
              fresh: bool = True, pin_hot_end: bool = False,
              ascii_only: bool = False, intlib: bool = False,
              libpkg: str = "") -> str:
    """Собрать текст задания."""
    global _ASCII_ONLY
    _ASCII_ONLY = ascii_only
    st = st or DEFAULT
    j = _Job()
    j.add("VER", VERSION)
    if log_path:
        j.add("LOG", esc(log_path))
    j.add("SCHLIB", esc(schlib))
    j.add("PCBLIB", esc(pcblib))
    j.add("FONT", esc(font or st.font))
    j.add("INSTALL", 1 if install else 0)
    j.add("FRESH", 1 if fresh else 0)
    # 0 -- Location вывода это конец у корпуса. Проверено на живом Altium:
    # при 1 выводы уезжают наружу ровно на свою длину.
    j.add("PINLOC", 1 if pin_hot_end else 0)
    j.add("INTLIB", 1 if intlib else 0)
    # цвета Altium -- целое BGR; 0 = чёрный
    j.add("COLGRAPH", int(getattr(st, "color_graphic", 0)))
    j.add("COLTEXT", int(getattr(st, "color_text", 0)))
    j.add("COLPIN", int(getattr(st, "color_pin", 0)))
    # Тип микросхемы мы рисуем своим текстом шрифтом ГОСТ. Штатный Comment
    # Altium показывает то же самое, и на УГО выходит две одинаковые надписи.
    j.add("HIDECMT", 1 if getattr(st, "draw_type_label", True) else 0)
    if libpkg:
        j.add("LIBPKG", esc(libpkg))
    for lib in (vendor_libs or []):
        j.add("VENDOR", esc(lib))

    # посадочные места: сперва все, потом компоненты на них ссылаются
    seen = set()
    n_fp = 0
    for c in components:
        for fp in c.footprints:
            if fp.is_external() or not (fp.pads or fp.prims):
                continue
            if fp.name in seen:
                continue
            seen.add(fp.name)
            _emit_footprint(j, fp)
            n_fp += 1

    n_c = 0
    for c in components:
        if not c.name:
            continue
        _emit_component(j, c, st)
        n_c += 1

    j.add("END", n_c, n_fp)
    return j.text()


def write_job(path: str, components: Sequence[Component], schlib: str,
              pcblib: str, st: Optional[Style] = None, log_path: str = "",
              font: str = "", install: bool = True,
              vendor_libs: Optional[Iterable[str]] = None,
              fresh: bool = True, pin_hot_end: bool = False,
              ascii_only: bool = False, intlib: bool = False,
              libpkg: str = "") -> str:
    text = build_job(components, schlib, pcblib, st=st, log_path=log_path,
                     font=font, install=install, vendor_libs=vendor_libs,
                     fresh=fresh, pin_hot_end=pin_hot_end,
                     ascii_only=ascii_only, intlib=intlib, libpkg=libpkg)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    enc = "ascii" if ascii_only else ENCODING
    with open(path, "w", encoding=enc, errors="replace",
              newline="\r\n") as f:
        f.write(text)
    return path


def write_pointer(root: str, job_path: str) -> str:
    """Файл-указатель, который читает скрипт внутри Altium."""
    p = os.path.join(root, "current_job.txt")
    os.makedirs(root, exist_ok=True)
    with open(p, "w", encoding="ascii", errors="replace") as f:
        f.write(job_path + "\n")
    return p
