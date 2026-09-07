"""
Запись библиотеки в формате EAGLE .lbr (XML 6.x-9.4).

Зачем: Altium умеет импортировать EAGLE-библиотеки штатным мастером
(File > Import Wizard > "EAGLE Projects and Designs"), и делает это сам --
то есть .SchLib и .PcbLib создаёт САМ Altium, а не мы. Это снимает весь
риск «битого» бинарника и «I/O error 32».

Документация Altium: EAGLE Importer принимает XML-файлы EAGLE версий
6.4 ... 9.4, поэтому в заголовке пишем version="9.4.0".
Двоичные .lbr (EAGLE < 6) он не берёт -- нам это и не нужно.

Единицы EAGLE: везде миллиметры (обычные десятичные числа), ось Y вверх.
Символы: графика на слое 94, имена 95, номиналы 96.
Посадки: медь 1/16, шелкография 21/22, сборочный 51, keepout 39.
"""
from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, Tuple
from xml.sax.saxutils import escape, quoteattr

from ..ir import Component, Footprint, Pad, FpPrim, Symbol, SymPin, SymPrim

MM = 0.0254                      # мил -> мм
PT2MIL = 1000.0 / 72.0
TEXT_K = PT2MIL * MM * 0.70      # пункты -> мм (высота прописной буквы)

EAGLE_VERSION = "9.4.0"

# --- слои, которые реально используем -----------------------------------------
LAYERS: List[Tuple[int, str, int, int]] = [
    (1, "Top", 4, 1), (16, "Bottom", 1, 1), (17, "Pads", 2, 1),
    (18, "Vias", 2, 1), (19, "Unrouted", 6, 1), (20, "Dimension", 24, 1),
    (21, "tPlace", 16, 1), (22, "bPlace", 14, 1),
    (23, "tOrigins", 15, 1), (24, "bOrigins", 15, 1),
    (25, "tNames", 7, 1), (26, "bNames", 7, 1),
    (27, "tValues", 7, 1), (28, "bValues", 7, 1),
    (29, "tStop", 7, 3), (30, "bStop", 7, 6),
    (31, "tCream", 7, 4), (32, "bCream", 7, 5),
    (39, "tKeepout", 4, 11), (40, "bKeepout", 1, 11),
    (41, "tRestrict", 4, 10), (42, "bRestrict", 1, 10),
    (43, "vRestrict", 2, 10),
    (44, "Drills", 7, 1), (45, "Holes", 7, 1), (46, "Milling", 3, 1),
    (47, "Measures", 7, 1), (48, "Document", 7, 1), (49, "Reference", 7, 1),
    (51, "tDocu", 7, 1), (52, "bDocu", 7, 1),
    (91, "Nets", 2, 1), (92, "Busses", 1, 1), (93, "Pins", 2, 1),
    (94, "Symbols", 4, 1), (95, "Names", 7, 1), (96, "Values", 7, 1),
    (97, "Info", 7, 1), (98, "Guide", 6, 1),
]

FP_LAYER = {
    "silk": 21, "silk_bot": 22, "assy": 51, "assy_bot": 52,
    "courtyard": 39, "keepout": 39, "mech": 51,
    "copper_top": 1, "copper_bot": 16,
}

# электрический тип вывода -> direction EAGLE
DIRECTION = {
    "input": "in", "output": "out", "io": "io",
    "open_collector": "oc", "open_emitter": "oc",
    "passive": "pas", "hiz": "hiz", "power": "pwr",
}

# длина вывода EAGLE фиксирована сеткой 0.1"
PIN_LEN = [(0, "point"), (100, "short"), (200, "middle"), (300, "long")]

ALIGN_H = {0: "left", 1: "center", 2: "right"}
ALIGN_V = {0: "bottom", 1: "center", 2: "top"}


def _align(just: int, vjust: int) -> str:
    """EAGLE не знает 'center-center' -- у него это просто 'center'."""
    h = ALIGN_H.get(just, "left")
    v = ALIGN_V.get(vjust, "bottom")
    return "center" if (h == "center" and v == "center") else f"{v}-{h}"


