"""
Прямая запись .PcbLib для Altium.

Формат разобран по реальным файлам (Ultra Librarian + пустая библиотека,
созданная самим Altium):

  FileHeader                     "PCB 6.0 Binary Library File"
  Library/Data                   большой блок настроек платы + список имён
  Library/ComponentParamsTOC     сводка по посадкам
  Library/*                      слои, модели, библиотека площадок
  <Посадка>/Data                 имя + записи примитивов
  <Посадка>/Header               число примитивов
  <Посадка>/Parameters           |PATTERN=..|HEIGHT=..|DESCRIPTION=..

Запись примитива: байт типа, затем блоки `uint32 длина + данные`
(у площадки таких блоков шесть, у остальных -- один).

Пишутся только площадки и отрезки: дуги и окружности раскладываются на
отрезки. Так надёжнее -- нет ни одной записи, формат которой не подтверждён
живым файлом, а на глаз разница неразличима.

Настройки платы (слои, правила) берутся из шаблона -- это файл, созданный
самим Altium, поэтому стек слоёв заведомо корректный.
"""
from __future__ import annotations

import math
import os
import random
import re
import string
import struct
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import olefile
except ImportError:
    olefile = None

from ..ir import Footprint, Pad
from .cfb import write_cfb

# 1 внутренняя единица Altium = 1/10000 мила
UNITS_PER_MM = 10000.0 / 0.0254


def mm(v: float) -> int:
    return int(round(float(v) * UNITS_PER_MM))


# --- номера слоёв Altium ------------------------------------------------------
LAYER = {
    "top": 1, "copper_top": 1,
    "bottom": 32, "copper_bot": 32,
    "silk": 33, "silk_bot": 34,
    "paste": 35, "paste_bot": 36,
    "mask": 37, "mask_bot": 38,
    "keepout": 56,
    "mech": 57,          # Mechanical1
    "assy": 69,          # Mechanical13
    "courtyard": 71,     # Mechanical15
    "multi": 74,
}

SHAPE = {"round": 1, "oval": 1, "rect": 2, "roundrect": 2, "octagon": 3}

# Эталонные записи, снятые с рабочей библиотеки. Патчатся только те поля,
# смысл которых подтверждён; остальное остаётся как у Altium.
PAD_TEMPLATE = bytes.fromhex(
    "010800ffffffffffffffffffff5191ebff869e0f00ab1201009eda0400ab1201009eda0400ab1201009eda0400000000000202020000000000e070400100000000000000a08601000400a0860100400d0300400d030000000000000000000000000000000002020000000000000000000000010000010000000000000000000000000000000000000000000000000000000000000000000000000000000000000000ffffff7fffffff7f00011a0000000000000000000000010300000000000000000000000000000000")

TRACK_TEMPLATE = bytes.fromhex(
    "210c00ffffffffffffffffffff102eeaff102eeaff6dd8edff102eeaff60ea000000000000000000000600030100000000")

# смещения внутри записей
PAD_OFF = dict(layer=0, x=13, y=17, topx=21, topy=25, midx=29, midy=33,
               botx=37, boty=41, hole=45, shape_top=49, shape_mid=50,
               shape_bot=51, rot=52, plated=60)
TRK_OFF = dict(layer=0, x1=13, y1=17, x2=21, y2=25, width=29)

ARC_SEGMENTS = 32          # на полную окружность


def _uid8() -> str:
    return "".join(random.choice(string.ascii_uppercase) for _ in range(8))


