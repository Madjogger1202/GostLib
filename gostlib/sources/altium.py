"""
Чтение бинарных библиотек Altium (.SchLib, .PcbLib).

Нужно, чтобы «переварить» готовые библиотеки от Ultra Librarian, SnapEDA,
Component Search Engine и т.п.: из .SchLib берём список выводов (номер, имя,
электрический тип, секция), из .PcbLib -- список имён посадочных мест.
Графика символа не читается: символ всё равно перерисовывается по ГОСТ.
Посадочное место переносится в целевую библиотеку самим Altium (копированием).

Формат .SchLib: OLE-контейнер, поток <Component>/Data состоит из записей
    uint16 length; uint16 type;  payload[length]
type = 0     -- ASCII-запись вида |RECORD=NN|Key=Value|...
type = 0x100 -- бинарная запись (вывод, RECORD=2)
"""
from __future__ import annotations

import os
import re
import struct
from typing import Dict, List, Optional, Tuple

try:
    import olefile
except ImportError:  # подскажем понятно
    olefile = None

from ..ir import ETYPES, Component, Footprint, SymPin, Symbol
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
    if olefile is None:
        raise RuntimeError(
            "Не установлен модуль olefile. Выполните: pip install olefile")


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
    fp_names = read_pcblib_names(pcblib) if pcblib and os.path.isfile(pcblib) else []
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
                fp = Footprint(name=fpn, source_pcblib=pcblib, source_name=fpn)
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
