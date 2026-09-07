"""
Импорт из KiCad: .kicad_sym (символы), .kicad_mod (посадки),
sym-lib-table / fp-lib-table (поиск установленных библиотек).

KiCad-символы читаются ТОЛЬКО ради списка выводов (номер/имя/тип/секция) --
графика потом рисуется заново по ГОСТ. Посадочные места переносятся
геометрически один-в-один.
"""
from __future__ import annotations

import os
import re
import glob
from typing import Dict, List, Optional, Tuple

from . import sexpr as S
from ..ir import (KICAD_ETYPE, Component, Footprint, FpPrim, Model3D, Pad,
                  SymPin, SymPrim, Symbol)
from .. import classify

# --------------------------------------------------------------- символы -----


def _pin_from_node(node: list, unit: int) -> SymPin:
    etype_raw = node[1] if len(node) > 1 and isinstance(node[1], str) else "passive"
    shape = node[2] if len(node) > 2 and isinstance(node[2], str) else "line"
    name_n = S.find(node, "name")
    num_n = S.find(node, "number")
    name = name_n[1] if name_n and len(name_n) > 1 else ""
    number = num_n[1] if num_n and len(num_n) > 1 else ""
    x, y, rot = S.xy(node, "at")
    # Признак hide из KiCad НЕ переносим. Там скрытый вывод питания
    # соединяется с одноимённой цепью сам, поэтому дубли IOVDD/DVDD принято
    # прятать. В Altium скрытый вывод просто не рисуется и подключиться к
    # нему нельзя -- выглядит как пропавшие выводы. По ЕСКД каждый вывод
    # питания разводится явно, так что все выводы делаем видимыми.
    p = SymPin(
        number=str(number),
        name=str(name),
        etype=KICAD_ETYPE.get(etype_raw, "passive"),
        unit=unit,
        inverted="inverted" in shape,
        clock="clock" in shape,
        hidden=False,
    )
    # сторона по углу поворота в KiCad: 0 = вывод смотрит вправо (тело слева)
    # то есть пин находится СЛЕВА от тела -> сторона L
    p.side = {0: "L", 180: "R", 90: "B", 270: "T"}.get(int(rot) % 360, "L")
    p._kx, p._ky = x, y  # type: ignore[attr-defined]
    p._krot = int(rot) % 360  # type: ignore[attr-defined]
    ln = S.val(node, "length", 2.54)
    try:
        p._klen = float(ln)  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        p._klen = 2.54  # type: ignore[attr-defined]
    return p


MM2MIL = 1.0 / 0.0254


def _mil(v) -> int:
    return int(round(float(v) * MM2MIL))


def _graphics_of(unit_node: list, unit: int) -> List[SymPrim]:
    """
    Графика символа KiCad как есть: прямоугольники, ломаные, окружности,
    дуги, подписи. Нужна для пассивок -- их обозначения в KiCad уже по
    делу, перерисовывать их по ГОСТ незачем и вредно.

    В .kicad_sym всё в миллиметрах, ось Y вверх -- как и у нас, поэтому
    пересчёт только в милы.
    """
    out: List[SymPrim] = []
    for r in S.find_all(unit_node, "rectangle"):
        sx, sy, _ = S.xy(r, "start")
        ex, ey, _ = S.xy(r, "end")
        out.append(SymPrim(kind="rect", unit=unit,
                           pts=[[_mil(sx), _mil(sy)], [_mil(ex), _mil(ey)]],
                           filled=_filled(r)))
    for pl in S.find_all(unit_node, "polyline"):
        pts = _pts_of(pl)
        if len(pts) >= 2:
            out.append(SymPrim(kind="poly", unit=unit, pts=pts,
                               filled=_filled(pl)))
    for ci in S.find_all(unit_node, "circle"):
        cx, cy, _ = S.xy(ci, "center")
        rad = S.val(ci, "radius", 0) or 0
        out.append(SymPrim(kind="ellipse", unit=unit,
                           pts=[[_mil(cx), _mil(cy)]], radius=_mil(rad),
                           filled=_filled(ci)))
    for ar in S.find_all(unit_node, "arc"):
        sx, sy, _ = S.xy(ar, "start")
        mx, my, _ = S.xy(ar, "mid")
        ex, ey, _ = S.xy(ar, "end")
        c = _arc_from_3pt((sx, sy), (mx, my), (ex, ey))
        if not c:
            continue
        cx, cy, rad, a1, a2 = c
        out.append(SymPrim(kind="arc", unit=unit,
                           pts=[[_mil(cx), _mil(cy)]], radius=_mil(rad),
                           a1=a1, a2=a2))
    for tx in S.find_all(unit_node, "text"):
        body = tx[1] if len(tx) > 1 and isinstance(tx[1], str) else ""
        if not body:
            continue
        x, y, rot = S.xy(tx, "at")
        out.append(SymPrim(kind="text", unit=unit, text=body,
                           pts=[[_mil(x), _mil(y)]], size=8,
                           justify=1, vjustify=1,
                           rotation=int(rot) % 360))
    return out


