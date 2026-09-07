"""
Запись библиотеки обратно в формат KiCad (.kicad_sym + .pretty/*.kicad_mod).

Зачем: в Altium 25/26 есть ШТАТНЫЙ импортёр KiCad (расширение «KiCad Importer»,
File > Import Wizard > "KiCad Design Files"). Он принимает *.kicad_sym и
*.kicad_mod и сам делает из них *.SchLib и *.PcbLib.

То есть мы отдаём Altium текстовые файлы, которые он официально понимает,
а бинарные библиотеки он собирает сам. При этом символы -- уже ГОСТовские,
потому что мы пишем не исходный символ KiCad, а свой, перестроенный.

Единицы KiCad: миллиметры.
  * .kicad_sym -- ось Y ВВЕРХ (как у нас),
  * .kicad_mod -- ось Y ВНИЗ (инвертируем).
"""
from __future__ import annotations

import math
import os
import re
from typing import Dict, Iterable, List

from ..ir import Component, Footprint, FpPrim, Pad, Symbol, SymPin, SymPrim

MM = 0.0254
PT2MIL = 1000.0 / 72.0
TEXT_K = PT2MIL * MM * 0.70

SYM_VERSION = "20231120"
FP_VERSION = "20221018"

ETYPE = {
    "input": "input", "output": "output", "io": "bidirectional",
    "open_collector": "open_collector", "open_emitter": "open_emitter",
    "passive": "passive", "hiz": "tri_state", "power": "power_in",
}

FP_LAYER = {
    "silk": "F.SilkS", "silk_bot": "B.SilkS",
    "assy": "F.Fab", "assy_bot": "B.Fab",
    "courtyard": "F.CrtYd", "keepout": "F.CrtYd",
    "mech": "Dwgs.User", "copper_top": "F.Cu", "copper_bot": "B.Cu",
}


def _f(v: float) -> str:
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _q(s: str) -> str:
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _safe(s: str, fallback: str = "X") -> str:
    s = re.sub(r'[\\/:*?"<>|\s]', "_", (s or "").strip())
    return s or fallback


# ==============================================================================
#  .kicad_sym
# ==============================================================================

def _eff(size_mm: float, hide: bool = False, just: str = "") -> str:
    j = f" (justify {just})" if just else ""
    return (f"(effects (font (size {_f(size_mm)} {_f(size_mm)}))"
            f"{j}{' hide' if hide else ''})")