def _f(v: float) -> str:
    """Число в том виде, в котором его пишет EAGLE: до 4 знаков, без хвостов."""
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _name(s: str, fallback: str = "X") -> str:
    """Имя, пригодное для EAGLE (он не любит пробелы и спецсимволы)."""
    s = (s or "").strip()
    s = re.sub(r"[^A-Za-z0-9_\-\.\+#$/]", "_", s)
    s = s.strip("_")
    return (s or fallback).upper()


def _pin_len(mil: int) -> Tuple[str, int]:
    best = min(PIN_LEN, key=lambda t: abs(t[0] - mil))
    return best[1], best[0]


def _rot(deg: float) -> str:
    d = int(round(float(deg))) % 360
    d = min((0, 90, 180, 270), key=lambda x: min(abs(d - x), 360 - abs(d - x)))
    return f"R{d}"


# ==============================================================================
#  СИМВОЛ
# ==============================================================================

def _symbol_xml(name: str, sym: Symbol, unit: int, out: List[str]) -> None:
    out.append(f'<symbol name={quoteattr(name)}>')

    for pr in sym.prims:
        if pr.unit not in (0, unit):
            continue
        _sym_prim(pr, out)

    # >NAME / >VALUE -- позиционное обозначение и номинал
    dx, dy = (sym.designator_pos + [0, 0])[:2]
    cx, cy = (sym.comment_pos + [0, 0])[:2]
    out.append(f'<text x="{_f(dx*MM)}" y="{_f(dy*MM)}" size="1.778" '
               f'layer="95" font="vector" ratio="10">&gt;NAME</text>')
    out.append(f'<text x="{_f(cx*MM)}" y="{_f(cy*MM)}" size="1.778" '
               f'layer="96" font="vector" ratio="10">&gt;VALUE</text>')

    used: Dict[str, int] = {}
    for p in sym.pins:
        if p.unit not in (0, unit):
            continue
        pn = _pin_name(p, used)
        ln, lmil = _pin_len(int(p.length))
        # (p.x, p.y) -- точка на корпусе; вывод уходит наружу по p.rotation.
        a = math.radians(p.rotation)
        ex = p.x + lmil * math.cos(a)
        ey = p.y + lmil * math.sin(a)
        # в EAGLE (x,y) -- свободный конец, а rot задаёт направление НА корпус
        erot = _rot((p.rotation + 180) % 360)
        vis = "off" if not (p.show_name or p.show_number) else "both"
        if p.show_name and not p.show_number:
            vis = "pin"
        elif p.show_number and not p.show_name:
            vis = "pad"
        out.append(
            f'<pin name={quoteattr(pn)} x="{_f(ex*MM)}" y="{_f(ey*MM)}" '
            f'visible="{vis}" length="{ln}" '
            f'direction="{DIRECTION.get(p.etype, "pas")}" '
            f'function="{"dot" if p.inverted else ("clk" if p.clock else "none")}" '
            f'swaplevel="0" rot="{erot}"/>')
    out.append('</symbol>')


def _pin_name(p: SymPin, used: Dict[str, int]) -> str:
    """
    Имя вывода в EAGLE -- ключ для <connect>. Берём номер контакта:
    он уникален и совпадает с именем площадки в посадке.
    """
    base = _name(p.number or p.name, "P")
    n = used.get(base, 0)
    used[base] = n + 1
    return base if n == 0 else f"{base}@{n}"