def _filled(node: list) -> bool:
    f = S.find(node, "fill")
    if not f:
        return False
    t = S.val(f, "type", "none")
    return str(t) not in ("none", "background")


def _pts_of(node: list) -> List[List[float]]:
    p = S.find(node, "pts")
    out: List[List[float]] = []
    if not p:
        return out
    for xy in S.find_all(p, "xy"):
        if len(xy) >= 3:
            try:
                out.append([_mil(xy[1]), _mil(xy[2])])
            except (TypeError, ValueError):
                continue
    return out


def _place_native_pins_from(src: List[SymPin], dst: List[SymPin]) -> None:
    """Перенести на копии выводов их места из KiCad."""
    for a, b in zip(src, dst):
        for k in ("_kx", "_ky", "_krot", "_klen"):
            if hasattr(a, k):
                setattr(b, k, getattr(a, k))
    _place_native_pins(dst)


def _place_native_pins(pins: List[SymPin]) -> None:
    """
    Поставить выводы туда, где они стоят в KiCad.

    В .kicad_sym точка вывода -- его СВОБОДНЫЙ конец, а угол смотрит в
    сторону корпуса. У нас наоборот: координата -- конец у корпуса, а
    поворот -- наружу. Отсюда пересчёт.
    """
    import math
    for p in pins:
        kx = getattr(p, "_kx", None)
        ky = getattr(p, "_ky", None)
        if kx is None or ky is None:
            continue
        a = math.radians(getattr(p, "_krot", 0))
        L = getattr(p, "_klen", 2.54) or 2.54
        p.x = _mil(kx + L * math.cos(a))
        p.y = _mil(ky + L * math.sin(a))
        p.length = _mil(L)
        p.rotation = (int(round(math.degrees(a))) + 180) % 360


def _units_of(symbol_node: list) -> List[Tuple[int, list]]:
    """Вернуть [(unit_number, node)] для дочерних (symbol "NAME_u_s" ...)."""
    out = []
    for sub in S.find_all(symbol_node, "symbol"):
        nm = sub[1] if len(sub) > 1 and isinstance(sub[1], str) else ""
        m = re.search(r"_(\d+)_(\d+)$", nm)
        unit = int(m.group(1)) if m else 1
        out.append((unit, sub))
    return out


def parse_kicad_sym(path: str) -> Dict[str, dict]:
    """
    Прочитать .kicad_sym. Возвращает {имя_символа: {...}}.
    Наследование (extends) разворачивается.
    """
    tree = S.parse_file(path)
    root = None
    for t in tree:
        if S.name_of(t) == "kicad_symbol_lib":
            root = t
            break
    if root is None:
        return {}

    raw: Dict[str, list] = {}
    for sym in S.find_all(root, "symbol"):
        nm = sym[1] if len(sym) > 1 and isinstance(sym[1], str) else ""
        if nm:
            raw[nm] = sym

    out: Dict[str, dict] = {}
    for nm, sym in raw.items():
        props = {}
        for pr in S.find_all(sym, "property"):
            if len(pr) >= 3 and isinstance(pr[1], str):
                props[pr[1]] = pr[2] if isinstance(pr[2], str) else ""
        ext = S.val(sym, "extends")
        base = raw.get(ext) if ext else None

        pins: List[SymPin] = []
        src = base if base is not None else sym
        for unit, unode in _units_of(src):
            for pn in S.find_all(unode, "pin"):
                pins.append(_pin_from_node(pn, unit))
        # некоторые библиотеки кладут пины прямо в symbol
        for pn in S.find_all(src, "pin"):
            pins.append(_pin_from_node(pn, 1))

        if base is not None:
            bprops = {}
            for pr in S.find_all(base, "property"):
                if len(pr) >= 3 and isinstance(pr[1], str):
                    bprops[pr[1]] = pr[2] if isinstance(pr[2], str) else ""
            merged = dict(bprops)
            merged.update({k: v for k, v in props.items() if v})
            props = merged

        prims: List[SymPrim] = []
        for unit, unode in _units_of(src):
            prims.extend(_graphics_of(unode, unit))
        prims.extend(_graphics_of(src, 1))

        units = sorted({p.unit for p in pins}) or [1]
        out[nm] = {
            "name": nm,
            "props": props,
            "pins": pins,
            "prims": prims,
            "part_count": max(units),
            "extends": ext or "",
        }
    return out


