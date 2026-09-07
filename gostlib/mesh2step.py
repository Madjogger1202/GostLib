"""
Конвертация полигональной модели (OBJ) в фасетный STEP AP214.

Нужна для 3D-моделей из EasyEDA: они отдаются в OBJ, а Altium понимает
только STEP/Parasolid. Полученный STEP -- фасетный (каждый треугольник --
плоская грань), для визуализации и проверки габаритов этого достаточно.
"""
from __future__ import annotations

import datetime
import os
from typing import Dict, List, Optional, Sequence, Tuple

MAX_TRIANGLES = 60000


def read_obj(path: str) -> Tuple[List[Tuple[float, float, float]],
                                 List[Tuple[int, int, int]]]:
    verts: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                if len(p) >= 4:
                    verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                idx = []
                for tok in line.split()[1:]:
                    s = tok.split("/")[0]
                    if not s:
                        continue
                    i = int(s)
                    idx.append(i - 1 if i > 0 else len(verts) + i)
                for k in range(1, len(idx) - 1):
                    faces.append((idx[0], idx[k], idx[k + 1]))
    return verts, faces


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _norm(v):
    import math
    n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    if n < 1e-12:
        return None
    return (v[0] / n, v[1] / n, v[2] / n)


def _perp(n):
    """Любой единичный вектор, перпендикулярный n."""
    ref = (0.0, 0.0, 1.0) if abs(n[2]) < 0.9 else (1.0, 0.0, 0.0)
    return _norm(_cross(ref, n)) or (1.0, 0.0, 0.0)


# С этого числа треугольников сетка прореживается: столько подробностей
# корпусу на плате не нужно, а STEP растёт линейно и очень быстро.
SIMPLIFY_OVER = 12000


def simplify(verts, faces, cell: float):
    """
    Огрубление сеткой: вершины прилипают к узлам решётки с шагом cell,
    совпавшие сливаются, выродившиеся треугольники выбрасываются.

    Способ грубый, зато без сторонних библиотек и без сюрпризов: габарит
    сохраняется с точностью до шага, а корпус на плате выглядит так же.
    """
    if cell <= 0:
        return verts, faces
    remap = {}
    new_verts = []
    idx = []
    for v in verts:
        key = (round(v[0] / cell), round(v[1] / cell), round(v[2] / cell))
        j = remap.get(key)
        if j is None:
            j = len(new_verts)
            remap[key] = j
            new_verts.append((key[0] * cell, key[1] * cell, key[2] * cell))
        idx.append(j)
    seen = set()
    new_faces = []
    for (a, b, c) in faces:
        if a >= len(idx) or b >= len(idx) or c >= len(idx):
            continue
        A, B, C = idx[a], idx[b], idx[c]
        if A == B or B == C or A == C:
            continue                      # треугольник схлопнулся
        key = tuple(sorted((A, B, C)))
        if key in seen:
            continue                      # дубликат после слияния
        seen.add(key)
        new_faces.append((A, B, C))
    return new_verts, new_faces


def _auto_cell(verts) -> float:
    """Шаг решётки: примерно 1/400 наибольшего габарита, но не мельче 10 мкм."""
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    return max(span / 400.0, 0.01)