def _blk(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def _pascal(s: str) -> bytes:
    raw = (s or "").encode("cp1251", errors="replace")[:255]
    return bytes([len(raw)]) + raw


def _pad_record(p: Pad) -> bytes:
    body = bytearray(PAD_TEMPLATE)
    layer = LAYER.get(p.layer, 1)
    if p.hole > 0:
        layer = LAYER["multi"]
    body[PAD_OFF["layer"]] = layer
    struct.pack_into("<i", body, PAD_OFF["x"], mm(p.x))
    struct.pack_into("<i", body, PAD_OFF["y"], mm(p.y))
    w, h = mm(p.w), mm(p.h)
    for k in ("topx", "midx", "botx"):
        struct.pack_into("<i", body, PAD_OFF[k], w)
    for k in ("topy", "midy", "boty"):
        struct.pack_into("<i", body, PAD_OFF[k], h)
    struct.pack_into("<i", body, PAD_OFF["hole"], mm(p.hole))
    shp = SHAPE.get(p.shape, 2)
    body[PAD_OFF["shape_top"]] = shp
    body[PAD_OFF["shape_mid"]] = shp
    body[PAD_OFF["shape_bot"]] = shp
    struct.pack_into("<d", body, PAD_OFF["rot"], float(p.rot) % 360.0)
    body[PAD_OFF["plated"]] = 1 if p.plated else 0

    return (b"\x02"
            + _blk(_pascal(p.number))
            + _blk(b"\x00")
            + _blk(_pascal("|&|0"))
            + _blk(b"\x00")
            + _blk(bytes(body))
            + _blk(b""))


def _track_record(x1: float, y1: float, x2: float, y2: float,
                  width: float, layer: str) -> bytes:
    body = bytearray(TRACK_TEMPLATE)
    body[TRK_OFF["layer"]] = LAYER.get(layer, 33)
    struct.pack_into("<i", body, TRK_OFF["x1"], mm(x1))
    struct.pack_into("<i", body, TRK_OFF["y1"], mm(y1))
    struct.pack_into("<i", body, TRK_OFF["x2"], mm(x2))
    struct.pack_into("<i", body, TRK_OFF["y2"], mm(y2))
    struct.pack_into("<i", body, TRK_OFF["width"], mm(max(width, 0.02)))
    return b"\x04" + _blk(bytes(body))


def _arc_points(cx: float, cy: float, r: float, a1: float, a2: float
                ) -> List[Tuple[float, float]]:
    span = (a2 - a1) % 360.0
    if span == 0:
        span = 360.0
    n = max(2, int(round(ARC_SEGMENTS * span / 360.0)))
    pts = []
    for i in range(n + 1):
        a = math.radians(a1 + span * i / n)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


# идентификаторы объектов Altium -- нужны в потоке PrimitiveGuids
OBJ_PAD = 2
OBJ_TRACK = 4
OBJ_COMPONENT = 85


def _footprint_primitives(fp: Footprint) -> List[bytes]:
    out: List[bytes] = []
    for p in fp.pads:
        out.append(_pad_record(p))
    for pr in fp.prims:
        layer = pr.layer
        w = pr.width or 0.15
        if pr.kind == "line" and len(pr.pts) >= 2:
            (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
            out.append(_track_record(x1, y1, x2, y2, w, layer))
        elif pr.kind == "rect" and len(pr.pts) >= 2:
            (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
            for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)),
                         ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
                out.append(_track_record(a[0], a[1], b[0], b[1], w, layer))
        elif pr.kind in ("arc", "circle") and pr.pts:
            cx, cy = pr.pts[0]
            a1, a2 = (0.0, 360.0) if pr.kind == "circle" else (pr.a1, pr.a2)
            pts = _arc_points(cx, cy, pr.radius, a1, a2)
            for i in range(len(pts) - 1):
                out.append(_track_record(pts[i][0], pts[i][1],
                                         pts[i + 1][0], pts[i + 1][1], w, layer))
        elif pr.kind == "poly" and len(pr.pts) >= 3:
            pts = list(pr.pts) + [pr.pts[0]]
            for i in range(len(pts) - 1):
                out.append(_track_record(pts[i][0], pts[i][1],
                                         pts[i + 1][0], pts[i + 1][1],
                                         w or 0.1, layer))
    return out


def _load_template(template: str = "") -> Dict[str, object]:
    """Взять из шаблона потоки Library/* -- настройки платы и стек слоёв."""
    if olefile is None:
        raise RuntimeError("нужен модуль olefile: pip install olefile")
    if not template:
        template = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "templates", "empty.PcbLib")
    if not os.path.isfile(template):
        raise FileNotFoundError(
            f"не найден шаблон {template}. Создайте пустую PCB-библиотеку в "
            f"Altium (File -> New -> Library -> PCB Library), сохраните и "
            f"положите её по этому пути.")
    ole = olefile.OleFileIO(template)
    try:
        out: Dict[str, object] = {}
        # Library/*   -- настройки платы, стек слоёв, библиотека площадок
        # FileHeader  -- берётся как есть: длина строки в нём считается БЕЗ
        #                байта длины паскаль-строки, ошибка на байт ломает
        #                разбор версии и Altium объявляет файл повреждённым
        # FileVersionInfo -- Altium ждёт этот поток, без него файл не читается
        for entry in ole.listdir():
            if entry[0] not in ("Library", "FileHeader", "FileVersionInfo"):
                continue
            data = ole.openstream("/".join(entry)).read()
            if len(entry) == 1:
                out[entry[0]] = data
                continue
            node = out.setdefault(entry[0], {})
            cur = node
            for part in entry[1:-1]:
                cur = cur.setdefault(part, {})
            cur[entry[-1]] = data
        return out
    finally:
        ole.close()