def component_from_kicad_sym(path: str, sym_name: str) -> Component:
    data = parse_kicad_sym(path)
    if sym_name not in data:
        raise KeyError(f"Символ '{sym_name}' не найден в {os.path.basename(path)}")
    d = data[sym_name]
    props = d["props"]
    pins: List[SymPin] = d["pins"]

    c = Component()
    c.name = sym_name
    c.description = props.get("Description", "") or props.get("ki_description", "")
    c.value = props.get("Value", "")
    c.mpn = props.get("MPN", "") or props.get("Manufacturer_Part_Number", "")
    c.manufacturer = props.get("Manufacturer", "") or props.get("Mfr", "")
    c.datasheet = props.get("Datasheet", "")
    c.source = "kicad"
    c.source_ref = f"{os.path.basename(path)}::{sym_name}"
    kw = props.get("ki_keywords", "")
    fp_hint = props.get("Footprint", "")
    ref = (props.get("Reference", "") or "").strip()
    c.ctype = classify.guess_type(sym_name, c.description, kw, pins, fp_hint,
                                  ref, fp_hint.split(":")[-1])
    c.designator = ref or classify.CTYPE_PREFIX.get(c.ctype, "U")
    if not c.designator.endswith("?"):
        c.designator += "?"
    c.raw_pins = pins
    c.symbol = Symbol(part_count=d["part_count"], pins=list(pins))
    # Родное обозначение KiCad кладём отдельно: для пассивок оно и есть
    # рабочее (перерисовывать резистор по ГОСТ незачем -- он там уже
    # прямоугольник), а для микросхем пригодится как запасной вариант,
    # если автоопределение типа ошиблось.
    c.native_prims = list(d.get("prims") or [])
    c.native_pins = [SymPin(**{k: v for k, v in p.__dict__.items()
                              if k in SymPin.__dataclass_fields__})
                     for p in pins]
    _place_native_pins_from(pins, c.native_pins)
    c.symbol_source = ("native" if classify.keeps_native_symbol(c.ctype)
                       else "gost")
    if fp_hint:
        c.params["KiCadFootprint"] = fp_hint
    c.params.setdefault("PinCount", str(len(pins)))

    # Всё, что библиотека положила в свойства символа, забираем в параметры:
    # у KiCad там встречаются и парт-номер, и поставщик, и рабочие режимы.
    # Раньше брались только четыре известных поля, остальное терялось.
    for key, val in props.items():
        if not val or not str(val).strip():
            continue
        if key.startswith("ki_") or key in ("Reference", "Footprint"):
            continue
        c.params.setdefault(_PARAM_ALIASES.get(key, key), str(val).strip())
    if not c.mpn:
        for k in ("MPN", "Manufacturer_Part_Number", "PartNumber",
                  "Part Number", "Mfr. No", "MFN", "OrderCode", "Comment"):
            if c.params.get(k):
                c.mpn = c.params[k]
                break
    if not c.manufacturer:
        for k in ("Manufacturer", "Mfr", "Vendor", "Supplier"):
            if c.params.get(k):
                c.manufacturer = c.params[k]
                break
    if kw:
        c.tags = [t for t in kw.replace(",", " ").split() if t][:12]
    return c


# как называются одни и те же поля в разных библиотеках KiCad
_PARAM_ALIASES = {
    "Manufacturer_Part_Number": "MPN",
    "Mfr. No": "MPN",
    "PartNumber": "MPN",
    "Part Number": "MPN",
    "Mfr": "Manufacturer",
    "Vendor": "Manufacturer",
    "LCSC Part": "LCSC",
    "LCSC Part #": "LCSC",
    "Voltage": "Напряжение",
    "Tolerance": "Допуск",
    "Power": "Мощность",
    "Package": "Корпус",
}


# ------------------------------------------------------------ посадки --------

