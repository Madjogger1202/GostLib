"""
Прямая запись .SchLib для Altium -- без скриптов и без Altium вообще.

Формат разобран по реальным файлам: OLE-контейнер, в нём поток FileHeader,
поток Storage и по одному хранилищу на компонент с потоком Data. Внутри
Data -- записи `uint16 длина; uint16 тип; данные`, где тип 0 -- ASCII-строка
вида |RECORD=NN|Ключ=Значение|..., а тип 0x100 -- двоичная запись вывода.

Байтовая раскладка вывода повторяет то, что пишет сам Altium, поэтому
файл читается им как родной.

Координаты в файле -- десятые доли дюйма (единица = 10 mil).
"""
from __future__ import annotations

import os
import random
import string
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..classify import CTYPE_NAME
from ..gost.style import DEFAULT, Style
from ..ir import ETYPES, Component, SymPin, SymPrim
from .cfb import write_cfb

HEADER_MAGIC = ("Protel for Windows - Schematic Library Editor "
                "Binary File Version 5.0")

COLOR_GRAPHIC = 128          # тёмно-красный -- корпус, линии
COLOR_TEXT = 8388608         # синий -- имена выводов, позиционное обозначение
COLOR_NUMBER = 128           # номера выводов
COLOR_AREA = 11599871        # заливка по умолчанию

# Altium: 0=BottomLeft .. 8=TopRight
JUST_MAP = {0: 0, 1: 3, 2: 6}


def _uid() -> str:
    return "".join(random.choice(string.ascii_uppercase) for _ in range(8))


def u(mils: float) -> int:
    """Милы -> единицы файла (10 mil)."""
    return int(round(mils / 10.0))


def _ascii_record(props: Sequence[Tuple[str, object]]) -> bytes:
    parts = []
    for k, v in props:
        if v is None:
            continue
        parts.append(f"|{k}={v}")
    payload = "".join(parts).encode("cp1251", errors="replace") + b"\x00"
    return len(payload).to_bytes(2, "little") + b"\x00\x00" + payload


def _pascal(s: str) -> bytes:
    raw = (s or "").encode("cp1251", errors="replace")[:255]
    return bytes([len(raw)]) + raw


def _pin_record(p: SymPin) -> bytes:
    """Двоичная запись вывода -- байт в байт как у Altium."""
    flags = 0x20                      # бит, который Altium ставит всегда
    rot = int(p.rotation) % 360
    flags |= {0: 0, 90: 1, 180: 2, 270: 3}.get(rot, 0)
    if p.show_name:
        flags |= 0x08
    if p.show_number:
        flags |= 0x10

    etype = ETYPES.index(p.etype) if p.etype in ETYPES else 4
    body = bytearray()
    body += b"\x02"                                   # тип записи -- вывод
    body += b"\x00\x00\x00\x00"
    body += int(max(1, p.unit)).to_bytes(2, "little", signed=True)
    body += b"\x00" * 6
    body += b"\x01"                                   # display mode
    body += bytes([etype & 0xFF])
    body += bytes([flags & 0xFF])
    body += u(p.length).to_bytes(2, "little", signed=True)
    body += u(p.x).to_bytes(2, "little", signed=True)
    body += u(p.y).to_bytes(2, "little", signed=True)
    body += b"\x00" * 4
    body += _pascal(p.name)
    body += _pascal(p.number)
    body += _pascal("0")
    body += _pascal(f"0|&|{p.number}")
    body += _pascal("")
    return len(body).to_bytes(2, "little") + b"\x00\x01" + bytes(body)


class _FontTable:
    """Шрифты нумеруются с единицы; на каждый размер -- своя запись."""

    def __init__(self, name: str):
        self.name = name
        self.sizes: List[int] = []

    def fid(self, size: int) -> int:
        size = int(size) if size else 10
        if size not in self.sizes:
            self.sizes.append(size)
        return self.sizes.index(size) + 1

    def header_props(self) -> List[Tuple[str, object]]:
        out: List[Tuple[str, object]] = [("FontIdCount", len(self.sizes) or 1)]
        if not self.sizes:
            self.sizes.append(10)
        for i, s in enumerate(self.sizes, 1):
            out.append((f"Size{i}", s))
            out.append((f"FontName{i}", self.name))
        return out