def _patch_filename(settings: bytes, path: str) -> bytes:
    """
    Подменить |FILENAME=..| в блоке настроек. Путь подставляется функцией:
    в строке замены re обратные слэши Windows (C:\\Users\\...) были бы
    приняты за escape-последовательности и уронили бы sub.
    """
    newname = b"|FILENAME=" + (path or "").encode("cp1251", "replace") + b"|"
    return re.sub(rb"\|FILENAME=[^|]*\|", lambda _m: newname, settings, count=1)


def _safe_name(name: str, used: set) -> str:
    base = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in (name or "FP"))
    base = base[:31] or "FP"
    nm, n = base, 1
    while nm.upper() in used:
        suffix = f"_{n}"
        nm = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(nm.upper())
    return nm


def write_pcblib(path: str, footprints: Iterable[Footprint],
                 template: str = "", filename: str = "") -> str:
    fps = [f for f in footprints if f and f.name and (f.pads or f.prims)]
    tpl = _load_template(template)
    lib = dict(tpl.get("Library") or {})
    if not tpl.get("FileHeader"):
        raise ValueError("в шаблоне нет потока FileHeader")

    tree: Dict[str, object] = {}
    entries: List[Tuple[str, Footprint, List[bytes]]] = []
    used: set = set()
    for fp in fps:
        entries.append((_safe_name(fp.name, used), fp, _footprint_primitives(fp)))

    # --- список компонентов в Library/Data ---
    data = lib.get("Data") or b""
    if data:
        n = struct.unpack_from("<I", data, 0)[0]
        settings = _patch_filename(data[4:4 + n],
                                   filename or os.path.abspath(path))
    else:
        settings = b""
    names_blob = struct.pack("<I", len(entries))
    for nm, _fp, _prims in entries:
        names_blob += _blk(_pascal(nm))
    lib["Data"] = _blk(settings) + names_blob

    toc = b""
    for nm, fp, prims in entries:
        toc += (f"Name={nm}|Pad Count={len(fp.pads)}|"
                f"Height={fp.height or 0}|Description={fp.description or ''}"
                "\r\n").encode("cp1251", "replace")
    lib["ComponentParamsTOC"] = {"Header": struct.pack("<I", len(entries)),
                                 "Data": _blk(toc + b"\x00")}
    tree["Library"] = lib

    # --- служебные потоки: как есть из шаблона, они от самого Altium ---
    tree["FileHeader"] = tpl["FileHeader"]
    if tpl.get("FileVersionInfo"):
        tree["FileVersionInfo"] = tpl["FileVersionInfo"]

    # --- сами посадки ---
    for nm, fp, prims in entries:
        body = _blk(_pascal(nm)) + b"".join(prims)
        params = (f"|PATTERN={nm}|HEIGHT={fp.height or 0}mil"
                  f"|DESCRIPTION={fp.description or ''}"
                  "|ITEMGUID=|REVISIONGUID=").encode("cp1251", "replace")
        # Записей здесь ровно столько, сколько указано в Header: первая --
        # сам компонент, дальше по одной на примитив. Если не сойдётся,
        # Altium молча выбрасывает посадку из библиотеки.
        kinds = [OBJ_PAD] * len(fp.pads) + \
                [OBJ_TRACK] * (len(prims) - len(fp.pads))
        guids = (struct.pack("<I", OBJ_COMPONENT) + struct.pack("<I", 0)
                 + bytes(random.getrandbits(8) for _ in range(16)))
        for i, kind in enumerate(kinds, 1):
            guids += struct.pack("<I", kind) + struct.pack("<I", i) + \
                bytes(random.getrandbits(8) for _ in range(16))
        uniq = b""
        for i, p in enumerate(fp.pads):
            rec = (f"|PRIMITIVEINDEX={i}|PRIMITIVEOBJECTID=Pad|UNIQUEID="
                   ).encode("cp1251", "replace")
            uniq += _blk(rec + b"\x00")
        tree[nm] = {
            "Data": body,
            "Header": struct.pack("<I", len(prims)),
            "Parameters": _blk(params + b"\x00"),
            "WideStrings": struct.pack("<I", 1) + b"\x00",
            "PrimitiveGuids": {"Header": struct.pack("<I", len(prims) + 1),
                               "Data": guids},
            "UniqueIDPrimitiveInformation": {
                "Header": struct.pack("<I", len(fp.pads)),
                "Data": uniq},
        }

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    write_cfb(path, tree)
    return path


