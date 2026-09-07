"""
Импорт из EasyEDA / LCSC (JLCPCB).

По коду вида C2040 тянется компонент из открытого API EasyEDA Standard:
символ (нужен только список выводов -- графика рисуется заново по ГОСТ),
посадочное место (переносится геометрически) и, по возможности, 3D-модель.

EasyEDA отдаёт 3D в формате OBJ; Altium понимает STEP, поэтому OBJ
конвертируется модулем mesh2step.
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from .. import classify
from ..ir import Component, Footprint, FpPrim, Model3D, Pad, SymPin, Symbol

API_COMPONENT = ("https://easyeda.com/api/products/{code}/components"
                 "?version=6.4.19.5")
API_3D_OBJ = "https://modules.easyeda.com/3dmodel/{uuid}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; GostLib/1.0)",
      "Accept-Encoding": "gzip, deflate",
      "Accept": "application/json, text/plain, */*"}

UNIT_MM = 0.254        # 1 единица EasyEDA = 10 mil
UNIT_MIL = 10.0

EE_ETYPE = {0: "passive", 1: "input", 2: "output", 3: "io", 4: "power"}

EE_LAYER = {
    1: "copper_top", 2: "copper_bot", 3: "silk", 4: "silk_bot",
    5: "paste", 6: "paste", 7: "mask", 8: "mask",
    10: "mech", 11: "multi", 12: "mech", 13: "assy", 14: "assy",
    19: "mech", 21: "mech", 100: "mech",
}


class EasyEdaError(RuntimeError):
    pass


def _get(url: str, timeout: float = 25.0) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as e:
        raise EasyEdaError(f"HTTP {e.code} при запросе {url}") from e
    except Exception as e:
        raise EasyEdaError(f"Нет ответа от EasyEDA: {e}") from e


def fetch_raw(code: str) -> dict:
    code = (code or "").strip().upper()
    if not re.fullmatch(r"C\d+", code):
        raise EasyEdaError("Код LCSC должен выглядеть как C2040")
    data = json.loads(_get(API_COMPONENT.format(code=code)).decode("utf-8"))
    if not data.get("success") or not data.get("result"):
        raise EasyEdaError(f"{code}: компонент не найден в EasyEDA")
    return data["result"]


# ----------------------------------------------------------------- символ ----

def _parse_pins(shapes: List[str]) -> List[SymPin]:
    pins: List[SymPin] = []
    for sh in shapes:
        if not sh.startswith("P~"):
            continue
        segs = sh.split("^^")
        head = segs[0].split("~")
        etype = EE_ETYPE.get(int(_f(head[2], 0)), "passive")
        name, number = "", ""
        if len(segs) > 3:
            f = segs[3].split("~")
            if len(f) > 4:
                name = f[4]
        if len(segs) > 4:
            f = segs[4].split("~")
            if len(f) > 4:
                number = f[4]
        inverted = False
        if len(segs) > 5:
            f = segs[5].split("~")
            inverted = bool(f and f[0] not in ("", "0"))
        clock = False
        if len(segs) > 6:
            f = segs[6].split("~")
            clock = bool(f and f[0] not in ("", "0"))
        pins.append(SymPin(number=str(number).strip(), name=str(name).strip(),
                           etype=etype, inverted=inverted, clock=clock))
    return pins


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------- посадка -----

def _svg_path_points(path: str) -> List[List[float]]:
    toks = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?|[MmLlZzHhVvAaCc]", path or "")
    pts: List[List[float]] = []
    i = 0
    cur = [0.0, 0.0]
    cmd = "M"
    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd = t
            i += 1
            continue
        if cmd in ("M", "L"):
            cur = [float(toks[i]), float(toks[i + 1])]
            pts.append(list(cur))
            i += 2
        elif cmd in ("m", "l"):
            cur = [cur[0] + float(toks[i]), cur[1] + float(toks[i + 1])]
            pts.append(list(cur))
            i += 2
        elif cmd in ("H", "h"):
            x = float(toks[i])
            cur = [x if cmd == "H" else cur[0] + x, cur[1]]
            pts.append(list(cur))
            i += 1
        elif cmd in ("V", "v"):
            y = float(toks[i])
            cur = [cur[0], y if cmd == "V" else cur[1] + y]
            pts.append(list(cur))
            i += 1
        elif cmd in ("A", "a"):
            # rx ry rot large sweep x y
            if i + 6 < len(toks):
                cur = [float(toks[i + 5]), float(toks[i + 6])]
                pts.append(list(cur))
            i += 7
        else:
            i += 1
    return pts


def _arc_from_path(path: str) -> Optional[Tuple[float, float, float, float, float]]:
    """Дуга EasyEDA задаётся SVG-путём 'M x y A rx ry rot large sweep x2 y2'."""
    m = re.search(r"M\s*([-\d.]+)[ ,]+([-\d.]+).*?A\s*([-\d.]+)[ ,]+([-\d.]+)[ ,]+"
                  r"([-\d.]+)[ ,]+([01])[ ,]+([01])[ ,]+([-\d.]+)[ ,]+([-\d.]+)",
                  path or "", re.S)
    if not m:
        return None
    x1, y1, rx, ry, _rot, large, sweep, x2, y2 = [float(v) for v in m.groups()]
    r = max(rx, ry) or 1e-6
    dx, dy = x2 - x1, y2 - y1
    d = math.hypot(dx, dy)
    if d == 0 or d > 2 * r:
        r = max(r, d / 2 + 1e-9)
    h = math.sqrt(max(0.0, r * r - (d / 2) ** 2))
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    ux, uy = -dy / d, dx / d
    sign = 1 if (large != sweep) else -1
    cx, cy = mx + sign * h * ux, my + sign * h * uy
    a1 = math.degrees(math.atan2(y1 - cy, x1 - cx))
    a2 = math.degrees(math.atan2(y2 - cy, x2 - cx))
    return (cx, cy, r, a1, a2)


_PAD_SHAPE = {"ELLIPSE": "round", "RECT": "rect", "OVAL": "oval",
              "POLYGON": "rect"}


def _parse_footprint(pkg: dict) -> Tuple[Footprint, str]:
    ds = pkg.get("dataStr") or {}
    head = ds.get("head") or {}
    ox = _f(head.get("x"), 0.0)
    oy = _f(head.get("y"), 0.0)
    name = (pkg.get("title") or head.get("c_para", {}).get("package", "")
            or "FOOTPRINT").strip()
    fp = Footprint(name=re.sub(r"[^\w.\-+]", "_", name))
    model_uuid = ""

    def MX(v):
        return (_f(v) - ox) * UNIT_MM

    def MY(v):
        return -(_f(v) - oy) * UNIT_MM

    def MD(v):
        return _f(v) * UNIT_MM

    for sh in ds.get("shape") or []:
        parts = sh.split("~")
        kind = parts[0]
        if kind == "PAD" and len(parts) >= 12:
            shp = _PAD_SHAPE.get(parts[1].upper(), "rect")
            x, y = MX(parts[2]), MY(parts[3])
            w, h = MD(parts[4]), MD(parts[5])
            layer = int(_f(parts[6], 1))
            num = parts[8]
            hole_r = MD(parts[9])
            rot = -_f(parts[11]) if len(parts) > 11 else 0.0
            hole_len = MD(parts[13]) if len(parts) > 13 else 0.0
            plated = True
            if len(parts) > 15:
                plated = str(parts[15]).lower() not in ("y", "1", "true")
            lay = {1: "top", 2: "bottom", 11: "multi"}.get(layer, "top")
            fp.pads.append(Pad(number=str(num), x=x, y=y, w=w, h=h, shape=shp,
                               rot=rot, layer=lay, hole=hole_r * 2,
                               hole_len=hole_len, plated=plated))
        elif kind == "TRACK" and len(parts) >= 5:
            width = MD(parts[1])
            lay = EE_LAYER.get(int(_f(parts[2], 3)), "mech")
            coords = [_f(v) for v in parts[4].split(" ") if v.strip()]
            pts = [[(coords[i] - ox) * UNIT_MM, -(coords[i + 1] - oy) * UNIT_MM]
                   for i in range(0, len(coords) - 1, 2)]
            for i in range(len(pts) - 1):
                fp.prims.append(FpPrim(kind="line", layer=lay,
                                       pts=[pts[i], pts[i + 1]], width=width))
        elif kind == "CIRCLE" and len(parts) >= 6:
            fp.prims.append(FpPrim(
                kind="circle", layer=EE_LAYER.get(int(_f(parts[5], 3)), "mech"),
                pts=[[MX(parts[1]), MY(parts[2])]], radius=MD(parts[3]),
                width=MD(parts[4])))
        elif kind == "ARC" and len(parts) >= 5:
            a = _arc_from_path(parts[4])
            if a:
                cx, cy, r, a1, a2 = a
                fp.prims.append(FpPrim(
                    kind="arc", layer=EE_LAYER.get(int(_f(parts[2], 3)), "mech"),
                    pts=[[(cx - ox) * UNIT_MM, -(cy - oy) * UNIT_MM]],
                    radius=r * UNIT_MM, a1=-a2, a2=-a1, width=MD(parts[1])))
        elif kind == "SOLIDREGION" and len(parts) >= 4:
            pts = [[(p[0] - ox) * UNIT_MM, -(p[1] - oy) * UNIT_MM]
                   for p in _svg_path_points(parts[3])]
            if len(pts) >= 3:
                fp.prims.append(FpPrim(
                    kind="poly", layer=EE_LAYER.get(int(_f(parts[1], 3)), "mech"),
                    pts=pts, filled=True, width=0.0))
        elif kind == "HOLE" and len(parts) >= 4:
            fp.pads.append(Pad(number="", x=MX(parts[1]), y=MY(parts[2]),
                               w=MD(parts[3]) * 2, h=MD(parts[3]) * 2,
                               shape="round", layer="multi",
                               hole=MD(parts[3]) * 2, plated=False))
        elif kind == "TEXT" and len(parts) >= 11:
            lay = EE_LAYER.get(int(_f(parts[7], 3)), "mech")
            fp.prims.append(FpPrim(kind="text", layer=lay,
                                   pts=[[MX(parts[2]), MY(parts[3])]],
                                   text=parts[10], height=MD(parts[9]),
                                   rot=-_f(parts[5]), width=MD(parts[4])))
        elif kind == "SVGNODE":
            try:
                node = json.loads(sh[len("SVGNODE~"):])
                model_uuid = (node.get("attrs") or {}).get("uuid", "")
                z = _f((node.get("attrs") or {}).get("z"), 0.0)
                if z:
                    fp.height = z * UNIT_MM
            except Exception:
                pass
    return fp, model_uuid


# ------------------------------------------------------------------ сборка ---

def fetch(code: str, out_dir: str = "", want_3d: bool = True,
          log=None) -> Component:
    log = log or (lambda *_: None)
    res = fetch_raw(code)
    ds = res.get("dataStr") or {}
    head = ds.get("head") or {}
    para = head.get("c_para") or {}

    c = Component()
    c.name = (para.get("name") or res.get("title") or code).strip()
    c.mpn = (para.get("Manufacturer Part") or para.get("name") or "").strip()
    c.manufacturer = (para.get("Manufacturer") or "").strip()
    c.value = (para.get("Value") or "").strip()
    c.description = (res.get("description") or "").strip()
    c.datasheet = (res.get("dataStr", {}).get("head", {})
                   .get("c_para", {}).get("link", "")) or \
        (res.get("lcsc") or {}).get("url", "")
    c.designator = (para.get("pre") or "U?").replace("?", "") + "?"
    c.source = "easyeda"
    c.source_ref = code
    c.params["LCSC"] = code
    if para.get("package"):
        c.params["Package"] = para["package"]

    pins = _parse_pins(ds.get("shape") or [])
    c.raw_pins = pins
    c.symbol = Symbol(pins=list(pins))
    c.params["PinCount"] = str(len(pins))

    pkg = res.get("packageDetail") or {}
    model_uuid = ""
    if pkg:
        fp, model_uuid = _parse_footprint(pkg)
        if not fp.name or fp.name == "FOOTPRINT":
            fp.name = c.params.get("Package", c.name)
        c.footprints.append(fp)

    pkg_name = c.params.get("Package", "") or (
        c.footprints[0].name if c.footprints else "")
    if pkg_name:
        c.params["Package"] = pkg_name
    ee_pre = (para.get("pre") or "").strip()
    c.ctype = classify.guess_type(c.name, c.description, "", pins,
                                  pkg_name, ee_pre, pkg_name)
    c.designator = classify.CTYPE_PREFIX.get(c.ctype, "U") + "?"

    if want_3d and model_uuid and out_dir:
        try:
            path = download_3d(model_uuid, out_dir, c.footprints[0].name
                               if c.footprints else c.name, log=log)
            if path and c.footprints:
                c.footprints[0].model = Model3D(path=path)
        except Exception as e:      # 3D не критично
            log(f"3D-модель не получена: {e}")
    return c


def download_3d(uuid: str, out_dir: str, base: str, log=None) -> str:
    """Скачать OBJ и, если получится, конвертировать в STEP для Altium."""
    log = log or (lambda *_: None)
    os.makedirs(out_dir, exist_ok=True)
    raw = _get(API_3D_OBJ.format(uuid=uuid)).decode("utf-8", "replace")
    if "v " not in raw:
        raise EasyEdaError("EasyEDA вернул не OBJ")
    obj_path = os.path.join(out_dir, f"{base}.obj")
    with open(obj_path, "w", encoding="utf-8") as f:
        f.write(raw)
    try:
        from ..mesh2step import obj_to_step
        step_path = os.path.join(out_dir, f"{base}.step")
        obj_to_step(obj_path, step_path, name=base, log=log)
        log(f"3D: OBJ сконвертирован в STEP -> {os.path.basename(step_path)}")
        return step_path
    except Exception as e:
        log(f"3D: конвертация OBJ->STEP не удалась ({e}), оставлен {obj_path}")
        return ""
