"""
Чтение бинарных библиотек Altium (.SchLib, .PcbLib).

Нужно, чтобы «переварить» готовые библиотеки от Ultra Librarian, SnapEDA,
Component Search Engine и т.п.: из .SchLib берём список выводов (номер, имя,
электрический тип, секция), из .PcbLib -- имена посадочных мест и их
геометрию: площадки, линии, дуги, заливки.

Графика символа не читается: символ всё равно перерисовывается по ГОСТ.
Посадочное место в целевую библиотеку переносит сам Altium (копированием
из исходной библиотеки) -- геометрия нужна, чтобы её было видно в
программе: превью, габариты, число площадок, посадка 3D-модели.

Формат .SchLib: OLE-контейнер, поток <Component>/Data состоит из записей
    uint16 length; uint16 type;  payload[length]
type = 0     -- ASCII-запись вида |RECORD=NN|Key=Value|...
type = 0x100 -- бинарная запись (вывод, RECORD=2)
"""
from __future__ import annotations

import copy
import os
import re
import struct
import sys
from typing import Dict, List, Optional, Tuple

try:
    import olefile
except ImportError:  # подскажем понятно
    olefile = None

from ..ir import (ETYPES, Component, Footprint, FpPrim, Pad,
                  SymPin, Symbol)
from .easyeda import DERIVED_LAYERS
from .. import classify

_SWAP_RE = re.compile(r"^\d+\|&\|")

# Мусорные параметры, которыми Ultra Librarian / SnapEDA заполняют шаблон
_JUNK_KEYS = {"Copyright", "RefDes", "Type", "Component Kind", "Component Type"}


def _useful(name: str, text: str) -> bool:
    if name in _JUNK_KEYS:
        return False
    t = (text or "").strip()
    return bool(t) and t.lower() != name.strip().lower()


def _require_ole():
    """
    Проверить, что есть чем читать OLE-файл, и подсказать по делу.

    Совет «pip install olefile» бесполезен в двух самых частых случаях:
    у собранного exe своего pip нет вообще, а из исходников человек ставит
    пакет не в то окружение, из которого запускает программу. Поэтому
    говорим, куда именно ставить, и печатаем свой sys.executable.
    """
    if olefile is not None:
        return
    if getattr(sys, "frozen", False):
        raise RuntimeError(
            "в эту сборку не попал модуль olefile, поэтому .SchLib и "
            ".PcbLib она читать не умеет. Возьмите свежий установщик со "
            "страницы Releases либо пересоберите exe: сначала "
            "pip install olefile, затем python build_exe.py")
    raise RuntimeError(
        "не установлен модуль olefile. Ставить нужно в то окружение, из "
        "которого запущена программа:\n    "
        + sys.executable + " -m pip install olefile")


# ----------------------------------------------------------------- записи ----

def _iter_records(data: bytes):
    i = 0
    n = len(data)
    while i + 4 <= n:
        ln, typ = struct.unpack_from("<HH", data, i)
        payload = data[i + 4:i + 4 + ln]
        if len(payload) < ln:
            break
        yield typ, payload
        i += 4 + ln


def _ascii_props(payload: bytes) -> Dict[str, str]:
    s = payload.rstrip(b"\x00").decode("cp1251", errors="replace")
    out: Dict[str, str] = {}
    for part in s.split("|"):
        if not part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v
    return out


def _pascal_strings(b: bytes, off: int) -> List[str]:
    out = []
    while off < len(b):
        ln = b[off]
        out.append(b[off + 1:off + 1 + ln].decode("cp1251", errors="replace"))
        off += 1 + ln
    return out