def _sym_prim(pr: SymPrim, out: List[str]) -> None:
    w = {0: 0.1524, 1: 0.254, 2: 0.508, 3: 0.762}.get(int(pr.width), 0.254)
    L = 94
    if pr.kind == "line" or pr.kind == "poly":
        pts = pr.pts
        if pr.kind == "poly" and pr.filled and len(pts) >= 3:
            out.append(f'<polygon width="{_f(w)}" layer="{L}" pour="solid">')
            for x, y in pts:
                out.append(f'<vertex x="{_f(x*MM)}" y="{_f(y*MM)}"/>')
            out.append('</polygon>')
            return
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            out.append(f'<wire x1="{_f(x1*MM)}" y1="{_f(y1*MM)}" '
                       f'x2="{_f(x2*MM)}" y2="{_f(y2*MM)}" '
                       f'width="{_f(w)}" layer="{L}"/>')
    elif pr.kind == "rect":
        (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
        if pr.filled:
            out.append(f'<rectangle x1="{_f(min(x1,x2)*MM)}" '
                       f'y1="{_f(min(y1,y2)*MM)}" x2="{_f(max(x1,x2)*MM)}" '
                       f'y2="{_f(max(y1,y2)*MM)}" layer="{L}"/>')
        else:
            for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)),
                         ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
                out.append(f'<wire x1="{_f(a[0]*MM)}" y1="{_f(a[1]*MM)}" '
                           f'x2="{_f(b[0]*MM)}" y2="{_f(b[1]*MM)}" '
                           f'width="{_f(w)}" layer="{L}"/>')
    elif pr.kind == "ellipse":
        cx, cy = pr.pts[0] if pr.pts else (0, 0)
        out.append(f'<circle x="{_f(cx*MM)}" y="{_f(cy*MM)}" '
                   f'radius="{_f(max(pr.radius,1)*MM)}" '
                   f'width="{_f(0 if pr.filled else w)}" layer="{L}"/>')
    elif pr.kind == "arc":
        cx, cy = pr.pts[0] if pr.pts else (0, 0)
        _arc_wires(cx, cy, pr.radius, pr.a1, pr.a2, w, L, MM, out)
    elif pr.kind == "text":
        al = _align(pr.justify, pr.vjustify)
        x, y = pr.pts[0] if pr.pts else (0, 0)
        out.append(f'<text x="{_f(x*MM)}" y="{_f(y*MM)}" '
                   f'size="{_f(max(pr.size*TEXT_K, 0.4))}" layer="{L}" '
                   f'font="vector" ratio="10" rot="{_rot(pr.rotation)}" '
                   f'align="{al}">{escape(pr.text)}</text>')


def _arc_wires(cx, cy, r, a1, a2, w, layer, k, out, steps=0):
    span = (a2 - a1) % 360 or 360
    steps = steps or max(6, int(span / 12))
    pa = math.radians(a1)
    for i in range(1, steps + 1):
        pb = math.radians(a1 + span * i / steps)
        out.append(
            f'<wire x1="{_f((cx+r*math.cos(pa))*k)}" '
            f'y1="{_f((cy+r*math.sin(pa))*k)}" '
            f'x2="{_f((cx+r*math.cos(pb))*k)}" '
            f'y2="{_f((cy+r*math.sin(pb))*k)}" '
            f'width="{_f(w)}" layer="{layer}"/>')
        pa = pb


# ==============================================================================
#  ПОСАДОЧНОЕ МЕСТО
# ==============================================================================

def _pad_names(fp: Footprint) -> List[str]:
    """
    В EAGLE имена площадок внутри корпуса обязаны быть уникальными,
    а в KiCad номера повторяются (тепловые площадки, экраны). Дубли
    получают суффикс, а <connect> потом перечисляет их через пробел.
    """
    seen: Dict[str, int] = {}
    names: List[str] = []
    for i, p in enumerate(fp.pads):
        base = _name(p.number or str(i + 1), str(i + 1))
        n = seen.get(base, 0)
        seen[base] = n + 1
        names.append(base if n == 0 else f"{base}@{n}")
    return names


def _package_xml(name: str, fp: Footprint, out: List[str]) -> None:
    out.append(f'<package name={quoteattr(name)}>')
    if fp.description:
        out.append(f'<description>{escape(fp.description)}</description>')

    for pname, p in zip(_pad_names(fp), fp.pads):
        _pad_xml(pname, p, out)

    for pr in fp.prims:
        _fp_prim(pr, out)

    out.append('<text x="0" y="0" size="1" layer="25" font="vector" '
               'ratio="10" align="center">&gt;NAME</text>')
    out.append('</package>')