def obj_to_step(obj_path: str, step_path: str, name: str = "MODEL",
                scale: float = 1.0, log=None) -> str:
    verts, faces = read_obj(obj_path)
    if not verts or not faces:
        raise ValueError("OBJ не содержит геометрии")
    if len(faces) > SIMPLIFY_OVER:
        n0 = len(faces)
        verts, faces = simplify(verts, faces, _auto_cell(verts))
        if log:
            log(f"  сетка огрублена: {n0} -> {len(faces)} треугольников "
                f"(иначе STEP вышел бы на десятки мегабайт)")
    if len(faces) > MAX_TRIANGLES:
        raise ValueError(f"слишком много треугольников ({len(faces)}), "
                         f"предел {MAX_TRIANGLES}")
    if scale != 1.0:
        verts = [(v[0] * scale, v[1] * scale, v[2] * scale) for v in verts]

    out: List[str] = []
    nid = [0]

    def add(txt: str) -> int:
        nid[0] += 1
        out.append(f"#{nid[0]}={txt};")
        return nid[0]

    def pt(v) -> int:
        return add("CARTESIAN_POINT('',(%.6f,%.6f,%.6f))" % v)

    def dr(v) -> int:
        return add("DIRECTION('',(%.6f,%.6f,%.6f))" % v)

    # --- контекст ---
    dz = dr((0.0, 0.0, 1.0))
    dx = dr((1.0, 0.0, 0.0))
    p0 = pt((0.0, 0.0, 0.0))
    ax = add(f"AXIS2_PLACEMENT_3D('',#{p0},#{dz},#{dx})")
    uang = add("(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.))")
    usol = add("(NAMED_UNIT(*)SOLID_ANGLE_UNIT()SI_UNIT($,.STERADIAN.))")
    ulen = add("(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.))")
    unc = add(f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-006),#{ulen},"
              f"'distance_accuracy_value','')")
    ctx = add(f"(GEOMETRIC_REPRESENTATION_CONTEXT(3)"
              f"GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{unc}))"
              f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{ulen},#{uang},#{usol}))"
              f"REPRESENTATION_CONTEXT('',''))")

    # --- точки ---
    vids: Dict[int, int] = {}
    for i, v in enumerate(verts):
        vids[i] = pt(v)
    vpids: Dict[int, int] = {}

    def vpt(i: int) -> int:
        if i not in vpids:
            vpids[i] = add(f"VERTEX_POINT('',#{vids[i]})")
        return vpids[i]

    # Каждое ребро принадлежит двум треугольникам -- храним его один раз.
    # Без этого файл раздувается: у CH375B из EasyEDA получалось 69 МБ на
    # 2 МБ исходного OBJ, и любой разбор такой модели занимал минуты.
    dirs: Dict[Tuple[int, int, int], int] = {}

    def dr_cached(v) -> int:
        key = (round(v[0], 6), round(v[1], 6), round(v[2], 6))
        if key not in dirs:
            dirs[key] = dr(v)
        return dirs[key]

    edge_ids: Dict[Tuple[int, int], int] = {}

    def edge(i: int, j: int) -> Optional[int]:
        """EDGE_CURVE между вершинами i и j, общий для соседних граней."""
        key = (i, j) if i < j else (j, i)
        ec = edge_ids.get(key)
        if ec is None:
            d = _norm(_sub(verts[key[1]], verts[key[0]]))
            if d is None:
                return None
            vec = add(f"VECTOR('',#{dr_cached(d)},1.)")
            ln = add(f"LINE('',#{vids[key[0]]},#{vec})")
            ec = add(f"EDGE_CURVE('',#{vpt(key[0])},#{vpt(key[1])},#{ln},.T.)")
            edge_ids[key] = ec
        return ec

    face_ids: List[int] = []
    for (a, b, c) in faces:
        if a >= len(verts) or b >= len(verts) or c >= len(verts):
            continue
        va, vb, vc = verts[a], verts[b], verts[c]
        n = _norm(_cross(_sub(vb, va), _sub(vc, va)))
        if n is None:
            continue                     # вырожденный треугольник
        refd = _perp(n)
        edges = []
        for (i, j) in ((a, b), (b, c), (c, a)):
            ec = edge(i, j)
            if ec is None:
                edges = []
                break
            # направление ребра общее, поэтому у половины граней оно
            # обходится в обратную сторону
            fwd = ".T." if i < j else ".F."
            edges.append(add(f"ORIENTED_EDGE('',*,*,#{ec},{fwd})"))
        if len(edges) != 3:
            continue
        loop = add("EDGE_LOOP('',(#%d,#%d,#%d))" % tuple(edges))
        bound = add(f"FACE_OUTER_BOUND('',#{loop},.T.)")
        op = vids[a]
        nd = dr_cached(n)
        rd = dr_cached(refd)
        pax = add(f"AXIS2_PLACEMENT_3D('',#{op},#{nd},#{rd})")
        pl = add(f"PLANE('',#{pax})")
        face_ids.append(add(f"ADVANCED_FACE('',(#{bound}),#{pl},.T.)"))

    if not face_ids:
        raise ValueError("не удалось построить ни одной грани")

    shell = add("OPEN_SHELL('',(%s))" % ",".join(f"#{i}" for i in face_ids))
    ssm = add(f"SHELL_BASED_SURFACE_MODEL('',(#{shell}))")

    safe = "".join(ch for ch in (name or "MODEL")
                   if ch.isalnum() or ch in "_-. ") or "MODEL"
    appctx = add("APPLICATION_CONTEXT('automotive design')")
    add(f"APPLICATION_PROTOCOL_DEFINITION('international standard',"
        f"'automotive_design',2000,#{appctx})")
    prodctx = add(f"PRODUCT_CONTEXT('',#{appctx},'mechanical')")
    prod = add(f"PRODUCT('{safe}','{safe}','',(#{prodctx}))")
    pdf = add(f"PRODUCT_DEFINITION_FORMATION('','',#{prod})")
    pdc = add(f"PRODUCT_DEFINITION_CONTEXT('part definition',#{appctx},'design')")
    pd = add(f"PRODUCT_DEFINITION('','',#{pdf},#{pdc})")
    pds = add(f"PRODUCT_DEFINITION_SHAPE('','',#{pd})")
    srep = add(f"SHAPE_REPRESENTATION('{safe}',(#{ax},#{ssm}),#{ctx})")
    add(f"SHAPE_DEFINITION_REPRESENTATION(#{pds},#{srep})")

    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    header = (
        "ISO-10303-21;\n"
        "HEADER;\n"
        f"FILE_DESCRIPTION((''),'2;1');\n"
        f"FILE_NAME('{os.path.basename(step_path)}','{now}',(''),(''),"
        f"'GostLib','GostLib','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\n"
        "ENDSEC;\nDATA;\n")
    with open(step_path, "w", encoding="ascii", errors="replace") as f:
        f.write(header)
        f.write("\n".join(out))
        f.write("\nENDSEC;\nEND-ISO-10303-21;\n")
    return step_path
