"""
Быстрый разбор STEP: габариты и облако точек для превью.

Полноценное ядро геометрии сюда не затащить, да и не нужно. Задача другая:
показать, что модель на месте, что она того же размера, что и посадочное
место, и что она не перевёрнута. Для этого хватает координат вершин
(CARTESIAN_POINT) — из них получаются габаритный ящик и узнаваемый силуэт.

Точки берутся как есть, без учёта вложенных систем координат: в моделях
корпусов, которые выкладывают производители и KiCad, геометрия лежит в
одной системе, а расхождение всё равно видно на глаз по превью.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

Point = Tuple[float, float, float]

# Координаты в STEP лежат в CARTESIAN_POINT, но так же записаны и начала
# вспомогательных систем координат (AXIS2_PLACEMENT_3D). Если считать
# габарит по всем точкам подряд, корпус 4x4 превращается в 7x7. Поэтому
# берём только те точки, на которые ссылается VERTEX_POINT -- это и есть
# вершины реальной геометрии.
_POINT_RE = re.compile(
    rb"#\s*(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)",
    re.I)
_VERTEX_RE = re.compile(
    rb"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#\s*(\d+)", re.I)
_NUM_RE = re.compile(rb"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
# Рёбра: EDGE_CURVE ссылается на два VERTEX_POINT. По ним рисуется каркас --
# он куда понятнее облака точек и сразу показывает форму корпуса.
_EDGE_RE = re.compile(
    rb"EDGE_CURVE\s*\(\s*'[^']*'\s*,\s*#\s*(\d+)\s*,\s*#\s*(\d+)",
    re.I)
_VDEF_RE = re.compile(
    rb"#\s*(\d+)\s*=\s*VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#\s*(\d+)",
    re.I)
# Грани: ADVANCED_FACE -> FACE_OUTER_BOUND -> EDGE_LOOP -> ORIENTED_EDGE ->
# EDGE_CURVE -> две вершины. Из плоских граней собираются настоящие
# многоугольники, и модель рисуется телом, а не проволокой.
_ECURVE_RE = re.compile(
    rb"#\s*(\d+)\s*=\s*EDGE_CURVE\s*\(\s*'[^']*'\s*,\s*#\s*(\d+)\s*,"
    rb"\s*#\s*(\d+)", re.I)
_OEDGE_RE = re.compile(
    rb"#\s*(\d+)\s*=\s*ORIENTED_EDGE\s*\([^)]*?#\s*(\d+)\s*,\s*\.([TF])\.",
    re.I)
_ELOOP_RE = re.compile(
    rb"#\s*(\d+)\s*=\s*EDGE_LOOP\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)", re.I)
_BOUND_RE = re.compile(
    rb"#\s*(\d+)\s*=\s*FACE_(?:OUTER_)?BOUND\s*\(\s*'[^']*'\s*,\s*#\s*(\d+)",
    re.I)
_FACE_RE = re.compile(
    rb"#\s*\d+\s*=\s*ADVANCED_FACE\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)\s*,"
    rb"\s*#\s*(\d+)", re.I)
_PLANE_RE = re.compile(rb"#\s*(\d+)\s*=\s*PLANE\s*\(", re.I)
# цвет тела: COLOUR_RGB('', r, g, b) с долями от нуля до единицы
_COLOUR_RE = re.compile(
    rb"COLOUR_RGB\s*\(\s*'[^']*'\s*,\s*([^)]*)\)", re.I)
_REF_RE = re.compile(rb"#\s*(\d+)")

# больше и не нужно: превью рисуется по подвыборке
MAX_POINTS = 60000


@dataclass
class Model:
    path: str = ""
    points: List[Point] = field(default_factory=list)
    total: int = 0            # сколько точек было в файле
    truncated: bool = False
    error: str = ""
    units: str = "мм"
    edges: List[Tuple[Point, Point]] = field(default_factory=list)
    faces: List[List[Point]] = field(default_factory=list)
    color: Optional[Tuple[float, float, float]] = None

    @property
    def ok(self) -> bool:
        return bool(self.points) and not self.error

    def bbox(self) -> Optional[Tuple[float, float, float, float, float, float]]:
        if not self.points:
            return None
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        zs = [p[2] for p in self.points]
        return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    def size(self) -> Tuple[float, float, float]:
        b = self.bbox()
        if not b:
            return (0.0, 0.0, 0.0)
        return (b[3] - b[0], b[4] - b[1], b[5] - b[2])

    def z_top(self) -> float:
        """
        Верхняя точка вершин.

        Честно возвращаем максимум и не гадаем: в STEP часть тел бывает
        описана в своих системах координат, и их вершины лежат «этажом
        выше». Отличить такой дубль от настоящего корпуса по одним
        координатам нельзя -- у QFN между верхом выводов и верхом корпуса
        такой же разрыв, как у дубля. Поэтому Z подаём как диапазон и
        честно помечаем, когда он выглядит завышенным.
        """
        b = self.bbox()
        return b[5] if b else 0.0

    def height(self) -> float:
        b = self.bbox()
        return (b[5] - b[2]) if b else 0.0

    def z_suspicious(self) -> bool:
        """Размах по Z слишком велик для корпуса такого размера в плане."""
        w, d, h = self.size()
        plan = max(w, d)
        return bool(plan) and h > max(1.0, 0.5 * plan)

    def lines(self) -> List[str]:
        if self.error:
            return [f"3D-модель: {self.error}"]
        w, d, h = self.size()
        b = self.bbox()
        out = [f"Модель: {os.path.basename(self.path)}",
               f"В плане: {w:.2f} x {d:.2f} мм",
               f"По Z: от {b[2]:.2f} до {b[5]:.2f} мм (высота {h:.2f})"]
        if self.z_suspicious():
            out.append("  Z похож на завышенный: в файле, скорее всего, есть "
                       "тела в своих системах координат. На X и Y это не "
                       "влияет, сверка с посадкой ниже — по ним.")
        if b and b[2] < -0.05:
            out.append(f"Низ модели ниже платы на {abs(b[2]):.2f} мм")
        out.append(f"Вершин: {self.total}"
                   + (" (для превью взята часть)" if self.truncated else ""))
        return out


def parse(path: str, max_points: int = MAX_POINTS) -> Model:
    """Прочитать STEP и достать координаты вершин."""
    m = Model(path=path)
    if not path:
        m.error = "не задана"
        return m
    if not os.path.isfile(path):
        m.error = f"файл не найден: {path}"
        return m
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        m.error = f"не читается: {e}"
        return m
    if b"CARTESIAN_POINT" not in raw.upper() and b"cartesian_point" not in raw:
        m.error = "это не похоже на STEP (нет ни одной вершины)"
        return m

    coords = {}
    for mt in _POINT_RE.finditer(raw):
        nums = _NUM_RE.findall(mt.group(2))
        if len(nums) < 3:
            continue
        try:
            coords[mt.group(1)] = (float(nums[0]), float(nums[1]),
                                   float(nums[2]))
        except ValueError:
            continue

    # id вершины -> координаты
    vmap = {}
    for mt in _VDEF_RE.finditer(raw):
        pt = coords.get(mt.group(2))
        if pt is not None:
            vmap[mt.group(1)] = pt
    edges = []
    for mt in _EDGE_RE.finditer(raw):
        a = vmap.get(mt.group(1))
        b = vmap.get(mt.group(2))
        if a is not None and b is not None and a != b:
            edges.append((a, b))
    m.edges = edges
    m.faces = _faces(raw, vmap)
    m.color = _colour(raw)

    vertex_ids = {mt.group(1) for mt in _VERTEX_RE.finditer(raw)}
    picked = [coords[i] for i in vertex_ids if i in coords]
    if len(picked) < 4:
        # фасетный STEP (например, конвертированный из OBJ) вершин через
        # VERTEX_POINT не описывает -- там годятся все точки
        picked = list(coords.values())
        m.units = m.units

    m.total = len(picked)
    if len(picked) > max_points:
        step = len(picked) / float(max_points)
        picked = [picked[int(i * step)] for i in range(max_points)]
        m.truncated = True
    m.points = picked
    if not picked:
        m.error = "вершины не разобрались"
    return m


def _colour(raw: bytes) -> Optional[Tuple[float, float, float]]:
    """Цвет тела из STEP: берём самый часто встречающийся COLOUR_RGB."""
    counts = {}
    for mt in _COLOUR_RE.finditer(raw):
        nums = _NUM_RE.findall(mt.group(1))
        if len(nums) < 3:
            continue
        try:
            key = (round(float(nums[0]), 3), round(float(nums[1]), 3),
                   round(float(nums[2]), 3))
        except ValueError:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _faces(raw: bytes, vmap) -> List[List[Point]]:
    """
    Грани как многоугольники в порядке обхода контура.

    Цилиндрические и прочие кривые поверхности тоже берём: контур у них
    описан теми же рёбрами, и получается низкополигональная, но узнаваемая
    форма. Пропускать их -- значит показывать корпус дырявым.
    """
    ecurve = {}
    for mt in _ECURVE_RE.finditer(raw):
        a, b = vmap.get(mt.group(2)), vmap.get(mt.group(3))
        if a is not None and b is not None:
            ecurve[mt.group(1)] = (a, b)
    if not ecurve:
        return []
    oedge = {}
    for mt in _OEDGE_RE.finditer(raw):
        e = ecurve.get(mt.group(2))
        if e is None:
            continue
        oedge[mt.group(1)] = e if mt.group(3) == b"T" else (e[1], e[0])
    loops = {}
    for mt in _ELOOP_RE.finditer(raw):
        loops[mt.group(1)] = [r for r in _REF_RE.findall(mt.group(2))]
    bounds = {}
    for mt in _BOUND_RE.finditer(raw):
        bounds[mt.group(1)] = mt.group(2)
    out: List[List[Point]] = []
    for mt in _FACE_RE.finditer(raw):
        refs = _REF_RE.findall(mt.group(1))
        if not refs:
            continue
        loop = loops.get(bounds.get(refs[0], b""), None)
        if not loop:
            continue
        poly: List[Point] = []
        for oid in loop:
            seg = oedge.get(oid)
            if seg is None:
                poly = []
                break
            if not poly:
                poly.append(seg[0])
            poly.append(seg[1])
        if len(poly) >= 4:
            out.append(poly[:-1])
    return out


def compare_with_footprint(model: Model, fp) -> List[str]:
    """
    Сверить габариты модели с посадочным местом. Именно здесь ловятся
    перепутанные модели: корпус 7x7 на посадке 4x4 виден сразу.
    """
    from . import fpinfo
    out: List[str] = []
    if not model.ok:
        return out
    info = fpinfo.describe(fp)
    bw, bh = info.body_size
    pw, ph = info.pads_size
    mw, md, mh = model.size()
    ref_w = bw or pw
    ref_h = bh or ph
    if not (ref_w and ref_h):
        return out

    # модель может быть повёрнута на 90°, поэтому сравниваем и так, и так
    def diff(a, b):
        return abs(a - b) / max(a, b, 1e-6)

    straight = max(diff(mw, ref_w), diff(md, ref_h))
    turned = max(diff(mw, ref_h), diff(md, ref_w))
    dev = min(straight, turned)
    if dev <= 0.15:
        out.append(f"Сходится с посадкой: модель {mw:.2f}x{md:.2f}, "
                   f"посадка {ref_w:.2f}x{ref_h:.2f} мм")
    elif dev <= 0.4:
        out.append(f"ВНИМАНИЕ: модель {mw:.2f}x{md:.2f} мм заметно "
                   f"отличается от посадки {ref_w:.2f}x{ref_h:.2f} мм")
    else:
        out.append(f"НЕ СХОДИТСЯ: модель {mw:.2f}x{md:.2f} мм, "
                   f"посадка {ref_w:.2f}x{ref_h:.2f} мм — похоже, "
                   f"это модель другого корпуса")
    b = model.bbox()
    if b and b[2] < -0.05:
        out.append(f"Модель уходит ниже платы на {abs(b[2]):.2f} мм — "
                   f"возможно, нужен сдвиг по Z")
    return out


# ------------------------------------------------------------------ превью --
def _subsample(points: Sequence[Point], limit: int) -> List[Point]:
    if len(points) <= limit:
        return list(points)
    step = len(points) / float(limit)
    return [points[int(i * step)] for i in range(limit)]


def _fp_outline(fp) -> Tuple[List[Tuple[float, float, float, float]],
                             List[Tuple[float, float, float, float]]]:
    """Отрезки контура посадки и прямоугольники площадок (мм)."""
    segs = []
    pads = []
    if fp is None:
        return segs, pads
    for pr in getattr(fp, "prims", []):
        if pr.layer not in ("courtyard", "silk", "assy"):
            continue
        pts = pr.pts or []
        if pr.kind == "rect" and len(pts) >= 2:
            (x1, y1), (x2, y2) = pts[0], pts[1]
            ring = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
            segs += [(a[0], a[1], b[0], b[1]) for a, b in zip(ring, ring[1:])]
        elif len(pts) >= 2:
            segs += [(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:])]
    for p in getattr(fp, "pads", []):
        pads.append((p.x - p.w / 2, p.y - p.h / 2, p.w, p.h))
    return segs, pads


def preview_svg(model: Model, fp=None, width: int = 620,
                height: int = 320) -> str:
    """
    Два вида: сверху (X-Y) с наложенным контуром посадки и сбоку (X-Z)
    с линией платы. Сразу видно и размер, и высоту, и не ушла ли модель
    под плату.
    """
    if not model.ok:
        return ""
    b = model.bbox()
    minx, miny, minz, maxx, maxy, maxz = b
    segs, pads = _fp_outline(fp)
    if segs or pads:
        xs = [s[0] for s in segs] + [s[2] for s in segs] + \
             [p[0] for p in pads] + [p[0] + p[2] for p in pads]
        ys = [s[1] for s in segs] + [s[3] for s in segs] + \
             [p[1] for p in pads] + [p[1] + p[3] for p in pads]
        if xs:
            minx, maxx = min(minx, min(xs)), max(maxx, max(xs))
            miny, maxy = min(miny, min(ys)), max(maxy, max(ys))

    pad = 16
    half = (width - 3 * pad) / 2.0
    spanx = max(maxx - minx, 1e-3)
    spany = max(maxy - miny, 1e-3)
    spanz = max(maxz - minz, 1e-3)

    maxz = model.z_top()
    spanz = max(maxz - minz, 1e-3)
    k_top = min(half / spanx, (height - 2 * pad - 18) / spany)
    k_side = min(half / spanx, (height - 2 * pad - 18) / max(spanz, spanx / 6))
    k = min(k_top, k_side)

    ox1 = pad + (half - spanx * k) / 2.0
    oy = pad + 18 + (height - 2 * pad - 18 - spany * k) / 2.0
    ox2 = 2 * pad + half + (half - spanx * k) / 2.0
    oy2 = height - pad

    def TX(x, o):
        return o + (x - minx) * k

    def TY(y):
        return oy + (maxy - y) * k

    def SZ(z):
        return oy2 - (z - min(minz, 0.0)) * k

    pts = _subsample([p for p in model.points if p[2] <= maxz + 1e-6], 5000)
    o = ['<svg xmlns="http://www.w3.org/2000/svg" '
         f'viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
         f'<rect width="{width}" height="{height}" fill="#101018"/>']

    o.append(f'<text x="{pad}" y="{pad + 4}" fill="#8fa3c8" '
             f'font-size="11" font-family="sans-serif">сверху, '
             f'{maxx - minx:.2f} x {maxy - miny:.2f} мм</text>')
    o.append(f'<text x="{2 * pad + half:.0f}" y="{pad + 4}" fill="#8fa3c8" '
             f'font-size="11" font-family="sans-serif">сбоку, высота '
             f'{maxz - minz:.2f} мм</text>')

    # площадки посадки -- фоном под моделью
    for (px, py, pw, ph) in pads:
        o.append(f'<rect x="{TX(px, ox1):.1f}" y="{TY(py + ph):.1f}" '
                 f'width="{pw * k:.1f}" height="{ph * k:.1f}" '
                 f'fill="#3a5a3a" opacity="0.85"/>')
    for (x1, y1, x2, y2) in segs:
        o.append(f'<line x1="{TX(x1, ox1):.1f}" y1="{TY(y1):.1f}" '
                 f'x2="{TX(x2, ox1):.1f}" y2="{TY(y2):.1f}" '
                 f'stroke="#c8a04a" stroke-width="1"/>')

    # облако вершин модели
    top = []
    side = []
    for (x, y, z) in pts:
        top.append(f'{TX(x, ox1):.1f},{TY(y):.1f}')
        side.append(f'{TX(x, ox2):.1f},{SZ(z):.1f}')
    o.append('<g fill="#7fc7ff" opacity="0.55">')
    for p in top:
        cx, cy = p.split(",")
        o.append(f'<circle cx="{cx}" cy="{cy}" r="0.7"/>')
    for p in side:
        cx, cy = p.split(",")
        o.append(f'<circle cx="{cx}" cy="{cy}" r="0.7"/>')
    o.append('</g>')

    # линия платы на виде сбоку
    y0 = SZ(0.0)
    o.append(f'<line x1="{ox2 - 6:.1f}" y1="{y0:.1f}" '
             f'x2="{ox2 + spanx * k + 6:.1f}" y2="{y0:.1f}" '
             f'stroke="#6fbf73" stroke-width="1.5"/>')
    o.append(f'<text x="{ox2 - 4:.0f}" y="{y0 + 12:.0f}" fill="#6fbf73" '
             f'font-size="10" font-family="sans-serif">плата</text>')
    o.append('</svg>')
    return "\n".join(o)