def _pad_xml(pname: str, p: Pad, out: List[str]) -> None:
    w = max(float(p.w), 0.05)
    h = max(float(p.h), 0.05)
    if p.hole and p.hole > 0:
        shape = {"rect": "square", "roundrect": "square", "round": "round",
                 "oval": "long", "octagon": "octagon"}.get(p.shape, "round")
        out.append(
            f'<pad name={quoteattr(pname)} x="{_f(p.x)}" y="{_f(p.y)}" '
            f'drill="{_f(max(p.hole, 0.1))}" diameter="{_f(max(w, h))}" '
            f'shape="{shape}" rot="{_rot(p.rot)}"/>')
    else:
        rnd = 0
        if p.shape in ("round", "oval"):
            rnd = 100
        elif p.shape == "roundrect":
            rnd = int(max(0, min(100, round(p.corner_radius * 2))))
        layer = 16 if p.layer == "bottom" else 1
        out.append(
            f'<smd name={quoteattr(pname)} x="{_f(p.x)}" y="{_f(p.y)}" '
            f'dx="{_f(w)}" dy="{_f(h)}" layer="{layer}" '
            f'roundness="{rnd}" rot="{_rot(p.rot)}"/>')


def _fp_prim(pr: FpPrim, out: List[str]) -> None:
    L = FP_LAYER.get(pr.layer, 21)
    w = max(float(pr.width), 0.01)
    if pr.kind == "line":
        pts = pr.pts
        for i in range(len(pts) - 1):
            out.append(f'<wire x1="{_f(pts[i][0])}" y1="{_f(pts[i][1])}" '
                       f'x2="{_f(pts[i+1][0])}" y2="{_f(pts[i+1][1])}" '
                       f'width="{_f(w)}" layer="{L}"/>')
    elif pr.kind == "rect":
        (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
        if pr.filled:
            out.append(f'<rectangle x1="{_f(min(x1,x2))}" y1="{_f(min(y1,y2))}" '
                       f'x2="{_f(max(x1,x2))}" y2="{_f(max(y1,y2))}" '
                       f'layer="{L}"/>')
        else:
            for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)),
                         ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
                out.append(f'<wire x1="{_f(a[0])}" y1="{_f(a[1])}" '
                           f'x2="{_f(b[0])}" y2="{_f(b[1])}" '
                           f'width="{_f(w)}" layer="{L}"/>')
    elif pr.kind == "circle":
        cx, cy = pr.pts[0] if pr.pts else (0, 0)
        out.append(f'<circle x="{_f(cx)}" y="{_f(cy)}" '
                   f'radius="{_f(max(pr.radius, 0.01))}" '
                   f'width="{_f(0 if pr.filled else w)}" layer="{L}"/>')
    elif pr.kind == "arc":
        cx, cy = pr.pts[0] if pr.pts else (0, 0)
        _arc_wires(cx, cy, pr.radius, pr.a1, pr.a2, w, L, 1.0, out)
    elif pr.kind in ("poly", "region"):
        if len(pr.pts) >= 3:
            out.append(f'<polygon width="{_f(w)}" layer="{L}" pour="solid">')
            for x, y in pr.pts:
                out.append(f'<vertex x="{_f(x)}" y="{_f(y)}"/>')
            out.append('</polygon>')


# ==============================================================================
#  СБОРКА БИБЛИОТЕКИ
# ==============================================================================