def _component_records(c: Component, st: Style, fonts: _FontTable) -> List[bytes]:
    sym = c.symbol
    recs: List[bytes] = []
    idx = 0

    def add(props):
        nonlocal idx
        recs.append(_ascii_record(props))
        i = idx
        idx += 1
        return i

    pins = list(sym.pins)
    parts = max(1, sym.part_count)

    add([("RECORD", 1),
         ("LibReference", c.name),
         ("ComponentDescription", c.description or ""),
         ("PartCount", parts + 1),
         ("DisplayModeCount", 1),
         ("IndexInSheet", -1),
         ("OwnerPartId", -1),
         ("CurrentPartId", 1),
         ("LibraryPath", "*"),
         ("SourceLibraryName", "*"),
         ("SheetPartFileName", "*"),
         ("TargetFileName", "*"),
         ("UniqueID", _uid()),
         ("AreaColor", COLOR_AREA),
         ("Color", COLOR_GRAPHIC),
         ("PartIDLocked", "T"),
         ("AllPinCount", len(pins))])

    # --- позиционное обозначение ---
    dx, dy = sym.designator_pos
    add([("RECORD", 34),
         ("IndexInSheet", -1),
         ("OwnerPartId", -1),
         ("Location.X", u(dx)),
         ("Location.Y", u(dy)),
         ("Color", COLOR_TEXT),
         ("FontID", fonts.fid(st.size_desig)),
         ("Text", c.designator or "U?"),
         ("Name", "Designator"),
         ("ReadOnlyState", 1),
         ("UniqueID", _uid())])

    # --- комментарий (тип/парт-номер в основном поле) ---
    cx, cy = sym.comment_pos
    add([("RECORD", 41),
         ("IndexInSheet", -1),
         ("OwnerPartId", -1),
         ("Location.X", u(cx)),
         ("Location.Y", u(cy)),
         ("Color", COLOR_GRAPHIC),
         ("FontID", fonts.fid(st.size_type)),
         ("Justification", 4),
         ("Text", c.mpn or c.value or c.name),
         ("Name", "Comment"),
         ("UniqueID", _uid())])

    # --- параметры ---
    params: Dict[str, str] = dict(c.params)
    if c.manufacturer:
        params.setdefault("Manufacturer", c.manufacturer)
    if c.mpn:
        params.setdefault("MPN", c.mpn)
    if c.datasheet:
        params.setdefault("Datasheet", c.datasheet)
    if c.value:
        params.setdefault("Value", c.value)
    params.setdefault("ТипЭлемента", CTYPE_NAME.get(c.ctype, c.ctype))
    for name, value in params.items():
        if value is None or str(value) == "":
            continue
        add([("RECORD", 41),
             ("IndexInSheet", -1),
             ("OwnerPartId", -1),
             ("Location.X", u(dx)),
             ("Location.Y", u(dy) - 10),
             ("Color", COLOR_TEXT),
             ("FontID", fonts.fid(8)),
             ("IsHidden", "T"),
             ("Text", str(value)),
             ("Name", name),
             ("UniqueID", _uid())])

    # --- графика ---
    for pr in sym.prims:
        part = pr.unit
        if pr.kind == "rect" and len(pr.pts) >= 2:
            (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
            add([("RECORD", 14),
                 ("IsNotAccesible", "T"),
                 ("IndexInSheet", -1),
                 ("OwnerPartId", part),
                 ("Location.X", u(min(x1, x2))),
                 ("Location.Y", u(min(y1, y2))),
                 ("Corner.X", u(max(x1, x2))),
                 ("Corner.Y", u(max(y1, y2))),
                 ("LineWidth", pr.width),
                 ("Color", COLOR_GRAPHIC),
                 ("AreaColor", COLOR_AREA),
                 ("IsSolid", "T" if pr.filled else "F"),
                 ("Transparent", "F" if pr.filled else "T"),
                 ("UniqueID", _uid())])
        elif pr.kind == "line" and len(pr.pts) >= 2:
            (x1, y1), (x2, y2) = pr.pts[0], pr.pts[1]
            add([("RECORD", 13),
                 ("IsNotAccesible", "T"),
                 ("IndexInSheet", -1),
                 ("OwnerPartId", part),
                 ("Location.X", u(x1)),
                 ("Location.Y", u(y1)),
                 ("Corner.X", u(x2)),
                 ("Corner.Y", u(y2)),
                 ("LineWidth", pr.width),
                 ("Color", COLOR_GRAPHIC),
                 ("UniqueID", _uid())])
        elif pr.kind == "arc" and pr.pts:
            cxx, cyy = pr.pts[0]
            add([("RECORD", 12),
                 ("IsNotAccesible", "T"),
                 ("IndexInSheet", -1),
                 ("OwnerPartId", part),
                 ("Location.X", u(cxx)),
                 ("Location.Y", u(cyy)),
                 ("Radius", u(pr.radius)),
                 ("StartAngle", f"{pr.a1:.3f}"),
                 ("EndAngle", f"{pr.a2:.3f}"),
                 ("LineWidth", pr.width),
                 ("Color", COLOR_GRAPHIC),
                 ("UniqueID", _uid())])
        elif pr.kind == "ellipse" and pr.pts:
            cxx, cyy = pr.pts[0]
            add([("RECORD", 8),
                 ("IsNotAccesible", "T"),
                 ("IndexInSheet", -1),
                 ("OwnerPartId", part),
                 ("Location.X", u(cxx)),
                 ("Location.Y", u(cyy)),
                 ("Radius", u(pr.radius)),
                 ("SecondaryRadius", u(pr.radius)),
                 ("LineWidth", pr.width),
                 ("Color", COLOR_GRAPHIC),
                 ("AreaColor", COLOR_AREA),
                 ("IsSolid", "T" if pr.filled else "F"),
                 ("Transparent", "F" if pr.filled else "T"),
                 ("UniqueID", _uid())])
        elif pr.kind == "poly" and len(pr.pts) >= 2:
            props = [("RECORD", 7 if pr.filled else 6),
                     ("IsNotAccesible", "T"),
                     ("IndexInSheet", -1),
                     ("OwnerPartId", part),
                     ("LineWidth", pr.width),
                     ("Color", COLOR_GRAPHIC)]
            if pr.filled:
                props += [("AreaColor", COLOR_GRAPHIC), ("IsSolid", "T")]
            props.append(("LocationCount", len(pr.pts)))
            for i, pt in enumerate(pr.pts, 1):
                props.append((f"X{i}", u(pt[0])))
                props.append((f"Y{i}", u(pt[1])))
            props.append(("UniqueID", _uid()))
            add(props)
        elif pr.kind == "text" and pr.pts:
            x, y = pr.pts[0]
            just = JUST_MAP.get(pr.vjustify, 3) + {0: 0, 1: 1, 2: 2}.get(pr.justify, 0)
            orient = {0: 0, 90: 1, 180: 2, 270: 3}.get(int(pr.rotation) % 360, 0)
            add([("RECORD", 4),
                 ("IsNotAccesible", "T"),
                 ("IndexInSheet", -1),
                 ("OwnerPartId", part),
                 ("Location.X", u(x)),
                 ("Location.Y", u(y)),
                 ("Orientation", orient),
                 ("Justification", just),
                 ("Color", COLOR_NUMBER if pr.size == st.size_pin_num
                  else COLOR_TEXT),
                 ("FontID", fonts.fid(pr.size)),
                 ("Text", pr.text),
                 ("UniqueID", _uid())])

    # --- выводы (двоичные записи) ---
    for p in pins:
        recs.append(_pin_record(p))
        idx += 1

    # --- привязка посадочных мест ---
    list_idx = add([("RECORD", 44)])
    for fp in c.footprints:
        model = fp.source_name or fp.name
        if not model:
            continue
        mi = add([("RECORD", 45),
                  ("OwnerIndex", list_idx),
                  ("IndexInSheet", -1),
                  ("ModelName", model),
                  ("ModelType", "PCBLIB"),
                  ("DatafileCount", 1),
                  ("ModelDatafileEntity0", model),
                  ("ModelDatafileKind0", "PCBLIB"),
                  ("IsCurrent", "T"),
                  ("UniqueID", _uid())])
        add([("RECORD", 46), ("OwnerIndex", mi)])
        add([("RECORD", 48), ("OwnerIndex", mi)])
    return recs


def _safe_storage_name(name: str, used: set) -> str:
    """Имя хранилища в OLE -- не длиннее 31 знака и уникальное."""
    base = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in (name or "COMP"))
    base = base[:31] or "COMP"
    nm = base
    n = 1
    while nm.upper() in used:
        suffix = f"_{n}"
        nm = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(nm.upper())
    return nm