def _sym_graphics(sym: Symbol, unit: int, out: List[str], ind: str) -> None:
    for pr in sym.prims:
        if pr.unit not in (0, unit):
            continue
        w = {0: 0.1524, 1: 0.254, 2: 0.508, 3: 0.762}.get(int(pr.width), 0.254)
        stroke = f"(stroke (width {_f(w)}) (type default))"
        fill = f"(fill (type {'outline' if pr.filled else 'none'}))"
        if pr.kind == "rect" and len(pr.pts) >= 2:
            (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
            out.append(f"{ind}(rectangle (start {_f(x1*MM)} {_f(y1*MM)}) "
                       f"(end {_f(x2*MM)} {_f(y2*MM)}) {stroke} {fill})")
        elif pr.kind in ("line", "poly") and len(pr.pts) >= 2:
            pts = " ".join(f"(xy {_f(x*MM)} {_f(y*MM)})" for x, y in pr.pts)
            out.append(f"{ind}(polyline (pts {pts}) {stroke} {fill})")
        elif pr.kind == "ellipse":
            cx, cy = pr.pts[0] if pr.pts else (0, 0)
            out.append(f"{ind}(circle (center {_f(cx*MM)} {_f(cy*MM)}) "
                       f"(radius {_f(max(pr.radius,1)*MM)}) {stroke} {fill})")
        elif pr.kind == "arc":
            cx, cy = pr.pts[0] if pr.pts else (0, 0)
            r = max(pr.radius, 1)
            a1, a2 = pr.a1, pr.a2
            am = a1 + ((a2 - a1) % 360 or 360) / 2.0
            p = [(cx + r * math.cos(math.radians(a)),
                  cy + r * math.sin(math.radians(a))) for a in (a1, am, a2)]
            out.append(f"{ind}(arc (start {_f(p[0][0]*MM)} {_f(p[0][1]*MM)}) "
                       f"(mid {_f(p[1][0]*MM)} {_f(p[1][1]*MM)}) "
                       f"(end {_f(p[2][0]*MM)} {_f(p[2][1]*MM)}) "
                       f"{stroke} {fill})")
        elif pr.kind == "text" and pr.text:
            x, y = pr.pts[0] if pr.pts else (0, 0)
            just = {0: "left", 1: "", 2: "right"}.get(pr.justify, "")
            vj = {0: "bottom", 1: "", 2: "top"}.get(pr.vjustify, "")
            jj = " ".join(t for t in (just, vj) if t)
            out.append(f"{ind}(text {_q(pr.text)} "
                       f"(at {_f(x*MM)} {_f(y*MM)} {int(pr.rotation)%360}) "
                       f"{_eff(max(pr.size*TEXT_K, 0.4), just=jj)})")


def _sym_pins(sym: Symbol, unit: int, out: List[str], ind: str) -> None:
    for p in sym.pins:
        if p.unit not in (0, unit):
            continue
        a = math.radians(p.rotation)
        ex = p.x + p.length * math.cos(a)
        ey = p.y + p.length * math.sin(a)
        ang = (int(p.rotation) + 180) % 360     # KiCad: угол «от вывода к корпусу»
        shape = "inverted" if p.inverted else ("clock" if p.clock else "line")
        out.append(f"{ind}(pin {ETYPE.get(p.etype,'passive')} {shape} "
                   f"(at {_f(ex*MM)} {_f(ey*MM)} {ang}) "
                   f"(length {_f(p.length*MM)}){' hide' if p.hidden else ''}")
        out.append(f"{ind}  (name {_q(p.name or '~')} "
                   f"{_eff(1.27, hide=not p.show_name)})")
        out.append(f"{ind}  (number {_q(p.number or '~')} "
                   f"{_eff(1.27, hide=not p.show_number)})")
        out.append(f"{ind})")


def write_kicad_sym(path: str, components: Iterable[Component],
                    fp_lib: str = "GOST_Lib") -> str:
    out: List[str] = []
    out.append(f'(kicad_symbol_lib (version {SYM_VERSION}) (generator "gostlib")')
    used: Dict[str, int] = {}
    for c in components:
        if not c or not c.symbol or not c.symbol.pins:
            continue
        name = _safe(c.name, "PART")
        n = used.get(name, 0)
        used[name] = n + 1
        if n:
            name = f"{name}_{n}"
        units = sorted({p.unit or 1 for p in c.symbol.pins}) or [1]
        dx, dy = (c.symbol.designator_pos + [0, 0])[:2]
        cx, cy = (c.symbol.comment_pos + [0, 0])[:2]
        fp0 = c.footprints[0].name if c.footprints else ""

        out.append(f'  (symbol {_q(name)}')
        out.append('    (pin_names (offset 0.254) hide)')
        out.append('    (exclude_from_sim no) (in_bom yes) (on_board yes)')
        out.append(f'    (property "Reference" {_q(c.designator or "U")} '
                   f'(at {_f(dx*MM)} {_f(dy*MM)} 0) {_eff(1.27)})')
        out.append(f'    (property "Value" {_q(c.value or name)} '
                   f'(at {_f(cx*MM)} {_f(cy*MM)} 0) {_eff(1.27)})')
        out.append(f'    (property "Footprint" '
                   f'{_q(f"{fp_lib}:{_safe(fp0)}" if fp0 else "")} '
                   f'(at 0 0 0) {_eff(1.27, hide=True)})')
        out.append(f'    (property "Datasheet" {_q(c.datasheet)} '
                   f'(at 0 0 0) {_eff(1.27, hide=True)})')
        out.append(f'    (property "Description" {_q(c.description)} '
                   f'(at 0 0 0) {_eff(1.27, hide=True)})')
        for k, v in (c.params or {}).items():
            if not k or not v:
                continue
            out.append(f'    (property {_q(str(k))} {_q(str(v))} '
                       f'(at 0 0 0) {_eff(1.27, hide=True)})')
        for i, u in enumerate(units, start=1):
            out.append(f'    (symbol "{name}_{i}_1"')
            _sym_graphics(c.symbol, u, out, "      ")
            _sym_pins(c.symbol, u, out, "      ")
            out.append('    )')
        out.append('  )')
    out.append(')')
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    return path


# ==============================================================================
#  .kicad_mod
# ==============================================================================

def _pad_sexp(p: Pad, out: List[str]) -> None:
    w = max(float(p.w), 0.05)
    h = max(float(p.h), 0.05)
    y = -float(p.y)
    rot = (-float(p.rot)) % 360
    at = f"(at {_f(p.x)} {_f(y)}{'' if abs(rot) < 1e-6 else ' ' + _f(rot)})"
    extra = ""
    if p.hole and p.hole > 0:
        ptype = "thru_hole" if p.plated else "np_thru_hole"
        shape = {"rect": "rect", "roundrect": "roundrect", "round": "circle",
                 "oval": "oval", "octagon": "circle"}.get(p.shape, "circle")
        layers = '"*.Cu" "*.Mask"' if p.plated else '"F&B.Cu" "*.Mask"'
        drill = (f"(drill oval {_f(p.hole)} {_f(p.hole_len)})"
                 if p.hole_len else f"(drill {_f(p.hole)})")
    else:
        ptype = "smd"
        shape = {"rect": "rect", "roundrect": "roundrect", "round": "circle",
                 "oval": "oval", "octagon": "circle"}.get(p.shape, "rect")
        side = "B" if p.layer == "bottom" else "F"
        layers = f'"{side}.Cu" "{side}.Paste" "{side}.Mask"'
        drill = ""
    if shape == "roundrect":
        rr = max(0.0, min(0.5, float(p.corner_radius) / 100.0)) or 0.25
        extra = f" (roundrect_rratio {_f(rr)})"
    out.append(f'  (pad {_q(p.number)} {ptype} {shape} {at} '
               f'(size {_f(w)} {_f(h)}) {drill}(layers {layers}){extra})')


def _fpprim_sexp(pr: FpPrim, out: List[str]) -> None:
    L = FP_LAYER.get(pr.layer, "F.SilkS")
    w = max(float(pr.width), 0.01)
    st = f"(stroke (width {_f(w)}) (type solid))"
    if pr.kind == "line":
        for i in range(len(pr.pts) - 1):
            a, b = pr.pts[i], pr.pts[i + 1]
            out.append(f'  (fp_line (start {_f(a[0])} {_f(-a[1])}) '
                       f'(end {_f(b[0])} {_f(-b[1])}) {st} (layer "{L}"))')
    elif pr.kind == "rect" and len(pr.pts) >= 2:
        (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
        fill = "solid" if pr.filled else "none"
        out.append(f'  (fp_rect (start {_f(x1)} {_f(-y1)}) '
                   f'(end {_f(x2)} {_f(-y2)}) {st} (fill {fill}) (layer "{L}"))')
    elif pr.kind == "circle":
        cx, cy = pr.pts[0] if pr.pts else (0, 0)
        r = max(pr.radius, 0.01)
        fill = "solid" if pr.filled else "none"
        out.append(f'  (fp_circle (center {_f(cx)} {_f(-cy)}) '
                   f'(end {_f(cx + r)} {_f(-cy)}) {st} (fill {fill}) '
                   f'(layer "{L}"))')
    elif pr.kind == "arc":
        cx, cy = pr.pts[0] if pr.pts else (0, 0)
        r = max(pr.radius, 0.01)
        a1, a2 = pr.a1, pr.a2
        am = a1 + ((a2 - a1) % 360 or 360) / 2.0
        p = [(cx + r * math.cos(math.radians(a)),
              -(cy + r * math.sin(math.radians(a)))) for a in (a1, am, a2)]
        out.append(f'  (fp_arc (start {_f(p[0][0])} {_f(p[0][1])}) '
                   f'(mid {_f(p[1][0])} {_f(p[1][1])}) '
                   f'(end {_f(p[2][0])} {_f(p[2][1])}) {st} (layer "{L}"))')
    elif pr.kind in ("poly", "region") and len(pr.pts) >= 3:
        pts = " ".join(f"(xy {_f(x)} {_f(-y)})" for x, y in pr.pts)
        fill = "solid" if pr.filled else "none"
        out.append(f'  (fp_poly (pts {pts}) {st} (fill {fill}) (layer "{L}"))')


def write_kicad_mod(path: str, fp: Footprint) -> str:
    name = _safe(fp.name, "FP")
    smd = any((not p.hole) for p in fp.pads)
    tht = any(p.hole for p in fp.pads)
    attr = "smd" if smd and not tht else ("through_hole" if tht else "")
    out: List[str] = []
    out.append(f'(footprint {_q(name)} (version {FP_VERSION}) '
               f'(generator "gostlib") (layer "F.Cu")')
    if fp.description:
        out.append(f'  (descr {_q(fp.description)})')
    if attr:
        out.append(f'  (attr {attr})')
    out.append('  (fp_text reference "REF**" (at 0 -2) (layer "F.SilkS") '
               '(effects (font (size 1 1) (thickness 0.15))))')
    out.append(f'  (fp_text value {_q(name)} (at 0 2) (layer "F.Fab") '
               '(effects (font (size 1 1) (thickness 0.15))))')
    for pr in fp.prims:
        _fpprim_sexp(pr, out)
    for p in fp.pads:
        _pad_sexp(p, out)
    if fp.model and fp.model.path:
        m = fp.model
        out.append(f'  (model {_q(m.path)}')
        out.append(f'    (offset (xyz {_f(m.dx)} {_f(m.dy)} {_f(m.dz)}))')
        out.append('    (scale (xyz 1 1 1))')
        out.append(f'    (rotate (xyz {_f(m.rx)} {_f(m.ry)} {_f(m.rz)}))')
        out.append('  )')
    out.append(')')
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    return path


def write_kicad_bundle(root: str, components: Iterable[Component],
                       lib_name: str = "GOST_Lib") -> Dict[str, object]:
    """
    Готовит папку под штатный импортёр Altium:

        <root>/GOST_Lib.kicad_sym
        <root>/GOST_Lib.pretty/<footprint>.kicad_mod
    """
    comps = [c for c in components if c and c.symbol and c.symbol.pins]
    os.makedirs(root, exist_ok=True)
    pretty = os.path.join(root, f"{lib_name}.pretty")
    os.makedirs(pretty, exist_ok=True)

    sym_path = os.path.join(root, f"{lib_name}.kicad_sym")
    write_kicad_sym(sym_path, comps, fp_lib=lib_name)

    mods: List[str] = []
    done = set()
    for c in comps:
        for fp in c.footprints:
            if not fp.pads:
                continue
            nm = _safe(fp.name, "FP")
            if nm in done:
                continue
            done.add(nm)
            mods.append(write_kicad_mod(os.path.join(pretty, nm + ".kicad_mod"), fp))
    return {"sym": sym_path, "pretty": pretty, "mods": mods,
            "components": len(comps), "footprints": len(mods)}