def _parse_pin(payload: bytes) -> Optional[SymPin]:
    if len(payload) < 27 or payload[0] != 2:
        return None
    owner_part = struct.unpack_from("<h", payload, 5)[0]
    etype_i = payload[14]
    flags = payload[15]
    length = struct.unpack_from("<h", payload, 16)[0]
    x = struct.unpack_from("<h", payload, 18)[0]
    y = struct.unpack_from("<h", payload, 20)[0]
    strs = _pascal_strings(payload, 26)

    name, desig = "", ""
    swap_i = next((i for i, s in enumerate(strs) if _SWAP_RE.match(s)), None)
    if swap_i is not None and swap_i >= 2:
        name = strs[swap_i - 3] if swap_i >= 3 else strs[0]
        desig = strs[swap_i - 2]
    elif len(strs) >= 2:
        name, desig = strs[0], strs[1]
    elif strs:
        name = strs[0]

    rot = (flags & 0x03) * 90
    p = SymPin(
        number=desig.strip(),
        name=name.strip(),
        etype=ETYPES[etype_i] if 0 <= etype_i < len(ETYPES) else "passive",
        unit=max(1, owner_part),
        show_name=bool(flags & 0x08),
        show_number=bool(flags & 0x10),
    )
    # 10-mil единицы -> милы
    p.x, p.y, p.length = x * 10, y * 10, length * 10
    p.rotation = rot
    # сторона: 180° = вывод уходит влево => пин слева
    p.side = {0: "R", 90: "T", 180: "L", 270: "B"}.get(rot, "L")
    return p


# ---------------------------------------------------------------- SchLib -----

def read_schlib(path: str) -> Dict[str, dict]:
    """
    Возвращает {LibReference: {'name','description','designator','part_count',
                               'pins':[SymPin], 'params':{}, 'footprints':[str]}}
    """
    _require_ole()
    ole = olefile.OleFileIO(path)
    try:
        comps: Dict[str, dict] = {}
        for entry in ole.listdir():
            if len(entry) != 2 or entry[1] != "Data":
                continue
            storage = entry[0]
            if storage in ("FileHeader", "Storage", "Library", "FileVersionInfo"):
                continue
            data = ole.openstream("/".join(entry)).read()
            info = {
                "name": storage, "description": "", "designator": "U?",
                "part_count": 1, "pins": [], "params": {}, "footprints": [],
            }
            for typ, payload in _iter_records(data):
                if typ == 0:
                    p = _ascii_props(payload)
                    rec = p.get("RECORD", "")
                    if rec == "1":
                        info["name"] = p.get("LibReference", storage)
                        info["description"] = p.get("ComponentDescription", "")
                        info["part_count"] = int(p.get("PartCount", "2") or 2) - 1
                        info["part_count"] = max(1, info["part_count"])
                    elif rec == "34":
                        info["designator"] = p.get("Text", "U?")
                    elif rec == "41":
                        nm = p.get("Name", "")
                        tx = p.get("Text", "")
                        if nm and not nm.startswith("%") and _useful(nm, tx):
                            info["params"][nm] = tx
                    elif rec == "45":
                        if p.get("ModelType", "") == "PCBLIB":
                            mn = p.get("ModelName", "")
                            if mn and mn not in info["footprints"]:
                                info["footprints"].append(mn)
                else:
                    pin = _parse_pin(payload)
                    if pin is not None:
                        info["pins"].append(pin)
            comps[info["name"]] = info
        return comps
    finally:
        ole.close()


def read_schlib_header(path: str) -> Dict[str, str]:
    _require_ole()
    ole = olefile.OleFileIO(path)
    try:
        if not ole.exists("FileHeader"):
            return {}
        data = ole.openstream("FileHeader").read()
        for typ, payload in _iter_records(data):
            if typ == 0:
                return _ascii_props(payload)
        return {}
    finally:
        ole.close()


# ---------------------------------------------------------------- PcbLib -----

def read_pcblib_names(path: str) -> List[str]:
    """Список имён посадочных мест в .PcbLib."""
    _require_ole()
    ole = olefile.OleFileIO(path)
    try:
        skip = {"FileHeader", "Library", "FileVersionInfo", "Storage"}
        names = []
        for entry in ole.listdir():
            if len(entry) >= 2 and entry[1] == "Data" and entry[0] not in skip:
                names.append(entry[0])
        return names
    finally:
        ole.close()


# ------------------------------------------------- геометрия .PcbLib ---------

# Внутренняя единица Altium -- 1/10000 мила.
PCB_UNITS_PER_MM = 10000.0 / 0.0254