def write_schlib(path: str, components: Iterable[Component],
                 st: Optional[Style] = None) -> str:
    """Собрать .SchLib целиком. Библиотека -- проекция каталога, не дописка."""
    st = st or DEFAULT
    comps = [c for c in components if c and c.name]
    fonts = _FontTable(st.font)

    tree: Dict[str, object] = {}
    header: List[Tuple[str, object]] = []
    used: set = set()
    total_records = 0
    entries: List[Tuple[str, Component, bytes]] = []

    for c in comps:
        recs = _component_records(c, st, fonts)
        total_records += len(recs)
        entries.append((_safe_storage_name(c.name, used), c, b"".join(recs)))

    header.append(("HEADER", HEADER_MAGIC))
    header.append(("Weight", total_records))
    header.append(("MinorVersion", 9))
    header.append(("UniqueID", _uid()))
    header += fonts.header_props()
    header += [("UseMBCS", "T"), ("IsBOC", "T"), ("SheetStyle", 9),
               ("BorderOn", "T"), ("SheetNumberSpaceSize", 12),
               ("AreaColor", 16317695), ("SnapGridOn", "T"),
               ("SnapGridSize", 10), ("VisibleGridOn", "T"),
               ("VisibleGridSize", 10), ("CustomX", 18000),
               ("CustomY", 18000), ("UseCustomSheet", "T"),
               ("ReferenceZonesOn", "T"), ("Display_Unit", 0)]
    header.append(("CompCount", len(entries)))
    for i, (_nm, c, _data) in enumerate(entries):
        header.append((f"LibRef{i}", c.name))
        if c.description:
            header.append((f"CompDescr{i}", c.description))
        header.append((f"PartCount{i}", max(1, c.symbol.part_count) + 1))

    tree["FileHeader"] = _ascii_record(header)
    tree["Storage"] = _ascii_record([("HEADER", "Icon storage")])
    for nm, _c, data in entries:
        tree[nm] = {"Data": data}

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    write_cfb(path, tree)
    return path