_LAYER_MAP = {
    "F.SilkS": "silk", "B.SilkS": "silk_bot",
    "F.Fab": "assy", "B.Fab": "assy",
    "F.CrtYd": "courtyard", "B.CrtYd": "courtyard",
    "F.Cu": "copper_top", "B.Cu": "copper_bot",
    "F.Mask": "mask", "B.Mask": "mask",
    "F.Paste": "paste", "B.Paste": "paste",
    "Edge.Cuts": "mech", "User.Drawings": "mech", "User.Comments": "mech",
    "Dwgs.User": "mech", "Cmts.User": "mech", "Eco1.User": "mech", "Eco2.User": "mech",
}


def _layer(node: list) -> str:
    ly = S.val(node, "layer", "F.SilkS")
    if isinstance(ly, str):
        return _LAYER_MAP.get(ly, "mech")
    return "mech"


def _stroke_width(node: list, default=0.15) -> float:
    st = S.find(node, "stroke")
    if st:
        return S.fnum(S.val(st, "width", default), default)
    return S.fnum(S.val(node, "width", default), default)


_PAD_SHAPE = {"rect": "rect", "circle": "round", "oval": "oval",
              "roundrect": "roundrect", "trapezoid": "rect", "custom": "rect"}


def _drop_thermal_vias(fp: Footprint) -> int:
    """
    Убрать переходные отверстия из посадочного места.

    В KiCad под тепловой площадкой QFN/DFN стоит решётка via: те же номер и
    слой, что у площадки, отверстие во всю ширину. В Altium они превращаются
    в кучу налезающих друг на друга площадок с одним номером -- мешают и в
    редакторе, и при проверках. Настоящие выводные контакты не трогаем:
    у них есть ободок вокруг отверстия.
    """
    keep = []
    dropped = 0
    for p in fp.pads:
        if p.hole <= 0:
            keep.append(p)
            continue
        # Ободок вокруг отверстия. У настоящего выводного контакта он
        # заметный (обычно 0.25 мм и больше на сторону), у переходного
        # отверстия его почти нет -- отверстие во всю площадку.
        if not p.plated:
            keep.append(p)      # крепёжное отверстие -- не переходное
            continue
        ring = (min(p.w, p.h) - p.hole) / 2.0
        if ring < 0.15:
            dropped += 1
            continue
        keep.append(p)
    if dropped:
        fp.pads = keep
    return dropped


