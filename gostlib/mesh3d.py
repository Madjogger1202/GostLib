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
MAX_TRIS = 24000


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
        return trimesh.load(step_path), 1.0, "trimesh"

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
    return m


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
    out = [f"Сетка: {len(m.tris)} треугольников ({m.source})"]
    if b:
        out.append(f"Габарит сетки: {b[3] - b[0]:.2f} x {b[4] - b[1]:.2f} x "
                   f"{b[5] - b[2]:.2f} мм")
    return out