def write_lbr(path: str, components: Iterable[Component],
              lib_name: str = "GOST_Lib") -> str:
    comps = [c for c in components if c and c.symbol and c.symbol.pins]
    out: List[str] = []
    out.append('<?xml version="1.0" encoding="utf-8"?>')
    out.append('<!DOCTYPE eagle SYSTEM "eagle.dtd">')
    out.append(f'<eagle version="{EAGLE_VERSION}">')
    out.append('<drawing>')
    out.append('<settings>')
    out.append('<setting alwaysvectorfont="no"/>')
    out.append('<setting verticaltext="up"/>')
    out.append('</settings>')
    out.append('<grid distance="0.1" unitdist="inch" unit="inch" style="lines" '
               'multiple="1" display="no" altdistance="0.01" '
               'altunitdist="inch" altunit="inch"/>')
    out.append('<layers>')
    for num, nm, col, fill in LAYERS:
        out.append(f'<layer number="{num}" name="{nm}" color="{col}" '
                   f'fill="{fill}" visible="yes" active="yes"/>')
    out.append('</layers>')
    out.append(f'<library name={quoteattr(_name(lib_name, "LIB"))}>')
    out.append('<description>GostLib -- ЕСКД/ГОСТ, экспорт для Altium</description>')

    packages: List[str] = []
    symbols: List[str] = []
    devsets: List[str] = []

    pkg_done: Dict[str, Footprint] = {}
    sym_used: Dict[str, int] = {}
    dev_used: Dict[str, int] = {}

    stats = {"components": 0, "packages": 0, "symbols": 0, "skipped": []}

    for c in comps:
        units = sorted({p.unit or 1 for p in c.symbol.pins}) or [1]
        base = _name(c.name, "PART")

        # --- символы (по одному на секцию) ---
        gate_syms: List[Tuple[str, str]] = []
        for i, u in enumerate(units):
            sname = base if len(units) == 1 else f"{base}_{i+1}"
            n = sym_used.get(sname, 0)
            sym_used[sname] = n + 1
            if n:
                sname = f"{sname}${n}"
            _symbol_xml(sname, c.symbol, u, symbols)
            gate_syms.append((f"G${i+1}", sname))
            stats["symbols"] += 1

        # --- корпуса ---
        dev_pkgs: List[Tuple[str, Footprint, str]] = []
        for fp in c.footprints:
            if not fp.pads:
                continue
            pname = _name(fp.name, "PKG")
            if pname not in pkg_done:
                pkg_done[pname] = fp
                _package_xml(pname, fp, packages)
                stats["packages"] += 1
            suffix = "" if len(c.footprints) == 1 else "-" + pname
            dev_pkgs.append((pname, fp, suffix))

        # --- deviceset ---
        dname = base
        n = dev_used.get(dname, 0)
        dev_used[dname] = n + 1
        if n:
            dname = f"{dname}${n}"
        prefix = _name(re.sub(r"[\d?]+$", "", c.designator or "U"), "U")

        devsets.append(f'<deviceset name={quoteattr(dname)} '
                       f'prefix={quoteattr(prefix)} uservalue="yes">')
        if c.description:
            devsets.append(f'<description>{escape(c.description)}</description>')
        devsets.append('<gates>')
        for i, (gname, sname) in enumerate(gate_syms):
            devsets.append(f'<gate name="{gname}" symbol={quoteattr(sname)} '
                           f'x="0" y="{_f(-i * 25.4)}" addlevel="next" '
                           f'swaplevel="0"/>')
        devsets.append('</gates>')
        devsets.append('<devices>')
        if not dev_pkgs:
            devsets.append('<device name="">')
            devsets.append('<technologies><technology name=""/></technologies>')
            devsets.append('</device>')
        for pname, fp, suffix in dev_pkgs:
            devsets.append(f'<device name={quoteattr(suffix)} '
                           f'package={quoteattr(pname)}>')
            devsets.append('<connects>')
            for line in _connects(c, fp, gate_syms, units):
                devsets.append(line)
            devsets.append('</connects>')
            devsets.append('<technologies><technology name=""/></technologies>')
            devsets.append('</device>')
        devsets.append('</devices>')
        devsets.append('</deviceset>')
        stats["components"] += 1

    out.append('<packages>')
    out += packages
    out.append('</packages>')
    out.append('<symbols>')
    out += symbols
    out.append('</symbols>')
    out.append('<devicesets>')
    out += devsets
    out.append('</devicesets>')
    out.append('</library>')
    out.append('</drawing>')
    out.append('</eagle>')

    text = "\n".join(out) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    write_lbr.last_stats = stats          # type: ignore[attr-defined]
    return path


def _connects(c: Component, fp: Footprint,
              gate_syms: List[Tuple[str, str]], units: List[int]) -> List[str]:
    """Связь «вывод символа -> площадка корпуса» по номеру контакта."""
    pad_by_num: Dict[str, List[str]] = {}
    for pname, p in zip(_pad_names(fp), fp.pads):
        key = _name(p.number or "", "")
        pad_by_num.setdefault(key, []).append(pname)

    res: List[str] = []
    for gi, u in enumerate(units):
        gname = gate_syms[gi][0]
        used: Dict[str, int] = {}
        for p in c.symbol.pins:
            if p.unit not in (0, u):
                continue
            pin = _pin_name(p, used)
            pads = pad_by_num.get(_name(p.number or "", ""), [])
            if not pads:
                continue
            res.append(f'<connect gate="{gname}" pin={quoteattr(pin)} '
                       f'pad={quoteattr(" ".join(pads))} route="all"/>')
    return res
