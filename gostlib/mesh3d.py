"""
Настоящая триангуляция STEP — для просмотра модели телом.

Свой разбор STEP (step3d) годится для габаритов и грубого силуэта, но
рисовать по нему тело нельзя: цилиндры и скругления там описаны кривыми,
а не многоугольниками, и картинка рассыпается. Поэтому, если в окружении
есть OpenCASCADE-конвертер, STEP переводится в честную треугольную сетку.

Нужны два пакета:

    pip install cascadio trimesh

cascadio — маленькая обёртка над OCCT, переводит STEP в glTF; trimesh
читает результат. Оба ставятся колёсами, без сборки. Если их нет,
функция вернёт None, и просмотр останется приблизительным — работать это
не мешает.

Результат кешируется рядом с моделью: конвертация занимает от десятых
долей секунды до нескольких секунд, и повторять её при каждом клике по
компоненту незачем.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

Point = Tuple[float, float, float]
Tri = Tuple[Point, Point, Point]

# glTF хранит метры, у нас всё в миллиметрах
M_TO_MM = 1000.0

# больше и не надо: на экране разницы нет, а перерисовка должна быть быстрой
MAX_TRIS = 5000
_MEM_CACHE = {}                 # повторный выбор строки не читает модель снова
_MEM_CACHE_LIMIT = 12


@dataclass
class Mesh:
    tris: List[Tri] = field(default_factory=list)
    colors: List[Tuple[float, float, float]] = field(default_factory=list)
    error: str = ""
    source: str = ""          # чем построено

    @property
    def ok(self) -> bool:
        return bool(self.tris)

    def bbox(self):
        if not self.tris:
            return None
        xs = [v[0] for t in self.tris for v in t]
        ys = [v[1] for t in self.tris for v in t]
        zs = [v[2] for t in self.tris for v in t]
        return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def available() -> bool:
    try:
        import cascadio  # noqa: F401
        import trimesh   # noqa: F401
        return True
    except Exception:
        return False


def preview_source(path: str) -> str:
    """Предпочесть исходную лёгкую сетку рядом с тяжёлым STEP из OBJ."""
    if not path:
        return path
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".step", ".stp"):
        return path
    base = os.path.splitext(path)[0]
    for suffix in (".obj", ".stl"):
        candidate = base + suffix
        if os.path.isfile(candidate):
            return candidate
    return path


HINT = ("Точный просмотр 3D выключен: нет пакетов cascadio и trimesh.\n"
        "Поставьте их один раз командой в папке программы:\n"
        "    gostlib.bat setup3d\n"
        "Она ставит пакеты именно в .venv программы, а не в системный "
        "Python.")


def _cache_path(step_path: str, cache_dir: str, tol: float) -> str:
    try:
        st = os.stat(step_path)
        key = f"{os.path.abspath(step_path)}|{st.st_mtime_ns}|{st.st_size}|{tol}"
    except OSError:
        key = os.path.abspath(step_path) + f"|{tol}"
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(cache_dir, h + ".glb")


def load(step_path: str, cache_dir: str = "", tol: float = 0.05
         ) -> Mesh:
    """
    Треугольная сетка модели в миллиметрах.

    tol -- линейный допуск триангуляции в миллиметрах: чем меньше, тем
    больше треугольников и точнее скругления.
    """
    m = Mesh()
    if not step_path or not os.path.isfile(step_path):
        m.error = "файл модели не найден"
        return m
    requested = step_path
    step_path = preview_source(step_path)
    try:
        st = os.stat(step_path)
        memory_key = (os.path.abspath(step_path), st.st_mtime_ns, st.st_size,
                      MAX_TRIS)
    except OSError:
        memory_key = None
    if memory_key in _MEM_CACHE:
        return _MEM_CACHE[memory_key]
    ext = os.path.splitext(step_path)[1].lower()
    try:
        import trimesh
    except Exception:
        m.error = HINT
        return m

    def build(t: float):
        """Сцена и её части при данном допуске триангуляции."""
        if ext in (".step", ".stp"):
            import cascadio
            cd = cache_dir or os.path.join(os.path.dirname(step_path),
                                           "_mesh_cache")
            os.makedirs(cd, exist_ok=True)
            glb = _cache_path(step_path, cd, t)
            if not os.path.isfile(glb):
                cascadio.step_to_glb(step_path, glb, tol_linear=t / M_TO_MM,
                                     tol_angular=0.5)
            return trimesh.load(glb), M_TO_MM, "cascadio"
        label = f"trimesh / {ext.lstrip('.').upper()}"
        if os.path.abspath(step_path) != os.path.abspath(requested):
            label += " (исходник EasyEDA)"
        return trimesh.load(step_path), 1.0, label

    # Слишком плотную сетку не прореживаем выборочно -- в теле появились бы
    # дыры. Вместо этого пересчитываем её с более грубым допуском: форма
    # остаётся целой, треугольников становится меньше.
    parts, scale = [], 1.0
    cur = tol
    for _attempt in range(3):
        try:
            scene, scale, m.source = build(cur)
        except Exception as e:
            m.error = f"не удалось построить сетку: {e}"
            return m
        parts = _parts(scene, trimesh)
        total = sum(len(f) for _v, f, _c in parts)
        if total <= MAX_TRIS or ext not in (".step", ".stp"):
            break
        cur *= 3.0

    if not parts:
        m.error = "в модели нет треугольников"
        return m

    parts = _simplify_parts(parts, MAX_TRIS)

    tris: List[Tri] = []
    colors: List[Tuple[float, float, float]] = []
    for verts, faces, col in parts:
        for f in faces:
            try:
                a, b, c = verts[f[0]], verts[f[1]], verts[f[2]]
            except Exception:
                continue
            tris.append(((float(a[0]) * scale, float(a[1]) * scale,
                          float(a[2]) * scale),
                         (float(b[0]) * scale, float(b[1]) * scale,
                          float(b[2]) * scale),
                         (float(c[0]) * scale, float(c[1]) * scale,
                          float(c[2]) * scale)))
            colors.append(col)
    m.tris = tris
    m.colors = colors
    if not tris:
        m.error = "сетка пустая"
    elif memory_key is not None:
        _MEM_CACHE[memory_key] = m
        while len(_MEM_CACHE) > _MEM_CACHE_LIMIT:
            _MEM_CACHE.pop(next(iter(_MEM_CACHE)))
    return m


def _simplify_parts(parts, limit: int):
    """
    Упростить плотную сетку слиянием близких вершин.

    В отличие от выборочного удаления треугольников поверхность остаётся
    замкнутой и в просмотре не появляются дыры. Это особенно важно для STEP,
    который GostLib раньше получил из исходного OBJ EasyEDA.
    """
    total = sum(len(f) for _v, f, _c in parts)
    if total <= limit:
        return parts
    try:
        import numpy as np
    except Exception:
        return parts
    out = []
    for verts, faces, color in parts:
        share = max(12, int(limit * len(faces) / max(1, total)))
        v2, f2 = _cluster_part(verts, faces, share, np)
        out.append((v2, f2, color))
    return out


def _cluster_part(verts, faces, limit: int, np):
    """Vertex clustering без внешней зависимости fast-simplification."""
    va = np.asarray(verts, dtype=float)
    fa = np.asarray(faces, dtype=np.int64)
    if len(fa) <= limit or len(va) < 4:
        return verts, faces
    if fa.ndim != 2 or fa.shape[1] < 3:
        return verts, faces
    fa = fa[:, :3]
    lo = va.min(axis=0)
    axis_span = va.max(axis=0) - lo
    span = float(np.max(axis_span))
    if span <= 1e-12:
        return verts, faces
    best = (va, fa)
    # От тонкой сетки к более грубой. Берём первый вариант, попавший в
    # бюджет: форма теряет минимум подробностей, достаточный для превью.
    for divisions in (160, 120, 90, 70, 52, 40, 30, 22, 16, 12, 9):
        # Отдельный шаг по каждой оси не даёт тонкому корпусу потерять
        # высоту и не смешивает верхнюю плоскость с боковыми фасками.
        cell = np.maximum(axis_span / divisions, span / 100000.0)
        corners = va[fa].reshape((-1, 3))
        tri = va[fa]
        normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        length = np.linalg.norm(normals, axis=1)
        normals = normals / np.maximum(length[:, None], 1e-12)
        # Нормаль входит в ключ: вершины верхней плоскости и боковой фаски
        # не усредняются друг с другом, поэтому острые рёбра не «плывут».
        spatial = np.rint((corners - lo) / cell).astype(np.int64)
        normal_key = np.rint(np.repeat(normals, 3, axis=0) * 8).astype(
            np.int64)
        keys = np.concatenate((spatial, normal_key), axis=1)
        _unique, inverse = np.unique(keys, axis=0, return_inverse=True)
        nf = inverse.reshape((-1, 3))
        keep = ((nf[:, 0] != nf[:, 1]) & (nf[:, 1] != nf[:, 2]) &
                (nf[:, 0] != nf[:, 2]))
        nf = nf[keep]
        if not len(nf):
            continue
        # Схлопывание может породить совпавшие грани. Оставляем первую и
        # тем самым сохраняем её исходное направление нормали.
        canonical = np.sort(nf, axis=1)
        _rows, first = np.unique(canonical, axis=0, return_index=True)
        nf = nf[np.sort(first)]
        count = np.bincount(inverse, minlength=len(_unique)).astype(float)
        nv = np.zeros((len(_unique), 3), dtype=float)
        for axis in range(3):
            nv[:, axis] = np.bincount(
                inverse, weights=corners[:, axis], minlength=len(_unique))
        nv /= np.maximum(count[:, None], 1.0)
        best = (nv, nf)
        if len(nf) <= limit:
            break
    return best


def _parts(scene, trimesh):
    """
    [(вершины, грани, цвет)] по частям сцены. Части не сливаем: у каждой
    свой материал, а слитая сетка выходит одноцветной -- корпус, выводы и
    метка становятся одинаково серыми.
    """
    out = []
    try:
        geoms = getattr(scene, "geometry", None)
        if geoms:
            for name, g in geoms.items():
                if getattr(g, "faces", None) is None or len(g.faces) == 0:
                    continue
                verts = g.vertices
                try:
                    tf = scene.graph.get(name)[0]
                    verts = trimesh.transformations.transform_points(verts, tf)
                except Exception:
                    pass
                out.append((verts, g.faces, _color_of(g)))
        elif getattr(scene, "faces", None) is not None \
                and len(scene.faces):
            out.append((scene.vertices, scene.faces, _color_of(scene)))
    except Exception:
        return out
    return out


DEFAULT_COLOR = (0.55, 0.55, 0.58)


def _color_of(geom) -> Tuple[float, float, float]:
    """Цвет части модели: из материала glTF, иначе серый по умолчанию."""
    for attr in ("baseColorFactor", "main_color", "diffuse"):
        try:
            v = getattr(getattr(geom, "visual", None), "material", None)
            v = getattr(v, attr, None)
            if v is None or len(v) < 3:
                continue
            vals = [float(x) for x in v[:3]]
            if max(vals) > 1.5:            # 0..255
                vals = [x / 255.0 for x in vals]
            if max(vals) <= 0.02:          # чёрный корпус всё же должен быть виден
                vals = [0.10, 0.10, 0.11]
            return (vals[0], vals[1], vals[2])
        except Exception:
            continue
    try:
        fc = getattr(getattr(geom, "visual", None), "face_colors", None)
        if fc is not None and len(fc):
            return (float(fc[0][0]) / 255.0, float(fc[0][1]) / 255.0,
                    float(fc[0][2]) / 255.0)
    except Exception:
        pass
    return DEFAULT_COLOR


def describe(m: Mesh) -> List[str]:
    if m.error:
        return [m.error]
    b = m.bbox()
    out = [f"3D-поверхность: {len(m.tris)} граней ({m.source})"]
    if b:
        out.append(f"Габарит модели: {b[3] - b[0]:.2f} x {b[4] - b[1]:.2f} x "
                   f"{b[5] - b[2]:.2f} мм")
    return out


def compare_with_footprint(m: Mesh, fp) -> List[str]:
    """Та же проверка габарита, но без повторного Python-разбора STEP."""
    b = m.bbox()
    if not b or fp is None:
        return []
    from . import fpinfo
    info = fpinfo.describe(fp)
    bw, bh = info.body_size
    pw, ph = info.pads_size
    ref_w, ref_h = bw or pw, bh or ph
    if not (ref_w and ref_h):
        return []
    mw, md = b[3] - b[0], b[4] - b[1]

    def diff(a, c):
        return abs(a - c) / max(a, c, 1e-6)

    dev = min(max(diff(mw, ref_w), diff(md, ref_h)),
              max(diff(mw, ref_h), diff(md, ref_w)))
    if dev <= 0.15:
        out = [f"Сходится с посадкой: модель {mw:.2f}x{md:.2f}, "
               f"посадка {ref_w:.2f}x{ref_h:.2f} мм"]
    elif dev <= 0.4:
        out = [f"ВНИМАНИЕ: модель {mw:.2f}x{md:.2f} мм заметно "
               f"отличается от посадки {ref_w:.2f}x{ref_h:.2f} мм"]
    else:
        out = [f"НЕ СХОДИТСЯ: модель {mw:.2f}x{md:.2f} мм, посадка "
               f"{ref_w:.2f}x{ref_h:.2f} мм — похоже, это модель "
               "другого корпуса"]
    if b[2] < -0.05:
        out.append(f"Модель уходит ниже платы на {abs(b[2]):.2f} мм — "
                   "возможно, нужен сдвиг по Z")
    return out