def parse_kicad_mod(path: str, drop_vias: bool = True) -> Footprint:
    tree = S.parse_file(path)
    root = None
    for t in tree:
        if S.name_of(t) in ("footprint", "module"):
            root = t
            break
    if root is None:
        raise ValueError(f"{path}: не похоже на .kicad_mod")

    name = root[1] if len(root) > 1 and isinstance(root[1], str) else \
        os.path.splitext(os.path.basename(path))[0]
    fp = Footprint(name=os.path.basename(str(name)))
    fp.description = S.val(root, "descr", "") or ""

    for pd in S.find_all(root, "pad"):
        num = str(pd[1]) if len(pd) > 1 else ""
        ptype = str(pd[2]) if len(pd) > 2 else "smd"
        pshape = str(pd[3]) if len(pd) > 3 else "rect"
        x, y, rot = S.xy(pd, "at")
        size = S.find(pd, "size")
        w = S.fnum(size[1]) if size and len(size) > 1 else 1.0
        h = S.fnum(size[2]) if size and len(size) > 2 else w
        layers = S.find(pd, "layers") or []
        lays = [str(v) for v in layers[1:]]
        has_cu = any(l.endswith(".Cu") or l == "*.Cu" for l in lays)
        if ptype in ("thru_hole", "np_thru_hole"):
            layer = "multi"
        elif not has_cu:
            # только паста или маска -- в Altium это делается правилами,
            # отдельной площадкой такое переносить нельзя (получится медь)
            continue
        elif any(l.startswith("B.") for l in lays) and not any(l.startswith("F.") for l in lays):
            layer = "bottom"
        else:
            layer = "top"
        drill = S.find(pd, "drill")
        hole = 0.0
        hole_len = 0.0
        hole_rot = 0.0
        if drill:
            if len(drill) > 1 and str(drill[1]) == "oval":
                # (drill oval W H) -- W по X, H по Y в собственной системе
                # площадки. У Altium паз задаётся шириной, длиной и УГЛОМ:
                # без угла вертикальный паз ложился поперёк, а
                # горизонтальный вообще терялся, потому что «длина» не
                # получалась больше «ширины».
                dw = S.fnum(drill[2]) if len(drill) > 2 else 0.0
                dh = S.fnum(drill[3]) if len(drill) > 3 else dw
                hole = min(dw, dh)
                hole_len = max(dw, dh)
                hole_rot = 90.0 if dh > dw else 0.0
            else:
                hole = S.fnum(drill[1]) if len(drill) > 1 else 0.0
        rr = S.fnum(S.val(pd, "roundrect_rratio", 0.0), 0.0) * 100.0
        fp.pads.append(Pad(
            number=num, x=x, y=-y, w=w, h=h,
            shape=_PAD_SHAPE.get(pshape, "rect"),
            rot=-rot, layer=layer, hole=hole, hole_len=hole_len,
            hole_rot=(hole_rot - rot) % 180.0 if hole_len > hole else 0.0,
            plated=(ptype != "np_thru_hole"),
            corner_radius=rr,
        ))

    if drop_vias:
        _drop_thermal_vias(fp)

    for ln in S.find_all(root, "fp_line"):
        sx, sy, _ = S.xy(ln, "start")
        ex, ey, _ = S.xy(ln, "end")
        fp.prims.append(FpPrim(kind="line", layer=_layer(ln),
                               pts=[[sx, -sy], [ex, -ey]], width=_stroke_width(ln)))

    for rc in S.find_all(root, "fp_rect"):
        sx, sy, _ = S.xy(rc, "start")
        ex, ey, _ = S.xy(rc, "end")
        fp.prims.append(FpPrim(kind="rect", layer=_layer(rc),
                               pts=[[sx, -sy], [ex, -ey]], width=_stroke_width(rc)))

    for ci in S.find_all(root, "fp_circle"):
        cx, cy, _ = S.xy(ci, "center")
        ex, ey, _ = S.xy(ci, "end")
        r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
        fp.prims.append(FpPrim(kind="circle", layer=_layer(ci),
                               pts=[[cx, -cy]], radius=r, width=_stroke_width(ci)))

    for ar in S.find_all(root, "fp_arc"):
        sx, sy, _ = S.xy(ar, "start")
        mx, my, _ = S.xy(ar, "mid")
        ex, ey, _ = S.xy(ar, "end")
        c = _arc_from_3pt((sx, -sy), (mx, -my), (ex, -ey))
        if c:
            cx, cy, r, a1, a2 = c
            fp.prims.append(FpPrim(kind="arc", layer=_layer(ar), pts=[[cx, cy]],
                                   radius=r, a1=a1, a2=a2, width=_stroke_width(ar)))

    for pg in S.find_all(root, "fp_poly"):
        ptsn = S.find(pg, "pts") or []
        pts = []
        for p in S.find_all(ptsn, "xy"):
            pts.append([S.fnum(p[1]), -S.fnum(p[2])])
        if len(pts) >= 3:
            fp.prims.append(FpPrim(kind="poly", layer=_layer(pg), pts=pts,
                                   width=_stroke_width(pg, 0.0), filled=True))

    md = S.find(root, "model")
    if md and len(md) > 1 and isinstance(md[1], str):
        off = S.find(md, "offset")
        rot = S.find(md, "rotate")
        oxyz = S.find(off, "xyz") if off else None
        rxyz = S.find(rot, "xyz") if rot else None
        fp.model = Model3D(
            path=md[1],
            dx=S.fnum(oxyz[1]) if oxyz and len(oxyz) > 1 else 0.0,
            dy=S.fnum(oxyz[2]) if oxyz and len(oxyz) > 2 else 0.0,
            dz=S.fnum(oxyz[3]) if oxyz and len(oxyz) > 3 else 0.0,
            rx=S.fnum(rxyz[1]) if rxyz and len(rxyz) > 1 else 0.0,
            ry=S.fnum(rxyz[2]) if rxyz and len(rxyz) > 2 else 0.0,
            rz=S.fnum(rxyz[3]) if rxyz and len(rxyz) > 3 else 0.0,
        )
    return fp


