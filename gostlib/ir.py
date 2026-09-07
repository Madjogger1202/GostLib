"""
Промежуточное представление компонента (IR).

Всё, что читается из KiCad / EasyEDA / Altium / SnapEDA / UltraLibrarian,
приводится к этим структурам. Дальше из них строится ГОСТ-символ,
SVG-превью, запись в каталог и задание для Altium.

Единицы:
  * символ (Symbol/SymPin/SymPrim) -- милы (mil), целые, сетка 100 mil,
    ось Y вверх (как в Altium).
  * посадочное место (Footprint/Pad/FpPrim) -- миллиметры, float.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

MM_PER_MIL = 0.0254


def mm2mil(v: float) -> float:
    return v / MM_PER_MIL


def mil2mm(v: float) -> float:
    return v * MM_PER_MIL


# --- электрический тип вывода -------------------------------------------------
# Значения совпадают с Altium TPinElectrical (порядок важен!)
ETYPES = [
    "input",           # 0 eElectricInput
    "io",              # 1 eElectricIO
    "output",          # 2 eElectricOutput
    "open_collector",  # 3 eElectricOpenCollector
    "passive",         # 4 eElectricPassive
    "hiz",             # 5 eElectricHiZ
    "open_emitter",    # 6 eElectricOpenEmitter
    "power",           # 7 eElectricPower
]

ETYPE_INDEX = {name: i for i, name in enumerate(ETYPES)}

# Как это называется в KiCad
KICAD_ETYPE = {
    "input": "input",
    "output": "output",
    "bidirectional": "io",
    "tri_state": "io",
    "passive": "passive",
    "free": "passive",
    "unspecified": "passive",
    "power_in": "power",
    "power_out": "power",
    "open_collector": "open_collector",
    "open_emitter": "open_emitter",
    "no_connect": "passive",
}

SIDES = ("L", "R", "T", "B")


@dataclass
class SymPin:
    """Вывод схемного символа."""
    number: str = ""            # обозначение вывода (номер контакта)
    name: str = ""              # имя вывода (VDD, GPIO0, ...)
    etype: str = "passive"      # см. ETYPES
    unit: int = 1               # номер части (Part) для многосекционных
    side: str = "L"             # L / R / T / B -- сторона размещения
    group: str = ""             # логическая группа (для зазоров в раскладке)
    order: int = 0              # порядок внутри группы
    manual: bool = False        # положение задано вручную в редакторе
    inverted: bool = False      # инверсия (кружок)
    clock: bool = False         # тактовый вход (треугольник)
    hidden: bool = False
    show_name: bool = True
    show_number: bool = True
    # заполняется генератором:
    x: int = 0                  # координата "внешнего" конца вывода, mil
    y: int = 0
    length: int = 200           # длина вывода, mil
    rotation: int = 0           # 0=вправо(вывод идёт вправо), 90=вверх, 180=влево, 270=вниз

    def key(self):
        return (self.unit, self.side, self.group, self.order, self.number)


@dataclass
class SymPrim:
    """Графический примитив символа. Координаты в милах."""
    kind: str = "line"          # line | rect | arc | poly | text | ellipse
    pts: List[List[float]] = field(default_factory=list)
    width: int = 1              # 1=Small, 2=Medium, 3=Large (Altium LineWidth)
    filled: bool = False
    text: str = ""
    size: int = 10              # размер шрифта (pt)
    rotation: int = 0
    justify: int = 1            # 0=L,1=C,2=R по горизонтали
    vjustify: int = 1           # 0=Bottom,1=Center,2=Top
    unit: int = 1
    radius: float = 0.0
    a1: float = 0.0             # начальный угол, град
    a2: float = 360.0           # конечный угол, град
    bold: bool = False
    # Цвет линии/фигуры, как его понимает Altium (целое BGR). -1 -- взять
    # общий цвет графики из настроек; иначе рисуем именно этим.
    color: int = -1


class PartGeom:
    """
    Живой доступ к геометрии одной секции символа.

    Редактор правит габариты «на месте» (тянет за край, добавляет линию в
    список), поэтому копия здесь не годится: нужны те самые объекты, что
    лежат в символе. Секция 1 -- это поля самого `Symbol`, остальные живут
    в `Symbol.parts`.
    """

    __slots__ = ("_sym", "_part", "_d")

    def __init__(self, sym: "Symbol", part: int = 1):
        self._sym = sym
        self._part = max(1, int(part or 1))
        if self._part == 1:
            self._d = None
        else:
            key = str(self._part)
            d = sym.parts.get(key)
            if d is None:
                d = {"body_w": 0, "body_h": 0, "field_l": 0, "field_r": 0,
                     "dividers": [], "user_lines": []}
                sym.parts[key] = d
            d.setdefault("dividers", [])
            d.setdefault("user_lines", [])
            self._d = d

    @property
    def part(self) -> int:
        return self._part

    def __getattr__(self, name):
        if name not in Symbol.GEOM:
            raise AttributeError(name)
        if self._d is None:
            return getattr(self._sym, name)
        return self._d[name]

    def __setattr__(self, name, value):
        if name in ("_sym", "_part", "_d"):
            object.__setattr__(self, name, value)
        elif name in Symbol.GEOM:
            if self._d is None:
                setattr(self._sym, name, value)
            else:
                self._d[name] = value
        else:
            raise AttributeError(name)


@dataclass
class Symbol:
    """Схемный символ (может быть многосекционным)."""
    part_count: int = 1
    pins: List[SymPin] = field(default_factory=list)
    prims: List[SymPrim] = field(default_factory=list)
    designator_pos: List[int] = field(default_factory=lambda: [0, 100])
    comment_pos: List[int] = field(default_factory=lambda: [0, -100])
    style: str = "gost"         # gost | native (как было в источнике)

    # --- ручная раскладка из редактора символа ---
    # Когда включена, генератор не переставляет выводы: он берёт их x/y/side
    # как есть и только перерисовывает корпус по сохранённым размерам.
    manual_layout: bool = False
    body_w: int = 0             # ширина корпуса, mil
    body_h: int = 0             # высота корпуса, mil (положительная)
    field_l: int = 0            # ширина левого дополнительного поля
    field_r: int = 0            # ширина правого дополнительного поля
    dividers: List[int] = field(default_factory=list)  # y горизонтальных линий
    # произвольные линии, нарисованные в редакторе: [x1, y1, x2, y2]
    user_lines: List[List[int]] = field(default_factory=list)

    # Секции 2..N: своя геометрия у каждой. Поля выше -- это секция 1,
    # поэтому старые компоненты читаются без переделки. Ключ -- номер
    # секции строкой (JSON не умеет целочисленные ключи).
    # Раньше габариты были общими на весь символ, и растянутый корпус
    # первой секции растягивал заодно вторую и третью.
    parts: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Поля геометрии, которые у каждой секции свои.
    GEOM = ("body_w", "body_h", "field_l", "field_r", "dividers",
            "user_lines")

    def geom(self, part: int = 1) -> Dict[str, Any]:
        """Геометрия секции. Секция 1 -- поля самого символа."""
        part = max(1, int(part or 1))
        if part == 1:
            return {k: getattr(self, k) for k in self.GEOM}
        d = self.parts.get(str(part)) or {}
        return {
            "body_w": int(d.get("body_w", 0) or 0),
            "body_h": int(d.get("body_h", 0) or 0),
            "field_l": int(d.get("field_l", 0) or 0),
            "field_r": int(d.get("field_r", 0) or 0),
            "dividers": list(d.get("dividers", []) or []),
            "user_lines": [list(l) for l in (d.get("user_lines") or [])],
        }

    def set_geom(self, part: int, **kw) -> None:
        """Записать геометрию секции. Неизвестные поля игнорируются."""
        part = max(1, int(part or 1))
        kw = {k: v for k, v in kw.items() if k in self.GEOM}
        if part == 1:
            for k, v in kw.items():
                setattr(self, k, v)
            return
        d = dict(self.parts.get(str(part)) or {})
        d.update(kw)
        self.parts[str(part)] = d

    def live(self, part: int = 1) -> "PartGeom":
        """Геометрия секции для правки на месте (редактор)."""
        return PartGeom(self, part)

    def drop_parts_above(self, n: int) -> None:
        """Убрать геометрию секций, которых больше нет."""
        for key in [k for k in self.parts if int(k or 1) > max(1, int(n))]:
            self.parts.pop(key, None)

    def bbox(self):
        xs, ys = [], []
        for p in self.pins:
            xs += [p.x, p.x]
            ys += [p.y, p.y]
        for pr in self.prims:
            for pt in pr.pts:
                xs.append(pt[0])
                ys.append(pt[1])
        if not xs:
            return (0, 0, 0, 0)
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class Pad:
    """Контактная площадка. Всё в мм."""
    number: str = ""
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0
    shape: str = "rect"         # rect | round | oval | roundrect | octagon
    rot: float = 0.0
    layer: str = "top"          # top | bottom | multi
    hole: float = 0.0           # диаметр отверстия, мм (0 = SMD)
    hole_len: float = 0.0       # длина паза, мм (0 = круглое отверстие)
    hole_rot: float = 0.0       # угол паза, град (0 = вдоль X)
    plated: bool = True
    corner_radius: float = 0.0  # % для roundrect
    mask_expansion: Optional[float] = None
    paste_expansion: Optional[float] = None


@dataclass
class FpPrim:
    """Графика посадочного места. Всё в мм."""
    kind: str = "line"          # line | arc | circle | rect | poly | text | region
    layer: str = "silk"         # silk | silk_bot | assy | courtyard | keepout | mech | copper_top
    pts: List[List[float]] = field(default_factory=list)
    width: float = 0.15
    radius: float = 0.0
    a1: float = 0.0
    a2: float = 360.0
    text: str = ""
    height: float = 1.0
    rot: float = 0.0
    filled: bool = False
    mirror: bool = False


@dataclass
class Model3D:
    path: str = ""              # путь к .step/.stp (абсолютный на момент сборки)
    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0             # standoff, мм
    rx: float = 0.0
    ry: float = 0.0
    rz: float = 0.0


def short_fp_name(name: str, limit: int = 31) -> str:
    """
    Имя хранилища в OLE не длиннее 31 знака, поэтому длинные имена посадок
    укорачиваются устойчиво (одинаковый вход -> одинаковый выход), чтобы
    ссылка из символа всегда совпадала с именем в библиотеке посадок.
    """
    import hashlib
    nm = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in (name or "FP"))
    if len(nm) <= limit:
        return nm
    h = hashlib.md5(nm.encode("utf-8")).hexdigest()[:5].upper()
    return nm[:limit - 6] + "_" + h


@dataclass
class Footprint:
    name: str = ""
    description: str = ""
    pads: List[Pad] = field(default_factory=list)
    prims: List[FpPrim] = field(default_factory=list)
    height: float = 0.0
    model: Optional[Model3D] = None
    # если посадка берётся готовой из чужой Altium-библиотеки:
    source_pcblib: str = ""
    source_name: str = ""

    def is_external(self) -> bool:
        return bool(self.source_pcblib)


@dataclass
class Component:
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""                       # LibReference в Altium
    description: str = ""
    designator: str = "U?"               # префикс позиционного обозначения
    ctype: str = "ic"                    # см. classify.CTYPES
    manufacturer: str = ""
    mpn: str = ""
    datasheet: str = ""
    value: str = ""
    source: str = ""                     # kicad / easyeda / altium / manual
    source_ref: str = ""                 # что именно было импортировано
    symbol: Symbol = field(default_factory=Symbol)
    footprints: List[Footprint] = field(default_factory=list)
    params: Dict[str, str] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    notes: str = ""
    created: str = ""
    # исходные (не ГОСТ) пины, если нужно пересобрать символ
    raw_pins: List[SymPin] = field(default_factory=list)
    # Личные настройки стиля этого компонента. Пустой словарь -- берём
    # общие. Класть сюда можно любое поле Style; сейчас осмысленны
    # show_pin_numbers и passive_scale. Общие настройки при этом не
    # трогаются: одна мелкая пассивка не должна ужимать всю библиотеку.
    style_over: Dict[str, Any] = field(default_factory=dict)
    # Откуда брать обозначение: "gost" -- рисуем сами по ЕСКД,
    # "native" -- как было в источнике (KiCad). Пассивки в KiCad уже
    # нарисованы правильно, а вот микросхемы там -- безликие коробки.
    symbol_source: str = "gost"
    native_prims: List[SymPrim] = field(default_factory=list)
    native_pins: List[SymPin] = field(default_factory=list)
    # Свои правила группировки выводов (см. gost/pingroups.py). Пусто --
    # берутся общие из настроек.
    pin_rules: str = ""

    # ---- сериализация ----
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Component":
        sym = d.get("symbol") or {}
        symbol = Symbol(
            part_count=sym.get("part_count", 1),
            pins=[SymPin(**p) for p in sym.get("pins", [])],
            prims=[SymPrim(**p) for p in sym.get("prims", [])],
            designator_pos=sym.get("designator_pos", [0, 100]),
            comment_pos=sym.get("comment_pos", [0, -100]),
            style=sym.get("style", "gost"),
            manual_layout=bool(sym.get("manual_layout", False)),
            body_w=int(sym.get("body_w", 0) or 0),
            body_h=int(sym.get("body_h", 0) or 0),
            field_l=int(sym.get("field_l", 0) or 0),
            field_r=int(sym.get("field_r", 0) or 0),
            dividers=list(sym.get("dividers", []) or []),
            user_lines=[list(l) for l in (sym.get("user_lines") or [])],
            parts={str(k): dict(v) for k, v in
                   (sym.get("parts") or {}).items()},
        )
        fps = []
        for f in d.get("footprints", []):
            m = f.get("model")
            fps.append(Footprint(
                name=f.get("name", ""),
                description=f.get("description", ""),
                pads=[Pad(**p) for p in f.get("pads", [])],
                prims=[FpPrim(**p) for p in f.get("prims", [])],
                height=f.get("height", 0.0),
                model=Model3D(**m) if m else None,
                source_pcblib=f.get("source_pcblib", ""),
                source_name=f.get("source_name", ""),
            ))
        c = Component(
            uid=d.get("uid") or uuid.uuid4().hex[:12],
            name=d.get("name", ""),
            description=d.get("description", ""),
            designator=d.get("designator", "U?"),
            ctype=d.get("ctype", "ic"),
            manufacturer=d.get("manufacturer", ""),
            mpn=d.get("mpn", ""),
            datasheet=d.get("datasheet", ""),
            value=d.get("value", ""),
            source=d.get("source", ""),
            source_ref=d.get("source_ref", ""),
            symbol=symbol,
            footprints=fps,
            params=dict(d.get("params", {})),
            tags=list(d.get("tags", [])),
            notes=d.get("notes", ""),
            style_over=dict(d.get("style_over", {}) or {}),
            symbol_source=d.get("symbol_source", "gost") or "gost",
            native_prims=[SymPrim(**p) for p in d.get("native_prims", [])],
            native_pins=[SymPin(**p) for p in d.get("native_pins", [])],
            pin_rules=d.get("pin_rules", "") or "",
            created=d.get("created", ""),
            raw_pins=[SymPin(**p) for p in d.get("raw_pins", [])],
        )
        return c

    @staticmethod
    def from_json(s: str) -> "Component":
        return Component.from_dict(json.loads(s))