# Номера слоёв Altium -> наши имена. 1..32 -- медь, 33/34 -- шелкография,
# 35..38 -- паста и маска (их Altium делает сам из площадок, графику оттуда
# не берём), 57..72 -- механические, из них 13-й обычно сборочный, 15-й --
# область установки.
PCB_LAYER = {1: "copper_top", 32: "copper_bot", 33: "silk", 34: "silk_bot",
             35: "paste", 36: "paste_bot", 37: "mask", 38: "mask_bot",
             56: "keepout", 69: "assy", 71: "courtyard", 74: "multi"}
PAD_LAYER = {1: "top", 32: "bottom", 74: "multi"}
PCB_PAD_SHAPE = {1: "round", 2: "rect", 3: "octagon", 9: "roundrect"}

# Сколько блоков-строк идёт в записи перед телом: у площадки шесть
# (имя, три служебных, тело, хвост), у строки текста два.
_PCB_BLOCKS = {2: 6, 5: 2}


def _pcb_records(data: bytes):
    """Записи потока <посадка>/Data: (тип, [блоки])."""
    if len(data) < 4:
        return
    off = 4 + struct.unpack_from("<I", data, 0)[0]
    while off < len(data):
        kind = data[off]
        off += 1
        blocks = []
        try:
            for _ in range(_PCB_BLOCKS.get(kind, 1)):
                ln = struct.unpack_from("<I", data, off)[0]
                blocks.append(data[off + 4:off + 4 + ln])
                off += 4 + ln
        except struct.error:
            return
        yield kind, blocks


def _pcb_mm(b: bytes, at: int) -> float:
    return struct.unpack_from("<i", b, at)[0] / PCB_UNITS_PER_MM


def _pcb_footprint(name: str, data: bytes) -> Footprint:
    """
    Разобрать одно посадочное место из .PcbLib.

    Раньше из библиотеки бралось только имя: посадку в целевую библиотеку
    всё равно копирует сам Altium. Но тогда в программе от неё не остаётся
    ничего -- ни превью, ни габаритов, ни числа площадок, и компонент из
    архива выглядит так, будто приехал один символ. Геометрию читаем для
    показа и замеров; в сборку по-прежнему уходит ссылка на исходную
    библиотеку, так что вендорская посадка не искажается.
    """
    fp = Footprint(name=name)
    for kind, blocks in _pcb_records(data):
        # У площадки тело -- пятый блок (шестой пустой, служебный),
        # у остальных примитивов -- первый и единственный.
        b = (blocks[4] if kind == 2 and len(blocks) > 4
             else (blocks[0] if blocks else b""))
        try:
            if kind == 2 and len(b) >= 61:           # площадка
                layer = PAD_LAYER.get(b[0], "top")
                w, h = _pcb_mm(b, 21), _pcb_mm(b, 25)
                hole = _pcb_mm(b, 45)
                shape = PCB_PAD_SHAPE.get(b[49], "rect")
                if shape == "round" and abs(w - h) > 1e-6:
                    shape = "oval"
                num = blocks[0]
                fp.pads.append(Pad(
                    number=num[1:1 + num[0]].decode("cp1251", "replace")
                    if num else "",
                    x=_pcb_mm(b, 13), y=_pcb_mm(b, 17), w=w, h=h,
                    shape=shape, rot=struct.unpack_from("<d", b, 52)[0],
                    layer="multi" if hole > 0 else layer, hole=hole,
                    plated=bool(b[60])))
            elif kind == 4 and len(b) >= 33:         # линия
                lay = PCB_LAYER.get(b[0], "mech")
                if lay in DERIVED_LAYERS:
                    continue
                fp.prims.append(FpPrim(
                    kind="line", layer=lay,
                    pts=[[_pcb_mm(b, 13), _pcb_mm(b, 17)],
                         [_pcb_mm(b, 21), _pcb_mm(b, 25)]],
                    width=_pcb_mm(b, 29)))
            elif kind == 1 and len(b) >= 45:         # дуга
                lay = PCB_LAYER.get(b[0], "mech")
                if lay in DERIVED_LAYERS:
                    continue
                fp.prims.append(FpPrim(
                    kind="arc", layer=lay,
                    pts=[[_pcb_mm(b, 13), _pcb_mm(b, 17)]],
                    radius=_pcb_mm(b, 21),
                    a1=struct.unpack_from("<d", b, 25)[0],
                    a2=struct.unpack_from("<d", b, 33)[0],
                    width=_pcb_mm(b, 41)))
            elif kind == 6 and len(b) >= 29:         # заливка
                lay = PCB_LAYER.get(b[0], "mech")
                if lay in DERIVED_LAYERS:
                    continue
                fp.prims.append(FpPrim(
                    kind="rect", layer=lay, filled=True,
                    pts=[[_pcb_mm(b, 13), _pcb_mm(b, 17)],
                         [_pcb_mm(b, 21), _pcb_mm(b, 25)]],
                    width=0.0))
        except (struct.error, IndexError, UnicodeDecodeError):
            continue
    return fp