def _arc_from_3pt(p1, p2, p3):
    (x1, y1), (x2, y2), (x3, y3) = p1, p2, p3
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return None
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) +
          (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) +
          (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    import math
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.degrees(math.atan2(y1 - uy, x1 - ux)) % 360
    a3 = math.degrees(math.atan2(y3 - uy, x3 - ux)) % 360
    a2m = math.degrees(math.atan2(y2 - uy, x2 - ux)) % 360
    # определить направление: середина должна лежать внутри дуги a1->a3 против часовой
    def inside(a, s, e):
        span = (e - s) % 360
        return ((a - s) % 360) <= span
    if inside(a2m, a1, a3):
        return (ux, uy, r, a1, a3)
    return (ux, uy, r, a3, a1)


# ------------------------------------------- поиск установленных библиотек ----

# Ограничение области поиска. Когда на машине стоят три версии KiCad,
# каждый символ находится трижды. Здесь можно оставить одну: либо папку
# установки (…/share/kicad), либо саму версию ("10", "9.0").
ONLY_ROOT = ""


def set_only_root(path_or_version: str) -> None:
    """Искать только в этой установке KiCad. Пустая строка -- во всех."""
    global ONLY_ROOT
    ONLY_ROOT = (path_or_version or "").strip()


def _root_matches(path: str) -> bool:
    if not ONLY_ROOT:
        return True
    want = ONLY_ROOT.replace("/", "\\").rstrip("\\").lower()
    have = os.path.normpath(path).replace("/", "\\").lower()
    if os.sep != "\\":
        want = ONLY_ROOT.rstrip("/").lower()
        have = os.path.normpath(path).lower()
    if os.path.isabs(ONLY_ROOT):
        return have.startswith(want)
    # версия: ищем её отдельным элементом пути ("...\\KiCad\\10\\share\\...")
    parts = [x for x in re.split(r"[\\/]+", os.path.normpath(path)) if x]
    return any(x.lower() == want for x in parts)


def kicad_config_dirs() -> List[str]:
    """Папки настроек KiCad (в них лежат sym-lib-table / fp-lib-table)."""
    dirs = []
    if os.name == "nt":
        base = os.environ.get("APPDATA", "")
        if base:
            dirs += glob.glob(os.path.join(base, "kicad", "*"))
    else:
        home = os.path.expanduser("~")
        dirs += glob.glob(os.path.join(home, ".config", "kicad", "*"))
        dirs += glob.glob(os.path.join(home, "Library", "Preferences",
                                       "kicad", "*"))
    return sorted(d for d in dirs
                  if os.path.isdir(d) and _root_matches(d))


def install_roots() -> List[str]:
    """
    Папки share/kicad установленного KiCad -- там лежат symbols/, footprints/,
    3dmodels/. Ищем по стандартным местам всех версий, что стоят на машине.
    """
    roots: List[str] = []
    pats: List[str] = []
    if os.name == "nt":
        bases = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 r"C:\Program Files", r"D:\Program Files"]
        for b in bases:
            if not b:
                continue
            pats.append(os.path.join(b, "KiCad", "*", "share", "kicad"))
            pats.append(os.path.join(b, "KiCad", "share", "kicad"))
    else:
        pats += ["/usr/share/kicad", "/usr/local/share/kicad",
                 "/Applications/KiCad/KiCad.app/Contents/SharedSupport",
                 os.path.expanduser("~/.local/share/kicad")]
    # то, на что указывают переменные окружения KICAD*_SYMBOL_DIR
    for k, v in os.environ.items():
        if re.match(r"^KICAD\d*_(SYMBOL|FOOTPRINT|3DMODEL)_DIR$", k) and v:
            up = os.path.dirname(os.path.normpath(v))
            if os.path.isdir(up):
                roots.append(up)
    for pat in pats:
        for d in glob.glob(pat):
            if os.path.isdir(d):
                roots.append(os.path.normpath(d))
    out, seen = [], set()
    for r in roots:
        key = r.lower()
        if key not in seen and _root_matches(r):
            seen.add(key)
            out.append(r)
    return out


def installed_versions() -> List[Tuple[str, str]]:
    """
    [(подпись, путь)] для каждой найденной установки KiCad -- чтобы в
    настройках можно было выбрать нужную, а не искать во всех сразу.
    """
    global ONLY_ROOT
    keep, ONLY_ROOT = ONLY_ROOT, ""       # список строим по всем установкам
    try:
        roots = install_roots()
    finally:
        ONLY_ROOT = keep
    out = []
    for r in roots:
        parts = [x for x in re.split(r"[\\/]+", os.path.normpath(r)) if x]
        ver = ""
        for x in parts:
            if re.fullmatch(r"\d+(\.\d+)*", x):
                ver = x
        out.append((f"KiCad {ver}" if ver else os.path.basename(r), r))
    return out


