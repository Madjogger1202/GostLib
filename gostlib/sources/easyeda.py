"""
Импорт из EasyEDA / LCSC (JLCPCB).

По коду вида C2040 тянется компонент из открытого API EasyEDA Standard:
символ (список выводов и родная графика -- по ГОСТ рисуется заново,
но родное обозначение сохраняется и его можно выбрать),
посадочное место (переносится геометрически) и, по возможности, 3D-модель.

3D берётся родным STEP производителя: он цветной и точный. Если его нет
или он лежит в чужой системе координат -- EasyEDA отдаёт ту же модель в
OBJ, и она переводится в STEP модулем mesh2step.
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
from ..ir import (Component, Footprint, FpPrim, Model3D, Pad, SymPin,
                  SymPrim, Symbol)

API_COMPONENT = ("https://easyeda.com/api/products/{code}/components"
                 "?version=6.4.19.5")
API_3D_OBJ = "https://modules.easyeda.com/3dmodel/{uuid}"
# Родной STEP той же модели. EasyEDA хранит его рядом с OBJ под тем же uuid
# (этим путём пользуется и easyeda2kicad). Это настоящая твердотельная
# модель производителя: точная геометрия и свои цвета, без перегонки
# сетки и без потерь на огрублении.
API_3D_STEP = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}"

# Насколько габариты STEP и OBJ могут расходиться, чтобы считать их одной
# и той же моделью в одной системе координат.
STEP_FIT_REL = 0.08         # доля габарита
STEP_FIT_ABS = 0.05         # мм -- для крошечных корпусов
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


# Слои, которые Altium строит сам из площадок. Всё, что источник рисует
# на них вручную, -- дубликат, и в Altium он превращается в мусор поверх
# нормальных окон маски и пасты.
DERIVED_LAYERS = ("paste", "mask")


class EasyEdaError(RuntimeError):
    pass


# Сколько ждать. Таймаут сокета не ограничивает загрузку целиком: сервер
# может отдавать по байту и формально не молчать, поэтому есть ещё и общий
# срок. Именно так выглядело «зависло на "Запрашиваю в EasyEDA"».
CONNECT_TIMEOUT = 12.0      # с, на один сокет
TOTAL_DEADLINE = 90.0       # с, на всю загрузку файла
MAX_BYTES = 80 * 1024 * 1024
RETRIES = 3


def _get(url: str, timeout: float = CONNECT_TIMEOUT,
         deadline: float = TOTAL_DEADLINE, retries: int = RETRIES,
         log=None) -> bytes:
    """
    Забрать ответ, не подвесив программу насовсем.

    Три отличия от простого `urlopen().read()`:
    читаем кусками и следим за ОБЩИМ временем, а не только за паузами в
    сокете; обрываем то, что переросло разумный размер; повторяем попытку
    при обрыве -- API EasyEDA время от времени рвёт соединение, и один
    повтор обычно решает дело.
    """
    import time as _time
    log = log or (lambda *_: None)
    last = ""
    for attempt in range(1, max(1, retries) + 1):
        t0 = _time.time()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                chunks = []
                got = 0
                while True:
                    if _time.time() - t0 > deadline:
                        raise TimeoutError(
                            f"не уложились в {deadline:.0f} с "
                            f"(получено {got // 1024} КБ)")
                    part = r.read(256 * 1024)
                    if not part:
                        break
                    chunks.append(part)
                    got += len(part)
                    if got > MAX_BYTES:
                        raise EasyEdaError(
                            f"ответ больше {MAX_BYTES // 1024 // 1024} МБ — "
                            f"это не похоже на компонент")
                data = b"".join(chunks)
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    data = gzip.decompress(data)
                if attempt > 1:
                    log(f"  получено со {attempt}-й попытки")
                return data
        except urllib.error.HTTPError as e:
            # 404 повторять бессмысленно
            raise EasyEdaError(f"HTTP {e.code} при запросе {url}") from e
        except EasyEdaError:
            raise
        except Exception as e:
            last = str(e)
            if attempt < retries:
                log(f"  попытка {attempt} не удалась ({last}), повторяю…")
                _time.sleep(1.5 * attempt)
    raise EasyEdaError(f"Нет ответа от EasyEDA: {last}")


def _cache_file(cache_dir: str, name: str) -> str:
    if not cache_dir:
        return ""
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(cache_dir, name)


def fetch_raw(code: str, cache_dir: str = "", log=None) -> dict:
    """
    Описание компонента. С кешем: повторный импорт того же кода не ходит в
    сеть вовсе -- а повторяют его постоянно, когда подбирают тип или
    правят посадку.
    """
    log = log or (lambda *_: None)
    code = (code or "").strip().upper()
    if not re.fullmatch(r"C\d+", code):
        raise EasyEdaError("Код LCSC должен выглядеть как C2040")
    cached = _cache_file(cache_dir, f"{code}.json")
    if cached and os.path.isfile(cached):
        try:
            with open(cached, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("result"):
                log(f"  {code}: взято из кеша")
                return data["result"]
        except Exception:
            pass          # битый кеш -- просто идём в сеть
    raw = _get(API_COMPONENT.format(code=code), log=log)
    data = json.loads(raw.decode("utf-8"))
    if not data.get("success") or not data.get("result"):
        raise EasyEdaError(f"{code}: компонент не найден в EasyEDA")
    if cached:
        try:
            with open(cached, "wb") as f:
                f.write(raw)
        except OSError:
            pass
    return data["result"]


# ----------------------------------------------------------------- символ ----

# Текстовые поля, которые EasyEDA рисует графикой, а мы ставим своими
# средствами: обозначение и подпись приезжают в Altium штатными Designator
# и Comment, и дубликат графикой только мешает -- его можно двигать, а
# копию нельзя.
_SKIP_TEXT = ("comment", "name", "prefix", "part", "spice")

# Длина вывода по умолчанию, в единицах EasyEDA (1 единица = 10 mil).
DEF_PIN = 10.0

_SIDE = {180: "L", 0: "R", 90: "T", 270: "B"}


def _lw(v) -> int:
    """Толщина линии EasyEDA (пиксели) в шкалу Altium: 1=Small..3=Large."""
    return max(1, min(3, int(round(_f(v, 1.0))) or 1))


def _is_filled(v) -> bool:
    return str(v or "").strip().lower() not in ("", "none", "transparent")


def _sym_graphics(shapes: List[str], unit: int,
                  ox: float, oy: float) -> List[SymPrim]:
    """
    Графика символа EasyEDA как есть: прямоугольники, окружности, ломаные,
    заливки, дуги и подписи.

    Нужна там, где обозначение из источника само по себе верное и
    перерисовывать его по ГОСТ нечем: у двунаправленного супрессора
    (ESD5471X) это два встречных треугольника, и обычный диод вместо них --
    враньё в схеме.

    Ось Y у EasyEDA смотрит вниз, у нас вверх: Y меняет знак, а вместе с
    ним и углы дуг. Начало координат символа лежит в ``dataStr.head``.
    """
    out: List[SymPrim] = []

    def X(v):
        return int(round((_f(v) - ox) * UNIT_MIL))

    def Y(v):
        return int(round(-(_f(v) - oy) * UNIT_MIL))

    def D(v):
        return _f(v) * UNIT_MIL

    def path_pts(path: str) -> List[List[float]]:
        pts = [[X(p[0]), Y(p[1])] for p in _svg_path_points(path)]
        # EasyEDA охотно пишет одну и ту же точку подряд -- в Altium это
        # линия нулевой длины, то есть точка посреди обозначения.
        return [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]

    for sh in shapes:
        f = str(sh).split("~")
        kind = f[0]
        try:
            if kind == "R" and len(f) > 10:
                x, y = _f(f[1]), _f(f[2])
                w, h = _f(f[5]), _f(f[6])
                out.append(SymPrim(kind="rect", unit=unit,
                                   pts=[[X(x), Y(y)], [X(x + w), Y(y + h)]],
                                   width=_lw(f[8]), filled=_is_filled(f[10])))
            elif kind == "E" and len(f) > 8:
                out.append(SymPrim(kind="ellipse", unit=unit,
                                   pts=[[X(f[1]), Y(f[2])]],
                                   radius=(D(f[3]) + D(f[4])) / 2.0,
                                   width=_lw(f[6]), filled=_is_filled(f[8])))
            elif kind in ("PL", "PG", "PT") and len(f) > 5:
                pts = path_pts(f[1])
                if len(pts) < 2:
                    continue
                closed = kind in ("PG", "PT") or "Z" in f[1].upper()
                if closed and pts[0] != pts[-1]:
                    pts.append(list(pts[0]))
                out.append(SymPrim(kind="poly", unit=unit, pts=pts,
                                   width=_lw(f[3]), filled=_is_filled(f[5])))
            elif kind == "A" and len(f) > 5:
                arc = _arc_from_path(f[1])
                if arc:
                    cx, cy, r, a1, a2 = arc
                    out.append(SymPrim(kind="arc", unit=unit,
                                       pts=[[X(cx), Y(cy)]], radius=D(r),
                                       a1=-a2, a2=-a1, width=_lw(f[3])))
                    continue
                pts = path_pts(f[1])
                if len(pts) >= 2:
                    out.append(SymPrim(kind="poly", unit=unit, pts=pts,
                                       width=_lw(f[3])))
            elif kind == "T" and len(f) > 12:
                if str(f[11]).strip().lower() in _SKIP_TEXT:
                    continue
                if len(f) > 13 and str(f[13]).strip() == "0":
                    continue
                body = str(f[12]).strip()
                if not body:
                    continue
                just = {"start": 0, "middle": 1, "end": 2}.get(
                    str(f[10]).strip().lower(), 0)
                size = int(round(_f(str(f[7]).lower().replace("pt", ""), 7.0)))
                out.append(SymPrim(kind="text", unit=unit, text=body,
                                   pts=[[X(f[2]), Y(f[3])]],
                                   size=max(6, size),
                                   rotation=int(round(_f(f[4]))) % 360,
                                   justify=just, vjustify=0))
        except (IndexError, ValueError):
            continue
    return out


def _place_pin(p: SymPin, head: List[str], segs: List[str],
               ox: float, oy: float) -> None:
    """
    Запомнить, где вывод стоит в EasyEDA.

    В EasyEDA координата вывода -- его СВОБОДНЫЙ конец, а линия из второго
    сегмента ("M x y h 10") идёт от него к корпусу. У нас наоборот:
    координата -- конец у корпуса, поворот смотрит наружу. Угол EasyEDA
    отсчитывает против часовой стрелки на экране, поэтому после смены знака
    Y он совпадает с нашим один в один.

    Геометрия кладётся в служебное поле, а не в сам вывод: по ГОСТ выводы
    раскладываются заново, и место из источника там только мешало бы.
    """
    if len(head) < 7:
        return
    ex, ey = _f(head[4]), _f(head[5])
    rot = int(round(_f(head[6]))) % 360
    bx = by = None
    if len(segs) > 2:
        # Линия вывода нарисована в любую сторону: у одного компонента она
        # идёт от свободного конца к корпусу ("M 150 20 h 10"), у соседнего
        # наоборот ("M 20 20 h 10" при выводе в точке 30). Значит, конец у
        # корпуса -- это тот конец линии, который НЕ совпадает с точкой
        # вывода, а не просто последний.
        pts = _svg_path_points(segs[2].split("~")[0])
        for cand in (pts[-1:] + pts[:1]) if pts else []:
            if abs(cand[0] - ex) > 1e-6 or abs(cand[1] - ey) > 1e-6:
                bx, by = cand
                break
    if bx is None:
        a = math.radians(rot + 180)          # от свободного конца к корпусу
        bx, by = ex + DEF_PIN * math.cos(a), ey - DEF_PIN * math.sin(a)
    ln = int(round(math.hypot(bx - ex, by - ey) * UNIT_MIL)) or 100
    p._ee = (int(round((bx - ox) * UNIT_MIL)),      # type: ignore[attr-defined]
             int(round(-(by - oy) * UNIT_MIL)), rot, ln)


def _parse_pins(shapes: List[str], unit: int = 1,
                ox: float = 0.0, oy: float = 0.0) -> List[SymPin]:
    """Разобрать выводы одной секции EasyEDA."""
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
        p = SymPin(number=str(number).strip(), name=str(name).strip(),
                   etype=etype, unit=max(1, int(unit or 1)),
                   inverted=inverted, clock=clock)
        _place_pin(p, head, segs, ox, oy)
        pins.append(p)
    return pins


def native_pins(pins: List[SymPin]) -> List[SymPin]:
    """Копии выводов, поставленные туда, где они стоят в EasyEDA."""
    out: List[SymPin] = []
    for p in pins:
        q = SymPin(**{k: v for k, v in p.__dict__.items()
                      if k in SymPin.__dataclass_fields__})
        geo = getattr(p, "_ee", None)
        if geo:
            q.x, q.y, q.rotation, q.length = geo
            q.side = _SIDE.get(int(round(q.rotation / 90.0)) * 90 % 360, "L")
        out.append(q)
    return out


def _origin(ds: dict) -> Tuple[float, float]:
    head = ds.get("head") or {}
    return _f(head.get("x"), 0.0), _f(head.get("y"), 0.0)


def _subpart_number(subpart: dict, fallback: int) -> int:
    """Номер секции из метаданных EasyEDA, с безопасным запасным вариантом."""
    ds = subpart.get("dataStr") or {}
    para = (ds.get("head") or {}).get("c_para") or {}
    candidates = [para.get("subpart_no")]
    title = str(subpart.get("title") or "")
    m = re.search(r"\.(\d+)\s*$", title)
    if m:
        candidates.append(m.group(1))
    for value in candidates:
        try:
            number = int(value)
            if number > 0:
                return number
        except (TypeError, ValueError):
            pass
    return max(1, int(fallback or 1))


def _parse_symbol(result: dict) -> Tuple[List[SymPin], int, List[SymPrim]]:
    """
    Разобрать символ, включая многосекционные компоненты EasyEDA.

    У обычного компонента выводы лежат в ``result.dataStr.shape``. У
    многосекционного родительский ``shape`` пуст, а настоящие символы лежат
    в ``result.subparts``. Раньше такой компонент импортировался с нулём
    выводов.

    Кроме выводов забираем графику: она нужна тем компонентам, чьё
    обозначение из источника мы не перерисовываем.
    """
    subparts = [p for p in (result.get("subparts") or [])
                if isinstance(p, dict)]
    if not subparts:
        ds = result.get("dataStr") or {}
        ox, oy = _origin(ds)
        shapes = ds.get("shape") or []
        return (_parse_pins(shapes, 1, ox, oy), 1,
                _sym_graphics(shapes, 1, ox, oy))

    pins: List[SymPin] = []
    prims: List[SymPrim] = []
    units: List[int] = []
    for fallback, subpart in enumerate(subparts, 1):
        unit = _subpart_number(subpart, fallback)
        units.append(unit)
        ds = subpart.get("dataStr") or {}
        # у каждой секции своё начало координат -- в Altium это отдельная
        # часть, и считать их от общего начала значило бы развести части
        # по листу на сотни милов
        ox, oy = _origin(ds)
        shapes = ds.get("shape") or []
        pins.extend(_parse_pins(shapes, unit, ox, oy))
        prims.extend(_sym_graphics(shapes, unit, ox, oy))
    return pins, max(units or [1]), prims


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


def _slot_angle(hole_points: str):
    """
    Угол овального отверстия по полю holePoints ('x1 y1 x2 y2').

    Возвращает градусы в НАШЕЙ системе координат (ось Y вверх) или None,
    если поля нет. Точки абсолютные, поэтому поворот самой площадки в них
    уже учтён -- складывать его сверху не нужно.
    """
    vals = [v for v in (hole_points or "").replace(",", " ").split() if v]
    if len(vals) < 4:
        return None
    try:
        x1, y1, x2, y2 = (float(vals[0]), float(vals[1]),
                          float(vals[2]), float(vals[3]))
    except ValueError:
        return None
    dx, dy = x2 - x1, -(y2 - y1)        # у EasyEDA ось Y вниз
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return None
    return math.degrees(math.atan2(dy, dx)) % 180.0


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
            # Угол овального отверстия. Раньше он не читался вовсе, и все
            # прорези ложились вдоль X: у разъёма USB-C крепёжные пазы
            # выходили повёрнутыми на 90 градусов и в превью, и на плате.
            # Направление есть прямо в данных -- поле holePoints -- двумя
            # абсолютными точками; считаем угол по ним, а не гадаем по
            # габаритам площадки.
            hole_rot = 0.0
            if hole_len > 0:
                hole_rot = _slot_angle(parts[14] if len(parts) > 14 else "")
                if hole_rot is None:
                    # Резерв: прорезь идёт вдоль длинной стороны площадки.
                    hole_rot = (90.0 if h > w else 0.0) + rot
            fp.pads.append(Pad(number=str(num), x=x, y=y, w=w, h=h, shape=shp,
                               rot=rot, layer=lay, hole=hole_r * 2,
                               hole_len=hole_len, hole_rot=hole_rot % 180.0,
                               plated=plated))
        elif kind == "TRACK" and len(parts) >= 5:
            width = MD(parts[1])
            lay = EE_LAYER.get(int(_f(parts[2], 3)), "mech")
            if lay in DERIVED_LAYERS:
                continue
            coords = [_f(v) for v in parts[4].split(" ") if v.strip()]
            pts = [[(coords[i] - ox) * UNIT_MM, -(coords[i + 1] - oy) * UNIT_MM]
                   for i in range(0, len(coords) - 1, 2)]
            for i in range(len(pts) - 1):
                fp.prims.append(FpPrim(kind="line", layer=lay,
                                       pts=[pts[i], pts[i + 1]], width=width))
        elif kind == "CIRCLE" and len(parts) >= 6:
            lay = EE_LAYER.get(int(_f(parts[5], 3)), "mech")
            if lay in DERIVED_LAYERS:
                continue
            fp.prims.append(FpPrim(
                kind="circle", layer=lay,
                pts=[[MX(parts[1]), MY(parts[2])]], radius=MD(parts[3]),
                width=MD(parts[4])))
        elif kind == "ARC" and len(parts) >= 5:
            if EE_LAYER.get(int(_f(parts[2], 3)), "mech") in DERIVED_LAYERS:
                continue
            a = _arc_from_path(parts[4])
            if a:
                cx, cy, r, a1, a2 = a
                fp.prims.append(FpPrim(
                    kind="arc", layer=EE_LAYER.get(int(_f(parts[2], 3)), "mech"),
                    pts=[[(cx - ox) * UNIT_MM, -(cy - oy) * UNIT_MM]],
                    radius=r * UNIT_MM, a1=-a2, a2=-a1, width=MD(parts[1])))
        elif kind == "SOLIDREGION" and len(parts) >= 4:
            lay = EE_LAYER.get(int(_f(parts[1], 3)), "mech")
            if lay in DERIVED_LAYERS:
                # Окна маски и пасты Altium делает САМ из площадок, а у
                # EasyEDA на каждую площадку лежит своя заливка. Мы её
                # рисовали контуром -- и вокруг каждого шарика BGA
                # появлялась квадратная обводка на слое пасты, которую и
                # выделить-то нечем. Такие фигуры не берём вовсе.
                continue
            pts = [[(p[0] - ox) * UNIT_MM, -(p[1] - oy) * UNIT_MM]
                   for p in _svg_path_points(parts[3])]
            if len(pts) >= 3:
                fp.prims.append(FpPrim(
                    kind="poly", layer=lay,
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
          log=None, cache_dir: str = "", budget: int = 0,
          prefer_step: bool = True) -> Component:
    import time as _time
    log = log or (lambda *_: None)
    t0 = _time.time()
    res = fetch_raw(code, cache_dir=cache_dir, log=log)
    log(f"  описание получено за {_time.time() - t0:.1f} с")
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

    pins, part_count, prims = _parse_symbol(res)
    c.raw_pins = pins
    c.symbol = Symbol(part_count=part_count, pins=list(pins))
    c.params["PinCount"] = str(len(pins))
    # Родное обозначение EasyEDA кладём рядом с ГОСТ-символом. Для диодов,
    # супрессоров, транзисторов и прочей дискретной мелочи оно и есть
    # рабочее: у двунаправленного ESD в источнике нарисованы два встречных
    # треугольника, а по одному лишь списку выводов его от обычного диода
    # не отличить.
    c.native_prims = prims
    c.native_pins = native_pins(pins)

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
    c.symbol_source = ("native" if prims and any(getattr(p, "_ee", None)
                                                 for p in pins)
                       and classify.keeps_native_symbol(c.ctype) else "gost")

    if want_3d and model_uuid and out_dir:
        t1 = _time.time()
        try:
            fp0 = c.footprints[0] if c.footprints else None
            tht = bool(fp0 and any(float(getattr(p, "hole", 0) or 0) > 0
                                   for p in fp0.pads))
            model = download_model(model_uuid, out_dir,
                                   fp0.name if fp0 else c.name, log=log,
                                   cache_dir=cache_dir, budget=budget,
                                   tht=tht, prefer_step=prefer_step)
            if model and fp0 is not None:
                fp0.model = model
            log(f"  3D готова за {_time.time() - t1:.1f} с")
        except Exception as e:      # 3D не критично
            log(f"3D-модель не получена: {e}")
    log(f"  {code}: всего {_time.time() - t0:.1f} с")
    return c


def _fetch_cached(url: str, cached: str, log, what: str) -> bytes:
    """Файл из кеша, а если его нет -- из сети с сохранением в кеш."""
    import time as _time
    if cached and os.path.isfile(cached):
        try:
            with open(cached, "rb") as f:
                data = f.read()
            if data:
                log(f"  3D ({what}): взята из кеша")
                return data
        except OSError:
            pass
    t0 = _time.time()
    data = _get(url, log=log)
    log(f"  3D ({what}): скачано {len(data) // 1024} КБ за "
        f"{_time.time() - t0:.1f} с")
    if cached and data:
        try:
            with open(cached, "wb") as f:
                f.write(data)
        except OSError:
            pass
    return data


def _get_obj(uuid: str, cache_dir: str, log) -> str:
    """OBJ модели по uuid (кешируется: он не меняется, а весит порой десятки МБ)."""
    raw = _fetch_cached(API_3D_OBJ.format(uuid=uuid),
                        _cache_file(cache_dir, f"3d_{uuid}.obj"), log,
                        "OBJ").decode("utf-8", "replace")
    if "v " not in raw:
        raise EasyEdaError("EasyEDA вернул не OBJ")
    return raw


def is_step(data: bytes) -> bool:
    """Похоже ли это на файл STEP (ISO 10303-21), а не на страницу ошибки."""
    head = (data or b"")[:512].lstrip()
    return head.startswith(b"ISO-10303-21") and b"DATA;" in (data or b"")[:1 << 20]


def _obj_bbox(raw: str):
    """Габарит OBJ по вершинам: (x0, y0, z0, x1, y1, z1) в мм."""
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for line in raw.splitlines():
        if not line.startswith("v "):
            continue
        p = line.split()
        try:
            v = (float(p[1]), float(p[2]), float(p[3]))
        except (IndexError, ValueError):
            continue
        for i in range(3):
            lo[i] = min(lo[i], v[i])
            hi[i] = max(hi[i], v[i])
    if lo[0] == float("inf"):
        return None
    return (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])


_VTX_RE = re.compile(
    rb"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*"
    rb"([-+.\dEe]+)\s*,\s*([-+.\dEe]+)\s*,\s*([-+.\dEe]+)")
_VREF_RE = re.compile(rb"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)")


def _step_bbox(path: str, work_dir: str = ""):
    """
    Габарит STEP в мм.

    Точнее всего -- по настоящей сетке (там учтены размещения деталей
    сборки: выводы у многих моделей лежат отдельными деталями со своим
    смещением). Если пакетов для сетки нет -- по вершинам тел; это верно
    для моделей из одного тела, а для сборки габарит может выйти врасплох,
    и тогда сверка с OBJ честно не сойдётся.
    """
    try:
        from .. import mesh3d
        m = mesh3d.load(path, cache_dir=os.path.join(
            work_dir or os.path.dirname(path), "_mesh_cache"), tol=0.2)
        if m.ok:
            return m.bbox()
    except Exception:
        pass
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    pts = {int(m.group(1)): m for m in _VTX_RE.finditer(data)}
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for ref in _VREF_RE.finditer(data):
        m = pts.get(int(ref.group(1)))
        if not m:
            continue
        try:
            v = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
        except ValueError:
            continue
        for i in range(3):
            lo[i] = min(lo[i], v[i])
            hi[i] = max(hi[i], v[i])
    if lo[0] == float("inf"):
        return None
    return (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])


def _fits(a, b) -> bool:
    """Совпадают ли габариты двух моделей по каждой оси."""
    for i in range(3):
        sa, sb = a[i + 3] - a[i], b[i + 3] - b[i]
        if abs(sa - sb) > max(STEP_FIT_REL * max(sa, sb), STEP_FIT_ABS):
            return False
    return True


def place_step(step_box, obj_box, tht: bool = False):
    """
    Смещение, которое ставит родной STEP туда же, где стоит OBJ.

    OBJ у EasyEDA уже в координатах посадочного места (по центру), а STEP
    остаётся в системе координат, в которой его рисовал производитель, --
    часто со сдвигом. Сверяем габариты: совпали -- это одна и та же модель,
    и разница центров и есть нужное смещение. Не совпали (другие единицы,
    модель повёрнута осью вверх) -- возвращаем None, и берётся OBJ: лучше
    знакомая сетка на своём месте, чем точная модель поперёк платы.

    По Z у SMD-корпуса дно садится на плату (Z = 0): у OBJ бывает ноль по
    центру корпуса. У выводного Z берётся как у OBJ -- ножки ниже платы.
    """
    if not step_box:
        return None
    if obj_box is not None and not _fits(step_box, obj_box):
        return None
    if obj_box is None:
        cx = cy = 0.0
        z0 = 0.0
    else:
        cx = (obj_box[0] + obj_box[3]) / 2.0
        cy = (obj_box[1] + obj_box[4]) / 2.0
        z0 = obj_box[2]
    dx = cx - (step_box[0] + step_box[3]) / 2.0
    dy = cy - (step_box[1] + step_box[4]) / 2.0
    dz = (z0 if tht else 0.0) - step_box[2]
    return (dx, dy, dz)


def download_model(uuid: str, out_dir: str, base: str, log=None,
                   cache_dir: str = "", budget: int = 0, tht: bool = False,
                   prefer_step: bool = True) -> Optional[Model3D]:
    """
    3D-модель компонента: родной STEP производителя, а если не вышло --
    STEP, собранный из OBJ.

    Родной STEP цветной и точный, но лежит в своей системе координат,
    поэтому ставится по OBJ: смещение кладётся в Model3D, сам файл не
    правится.
    """
    log = log or (lambda *_: None)
    os.makedirs(out_dir, exist_ok=True)
    raw_obj = ""
    try:
        raw_obj = _get_obj(uuid, cache_dir, log)
    except Exception as e:
        log(f"  3D: OBJ не получен ({e})")

    if prefer_step:
        try:
            data = _fetch_cached(API_3D_STEP.format(uuid=uuid),
                                 _cache_file(cache_dir, f"3d_{uuid}.step"),
                                 log, "STEP")
        except Exception as e:
            data = b""
            log(f"  3D: родного STEP нет ({e})")
        if data and not is_step(data):
            log("  3D: вместо STEP пришло что-то другое — беру OBJ")
            data = b""
        if data:
            step_path = os.path.join(out_dir, f"{base}.step")
            with open(step_path, "wb") as f:
                f.write(data)
            # Сетка с тем же именем рядом перехватила бы просмотр
            # (mesh3d.preview_source), и в окне была бы старая серая
            # модель вместо цветной.
            for ext in (".obj", ".stl"):
                stale = os.path.join(out_dir, base + ext)
                if os.path.isfile(stale):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
            obj_box = _obj_bbox(raw_obj) if raw_obj else None
            step_box = _step_bbox(step_path, out_dir)
            off = place_step(step_box, obj_box, tht=tht)
            if off is not None:
                dx, dy, dz = off
                log(f"3D: родной STEP производителя (цветной) -> "
                    f"{os.path.basename(step_path)}"
                    + (f", смещение {dx:+.2f}/{dy:+.2f}/{dz:+.2f} мм"
                       if max(abs(dx), abs(dy), abs(dz)) > 1e-3 else ""))
                return Model3D(path=step_path, dx=dx, dy=dy, dz=dz)
            if step_box and raw_obj:
                sb = step_box
                ob = obj_box
                log("  3D: родной STEP в другой системе координат (габарит "
                    f"{sb[3] - sb[0]:.2f}x{sb[4] - sb[1]:.2f}x{sb[5] - sb[2]:.2f}"
                    f" против {ob[3] - ob[0]:.2f}x{ob[4] - ob[1]:.2f}x"
                    f"{ob[5] - ob[2]:.2f} мм у OBJ) — беру OBJ, он стоит "
                    "на своём месте")
            else:
                log("  3D: габарит родного STEP не определился — беру OBJ")

    if not raw_obj:
        raise EasyEdaError("ни STEP, ни OBJ получить не удалось")
    path = download_3d(uuid, out_dir, base, log=log, cache_dir=cache_dir,
                       budget=budget, raw=raw_obj)
    return Model3D(path=path) if path else None


def download_3d(uuid: str, out_dir: str, base: str, log=None,
                cache_dir: str = "", budget: int = 0, raw: str = "") -> str:
    """
    Скачать OBJ и перевести его в STEP, который понимает Altium.

    Запасной путь: основной -- родной STEP (download_model). Скачанный OBJ
    кладётся в кеш по uuid: он не меняется, а весит иногда десятки
    мегабайт, и повторный импорт того же компонента сеть не трогает.
    """
    import time as _time
    log = log or (lambda *_: None)
    os.makedirs(out_dir, exist_ok=True)
    if not raw:
        raw = _get_obj(uuid, cache_dir, log)

    obj_path = os.path.join(out_dir, f"{base}.obj")
    with open(obj_path, "w", encoding="utf-8") as f:
        f.write(raw)

    # Крупная сетка -- это минуты в конвертере. Предупреждаем заранее,
    # чтобы «программа зависла» читалось как «идёт долгая работа».
    faces = raw.count("\nf ")
    if faces > 200000:
        log(f"  3D: в модели {faces} граней — перевод в STEP займёт время")
    try:
        from ..mesh2step import obj_to_step
        step_path = os.path.join(out_dir, f"{base}.step")
        t1 = _time.time()
        obj_to_step(obj_path, step_path, name=base, log=log,
                    budget=budget)
        log(f"3D: OBJ -> STEP за {_time.time() - t1:.1f} с "
            f"({faces} граней) -> {os.path.basename(step_path)}")
        return step_path
    except Exception as e:
        log(f"3D: конвертация OBJ->STEP не удалась ({e}), оставлен {obj_path}")
        return ""