def read_pcblib(path: str) -> Dict[str, Footprint]:
    """Посадочные места из .PcbLib с геометрией: {имя: Footprint}."""
    _require_ole()
    ole = olefile.OleFileIO(path)
    try:
        skip = {"FileHeader", "Library", "FileVersionInfo", "Storage"}
        out: Dict[str, Footprint] = {}
        for entry in ole.listdir():
            if len(entry) != 2 or entry[1] != "Data" or entry[0] in skip:
                continue
            data = ole.openstream("/".join(entry)).read()
            out[entry[0]] = _pcb_footprint(entry[0], data)
        return out
    finally:
        ole.close()


def pcblib_models(path: str) -> List[str]:
    """Имена 3D-моделей, вшитых в .PcbLib (информационно)."""
    _require_ole()
    ole = olefile.OleFileIO(path)
    try:
        if not ole.exists("Library/Models/Data"):
            return []
        data = ole.openstream("Library/Models/Data").read()
        out = []
        for typ, payload in _iter_records(data):
            p = _ascii_props(payload)
            nm = p.get("NAME") or p.get("Name")
            if nm:
                out.append(nm)
        return out
    finally:
        ole.close()


# --------------------------------------------------------- сборка Component --

def components_from_altium(schlib: str, pcblib: str = "",
                           step_files: Optional[Dict[str, str]] = None
                           ) -> List[Component]:
    """
    Собрать список Component из пары .SchLib/.PcbLib.
    step_files: {имя_посадки_или_'*': путь_к_step}
    """
    from ..ir import Model3D
    step_files = step_files or {}
    geoms: Dict[str, Footprint] = {}
    if pcblib and os.path.isfile(pcblib):
        try:
            geoms = read_pcblib(pcblib)
        except Exception:
            # геометрия -- не повод терять компонент: имена читаются
            # отдельным, более простым путём
            geoms = {}
    fp_names = list(geoms) or (read_pcblib_names(pcblib)
                               if pcblib and os.path.isfile(pcblib) else [])
    out: List[Component] = []
    for name, info in read_schlib(schlib).items():
        c = Component()
        c.name = name
        c.description = info["description"]
        if c.description.strip().lower() in ("description", "n/a", "-"):
            c.description = ""
        c.designator = info["designator"] or "U?"
        c.source = "altium"
        c.source_ref = f"{os.path.basename(schlib)}::{name}"
        c.params.update(info["params"])
        c.manufacturer = info["params"].get("Manufacturer_Name", "") or \
            info["params"].get("Manufacturer", "")
        c.mpn = info["params"].get("Manufacturer_Part_Number", "") or \
            info["params"].get("MPN", "")
        c.datasheet = info["params"].get("Datasheet", "") or \
            info["params"].get("Datasheet_Link", "")
        pins = info["pins"]
        c.raw_pins = pins
        c.symbol = Symbol(part_count=info["part_count"], pins=list(pins))
        want = info["footprints"] or fp_names
        for fpn in want:
            if pcblib and (fpn in fp_names or not fp_names):
                fp = geoms.get(fpn)
                fp = copy.deepcopy(fp) if fp is not None else Footprint(name=fpn)
                fp.source_pcblib, fp.source_name = pcblib, fpn
                st = step_files.get(fpn) or step_files.get("*")
                if st:
                    fp.model = Model3D(path=st)
                c.footprints.append(fp)
        pkg = want[0] if want else ""
        c.ctype = classify.guess_type(name, c.description, "", pins, pkg,
                                      c.designator, pkg)
        c.params.setdefault("PinCount", str(len(pins)))
        out.append(c)
    return out