def _expand(uri: str, subst: Dict[str, str]) -> str:
    def rep(m):
        k = m.group(1)
        return subst.get(k, os.environ.get(k, m.group(0)))
    p = re.sub(r"\$\{([^}]+)\}", rep, uri)
    return os.path.normpath(p)


def _kicad_env(cfgdir: str = "") -> Dict[str, str]:
    """
    Подстановки путей KiCad: из kicad_common.json плюс все найденные
    установки (KICAD6..KICAD12_SYMBOL_DIR и т.п.).
    """
    subst: Dict[str, str] = {}
    if cfgdir:
        common = os.path.join(cfgdir, "kicad_common.json")
        if os.path.isfile(common):
            try:
                import json
                with open(common, encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in (data.get("environment", {}).get("vars") or {}).items():
                    if v:
                        subst[k] = v
            except Exception:
                pass
    for root in install_roots():
        m = re.search(r"KiCad[\\/](\d+)", root, re.I)
        vers = [m.group(1)] if m else []
        vers += [str(v) for v in range(5, 15)]
        for v in vers:
            for name, sub in (("SYMBOL_DIR", "symbols"),
                              ("FOOTPRINT_DIR", "footprints"),
                              ("3DMODEL_DIR", "3dmodels"),
                              ("TEMPLATE_DIR", "template")):
                d = os.path.join(root, sub)
                if os.path.isdir(d):
                    subst.setdefault(f"KICAD{v}_{name}", d)
    return subst


def list_libraries() -> Dict[str, List[Tuple[str, str]]]:
    """
    Все библиотеки KiCad: и прописанные в таблицах пользователя, и просто
    лежащие в папках установки (их KiCad подключает сам, а таблица может
    быть неполной или ссылаться на неразвёрнутые переменные).
    Возвращает {'symbols': [(nickname, путь)], 'footprints': [(nickname, путь)]}
    """
    res: Dict[str, List[Tuple[str, str]]] = {"symbols": [], "footprints": []}
    subst = _kicad_env()

    for cfg in kicad_config_dirs():
        s2 = dict(subst)
        s2.update(_kicad_env(cfg))
        for table, kind in (("sym-lib-table", "symbols"),
                            ("fp-lib-table", "footprints")):
            tpath = os.path.join(cfg, table)
            if not os.path.isfile(tpath):
                continue
            try:
                tree = S.parse_file(tpath)
            except Exception:
                continue
            for t in tree:
                if S.name_of(t) not in ("sym_lib_table", "fp_lib_table"):
                    continue
                for lib in S.find_all(t, "lib"):
                    nick = S.val(lib, "name", "")
                    uri = S.val(lib, "uri", "")
                    if not nick or not uri:
                        continue
                    p = _expand(str(uri), s2)
                    if "${" in p:
                        continue          # переменную развернуть не смогли
                    res[kind].append((str(nick), p))

    # прямой обход папок установки
    for root in install_roots():
        sd = os.path.join(root, "symbols")
        if os.path.isdir(sd):
            for f in glob.glob(os.path.join(sd, "*.kicad_sym")):
                res["symbols"].append(
                    (os.path.splitext(os.path.basename(f))[0], f))
        fd = os.path.join(root, "footprints")
        if os.path.isdir(fd):
            for d in glob.glob(os.path.join(fd, "*.pretty")):
                res["footprints"].append(
                    (os.path.basename(d)[:-len(".pretty")], d))

    for k in res:
        seen = set()
        uniq = []
        for nick, p in res[k]:
            key = os.path.normcase(os.path.normpath(p))
            if key in seen:
                continue
            seen.add(key)
            uniq.append((nick, p))
        res[k] = sorted(uniq, key=lambda t: t[0].lower())
    return res


# ------------------------------------------------------- быстрый индекс ------

_SYM_NAME_RE = re.compile(r'^[ \t]*\(symbol[ \t]+"([^"]+)"', re.M)
_UNIT_SUFFIX_RE = re.compile(r"_\d+_\d+$")


def symbol_names(path: str) -> List[str]:
    """
    Имена символов в .kicad_sym БЕЗ полного разбора файла.
    Полный разбор Device.kicad_sym занимает секунды, а таких файлов сотня --
    поэтому для индекса берём только имена регулярным выражением.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return []
    out, seen = [], set()
    for m in _SYM_NAME_RE.finditer(text):
        nm = m.group(1)
        if _UNIT_SUFFIX_RE.search(nm):
            continue                       # это секция символа, а не символ
        if nm in seen:
            continue
        seen.add(nm)
        out.append(nm)
    return out


def symbol_footprint(path: str, name: str) -> str:
    """
    Достать свойство Footprint конкретного символа регулярным выражением --
    без полного разбора файла (Device.kicad_sym весит мегабайты).
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return ""
    m = re.search(r'\(symbol\s+"' + re.escape(name) + r'"', text)
    if not m:
        return ""
    chunk = text[m.end():m.end() + 4000]
    nxt = _SYM_NAME_RE.search(chunk)
    if nxt and _UNIT_SUFFIX_RE.search(nxt.group(1)) is None:
        chunk = chunk[:nxt.start()]
    fm = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', chunk)
    return fm.group(1).strip() if fm else ""


def footprint_names(path: str) -> List[str]:
    if not os.path.isdir(path):
        return []
    try:
        return sorted(os.path.splitext(f)[0] for f in os.listdir(path)
                      if f.lower().endswith(".kicad_mod"))
    except Exception:
        return []


def scan_all(progress=None) -> List[Tuple[str, str, str, str]]:
    """
    Полный индекс: [(kind, nickname, path, name)], kind = 'sym' | 'fp'.
    progress(done, total, текст) -- необязательный обратный вызов.
    """
    libs = list_libraries()
    total = len(libs["symbols"]) + len(libs["footprints"])
    out: List[Tuple[str, str, str, str]] = []
    done = 0
    for nick, path in libs["symbols"]:
        done += 1
        if progress:
            progress(done, total, f"символы: {nick}")
        for nm in symbol_names(path):
            out.append(("sym", nick, path, nm))
    for nick, path in libs["footprints"]:
        done += 1
        if progress:
            progress(done, total, f"посадки: {nick}")
        for nm in footprint_names(path):
            out.append(("fp", nick, path, nm))
    return out


# ------------------------------------------------- поиск без индекса ---------

def find_symbol(nickname_or_name: str, libs: Optional[dict] = None
                ) -> List[Tuple[str, str, str]]:
    """
    Медленный поиск напрямую по файлам (без каталога-индекса).
    Возвращает [(nickname, путь_к_kicad_sym, имя_символа)].
    """
    libs = libs or list_libraries()
    want_lib, _, want_sym = nickname_or_name.rpartition(":")
    want_sym = (want_sym or nickname_or_name).lower()
    out = []
    for nick, path in libs["symbols"]:
        if want_lib and nick.lower() != want_lib.lower():
            continue
        if not os.path.isfile(path):
            continue
        for nm in symbol_names(path):
            low = nm.lower()
            if low == want_sym or (not want_lib and want_sym in low):
                out.append((nick, path, nm))
    out.sort(key=lambda t: (t[2].lower() != want_sym, t[0].lower(), t[2].lower()))
    return out


def find_footprint(ref: str, libs: Optional[dict] = None) -> List[Tuple[str, str]]:
    """Поиск посадки. 'Package_QFP:LQFP-64...' или часть имени."""
    libs = libs or list_libraries()
    want_lib, _, want_fp = ref.rpartition(":")
    want_fp = (want_fp or ref).lower()
    out = []
    for nick, path in libs["footprints"]:
        if want_lib and nick.lower() != want_lib.lower():
            continue
        for nm in footprint_names(path):
            low = nm.lower()
            if low == want_fp or (not want_lib and want_fp in low):
                out.append((nick, os.path.join(path, nm + ".kicad_mod")))
    out.sort(key=lambda t: (os.path.basename(t[1]).lower() != want_fp + ".kicad_mod",
                            t[0].lower(), t[1].lower()))
    return out


def resolve_3d(model_path: str, subst: Optional[Dict[str, str]] = None) -> str:
    """Развернуть ${KICAD*_3DMODEL_DIR}/... и найти .step рядом с .wrl."""
    if not model_path:
        return ""
    all_subst = _kicad_env()
    for cfg in kicad_config_dirs():
        all_subst.update(_kicad_env(cfg))
    if subst:
        all_subst.update(subst)
    p = _expand(model_path, all_subst)
    if os.path.isfile(p) and p.lower().endswith((".step", ".stp")):
        return p
    stem = os.path.splitext(p)[0]
    for ext in (".step", ".stp", ".STEP", ".STP"):
        if os.path.isfile(stem + ext):
            return stem + ext
    return p if os.path.isfile(p) else ""