# ------------------------------------------------------- обратное чтение -----

def read_pcblib_footprints(path: str) -> Dict[str, dict]:
    """Разобрать .PcbLib обратно -- для проверки того, что записали."""
    if olefile is None:
        raise RuntimeError("нужен модуль olefile")
    ole = olefile.OleFileIO(path)
    try:
        out: Dict[str, dict] = {}
        for entry in ole.listdir():
            if len(entry) != 2 or entry[1] != "Data":
                continue
            if entry[0] in ("FileHeader", "Library", "FileVersionInfo"):
                continue
            d = ole.openstream("/".join(entry)).read()
            n = struct.unpack_from("<I", d, 0)[0]
            off = 4 + n
            pads, tracks = [], []
            while off < len(d):
                t = d[off]
                off += 1
                parts = []
                for _ in range({2: 6, 5: 2}.get(t, 1)):
                    ln = struct.unpack_from("<I", d, off)[0]
                    parts.append(d[off + 4:off + 4 + ln])
                    off += 4 + ln
                if t == 2 and len(parts) >= 5 and len(parts[4]) >= 61:
                    b = parts[4]
                    pads.append(dict(
                        number=parts[0][1:1 + parts[0][0]].decode("cp1251", "replace"),
                        layer=b[0],
                        x=struct.unpack_from("<i", b, 13)[0] / UNITS_PER_MM,
                        y=struct.unpack_from("<i", b, 17)[0] / UNITS_PER_MM,
                        w=struct.unpack_from("<i", b, 21)[0] / UNITS_PER_MM,
                        h=struct.unpack_from("<i", b, 25)[0] / UNITS_PER_MM,
                        hole=struct.unpack_from("<i", b, 45)[0] / UNITS_PER_MM,
                        shape=b[49],
                        rot=struct.unpack_from("<d", b, 52)[0],
                        plated=bool(b[60])))
                elif t == 4 and parts and len(parts[0]) >= 33:
                    b = parts[0]
                    tracks.append(dict(
                        layer=b[0],
                        x1=struct.unpack_from("<i", b, 13)[0] / UNITS_PER_MM,
                        y1=struct.unpack_from("<i", b, 17)[0] / UNITS_PER_MM,
                        x2=struct.unpack_from("<i", b, 21)[0] / UNITS_PER_MM,
                        y2=struct.unpack_from("<i", b, 25)[0] / UNITS_PER_MM,
                        width=struct.unpack_from("<i", b, 29)[0] / UNITS_PER_MM))
            out[entry[0]] = {"pads": pads, "tracks": tracks}
        return out
    finally:
        ole.close()
