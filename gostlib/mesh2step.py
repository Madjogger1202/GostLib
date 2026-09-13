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

# Цвет корпуса, если в OBJ материалов не было вовсе. Белый Altium
# показывает по умолчанию, и модель выглядит как кусок пенопласта --
# поэтому берём тёмно-серый, обычный цвет пластика микросхемы.
DEFAULT_BODY_RGB = (0.20, 0.20, 0.22)

# Метка в заголовке STEP: перечень исходных цветов в том же порядке, в
# каком в файле идут записи COLOUR_RGB. Нужна, чтобы выбранный вручную
# цвет можно было потом снять и вернуть родную раскраску модели, не
# перегоняя её заново из OBJ.
COLOR_TAG = "GostLib colors "

# Сколько треугольников оставлять по умолчанию.
#
# Это не про размер файла на диске, а про время сборки в Altium. Замер на
# живом проекте: посадка FBGA-96 с моделью на ~58 тысяч треугольников
# (STEP 23 МБ) строилась 337 СЕКУНД, при том что соседняя посадка на 355
# площадок и 4636 линий -- 8,5 секунды. Всё это время Altium разбирал
# фасетный B-Rep. Импорт растёт быстрее, чем линейно, поэтому режем
# заранее: на корпусе микросхемы шесть тысяч треугольников от шестидесяти
# на глаз не отличаются, а сборка становится быстрее на порядок.
FACE_BUDGET = 6000


def read_obj_full(path: str):
    """
    Разбор OBJ вместе с материалами.

    EasyEDA кладёт материалы прямо в тело файла (`newmtl` ... `endmtl`)
    и переключает их строками `usemtl`. Цвета там осмысленные: корпус
    микросхемы чёрный, шарики BGA почти белые. Раньше мы это выбрасывали,
    и в Altium любая перегнанная модель выходила белой -- терялась
    единственная информация о том, как деталь выглядит на самом деле.

    Возвращает (вершины, треугольники, материал каждого треугольника,
    словарь материал -> (r, g, b)). Прозрачность (`d`) намеренно
    игнорируется: EasyEDA пишет туда `d 0.0` у всех материалов подряд,
    и понимать это буквально -- значит получить невидимую модель.
    """
    verts: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    fmat: List[str] = []
    mats: Dict[str, Tuple[float, float, float]] = {}
    cur = ""            # текущий материал (пустая строка -- без материала)
    defining = ""       # имя материала, который сейчас описывается
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
                    fmat.append(cur)
            elif line.startswith("newmtl "):
                defining = line.split(None, 1)[1].strip()
            elif line.startswith("endmtl"):
                defining = ""
            elif line.startswith("usemtl "):
                cur = line.split(None, 1)[1].strip()
            elif line.startswith("Kd ") and defining:
                p = line.split()
                if len(p) >= 4:
                    try:
                        mats[defining] = (max(0.0, min(1.0, float(p[1]))),
                                          max(0.0, min(1.0, float(p[2]))),
                                          max(0.0, min(1.0, float(p[3]))))
                    except ValueError:
                        pass
    return verts, faces, fmat, mats


def read_obj(path: str) -> Tuple[List[Tuple[float, float, float]],
                                 List[Tuple[int, int, int]]]:
    verts, faces, _fmat, _mats = read_obj_full(path)
    return verts, faces


def parse_hex(color: str):
    """'#RRGGBB' -> (r, g, b) в долях единицы. Пустое/кривое -> None."""
    s = (color or "").strip()
    if len(s) != 7 or s[0] != "#":
        return None
    try:
        return (int(s[1:3], 16) / 255.0,
                int(s[3:5], 16) / 255.0,
                int(s[5:7], 16) / 255.0)
    except ValueError:
        return None


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


def simplify(verts, faces, cell: float, fmat=None):
    """
    Огрубление сеткой: вершины прилипают к узлам решётки с шагом cell,
    совпавшие сливаются, выродившиеся треугольники выбрасываются.

    Способ грубый, зато без сторонних библиотек и без сюрпризов: габарит
    сохраняется с точностью до шага, а корпус на плате выглядит так же.

    Если передан `fmat` (материал каждого треугольника), он прореживается
    вместе с треугольниками и возвращается третьим значением -- иначе
    после огрубления цвета съехали бы на чужие грани.
    """
    if cell <= 0:
        return (verts, faces) if fmat is None else (verts, faces, fmat)
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
    new_mat = []
    for k, (a, b, c) in enumerate(faces):
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
        if fmat is not None:
            new_mat.append(fmat[k] if k < len(fmat) else "")
    if fmat is None:
        return new_verts, new_faces
    return new_verts, new_faces, new_mat


def _fit_bbox(verts, box):
    """
    Вернуть огрублённую сетку в исходный габарит.

    Прилипание к решётке всегда стягивает модель внутрь: у корпуса FBGA-96
    габарит уезжал на 0,17 мм. Для 3D-тела на плате это уже заметно --
    именно по нему считают зазоры и высоту. Растягиваем обратно по каждой
    оси: форма не меняется, крайние точки встают на место.
    """
    if not verts:
        return verts
    out = list(verts)
    for ax in range(3):
        vals = [v[ax] for v in out]
        lo, hi = min(vals), max(vals)
        want_lo, want_hi = box[ax]
        span, want = hi - lo, want_hi - want_lo
        if span <= 1e-9 or want <= 1e-9:
            continue
        k = want / span
        if abs(k - 1.0) < 1e-9:
            continue
        out = [tuple(v[i] if i != ax else want_lo + (v[i] - lo) * k
                     for i in range(3)) for v in out]
    return out


def _bbox(verts):
    return [(min(v[i] for v in verts), max(v[i] for v in verts))
            for i in range(3)]


def _auto_cell(verts) -> float:
    """Шаг решётки: примерно 1/400 наибольшего габарита, но не мельче 10 мкм."""
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    return max(span / 400.0, 0.01)


def step_has_color(path: str) -> bool:
    """Есть ли в STEP наша раскраска (и, значит, можно перекрасить на месте)."""
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            return COLOR_TAG in f.read(4096)
    except OSError:
        return False


def set_step_color(path: str, color: str) -> bool:
    """
    Перекрасить готовый STEP на месте, не перегоняя его из OBJ.

    `color` -- '#RRGGBB'; пусто -- вернуть родные цвета модели, они
    записаны в заголовке файла. Возвращает True, если файл изменён.

    Трогаем только СВОИ файлы: у выбранного руками STEP нашей метки в
    заголовке нет, и функция честно ничего не делает.
    """
    import re

    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    pos = text.find(COLOR_TAG)
    if pos < 0:
        return False
    end = text.find("'", pos)
    if end < 0:
        return False
    native = [c.strip() for c in text[pos + len(COLOR_TAG):end].split("|")
              if c.strip()]
    rgb = parse_hex(color)
    seq = iter(native)

    def repl(m):
        if rgb:
            return "COLOUR_RGB('',%.6f,%.6f,%.6f)" % rgb
        try:
            parts = [float(v) for v in next(seq).split(",")]
        except (StopIteration, ValueError):
            parts = list(DEFAULT_BODY_RGB)
        if len(parts) != 3:
            parts = list(DEFAULT_BODY_RGB)
        return "COLOUR_RGB('',%.6f,%.6f,%.6f)" % tuple(parts)

    new, n = re.subn(r"COLOUR_RGB\('',[-0-9.eE]+,[-0-9.eE]+,[-0-9.eE]+\)",
                     repl, text)
    if not n or new == text:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii", errors="replace") as f:
        f.write(new)
    os.replace(tmp, path)
    return True


def obj_to_step(obj_path: str, step_path: str, name: str = "MODEL",
                scale: float = 1.0, log=None, budget: int = 0,
                color: str = "") -> str:
    """
    OBJ -> фасетный STEP. `budget` -- потолок треугольников (0 = по
    умолчанию). Чем он меньше, тем быстрее Altium вставляет модель в
    посадочное место.

    `color` ('#RRGGBB') красит всю модель в один цвет. Пусто -- берутся
    материалы из самого OBJ, и корпус в Altium выглядит как настоящий.
    """
    verts, faces, fmat, mats = read_obj_full(obj_path)
    if not verts or not faces:
        raise ValueError("OBJ не содержит геометрии")
    budget = int(budget or FACE_BUDGET)
    if len(faces) > max(budget, SIMPLIFY_OVER // 4):
        n0 = len(faces)
        box0 = _bbox(verts)
        cell = _auto_cell(verts)
        # Один проход раньше не гарантировал ничего: шаг решётки брался от
        # габарита, и у плотной модели после него оставались десятки тысяч
        # треугольников. Теперь огрубляем, пока не уложимся в бюджет.
        for _ in range(14):
            verts, faces, fmat = simplify(verts, faces, cell, fmat)
            if len(faces) <= budget:
                break
            cell *= 1.5
        verts = _fit_bbox(verts, box0)
        if log:
            log(f"  сетка огрублена: {n0} -> {len(faces)} треугольников "
                f"(шаг решётки {cell * 1000:.0f} мкм; крупная модель "
                f"добавляет минуты к сборке в Altium)")
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
    face_mat: List[str] = []
    for fi, (a, b, c) in enumerate(faces):
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
        face_mat.append(fmat[fi] if fi < len(fmat) else "")

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

    # --- раскраска ---
    #
    # Стиль вешается на КАЖДУЮ грань, а не на оболочку целиком. Так пишут
    # цвет промышленные CAD-ы, и так его читают все импортёры, включая
    # Altium; стиль на shell_based_surface_model половина программ просто
    # не замечает. Лишние записи -- около 30 байт на грань, на фоне самой
    # геометрии это незаметно.
    override = parse_hex(color)
    order: List[str] = []
    psa_of: Dict[str, int] = {}
    for key in list(dict.fromkeys(face_mat)):
        # В заголовок пишем РОДНОЙ цвет материала, даже когда сейчас
        # применён выбранный вручную: иначе «сбросить цвет» было бы
        # некуда возвращаться, кроме как перегонять модель заново.
        native = mats.get(key, DEFAULT_BODY_RGB)
        rgb = override or native
        cr = add("COLOUR_RGB('',%.6f,%.6f,%.6f)" % rgb)
        fac = add(f"FILL_AREA_STYLE_COLOUR('',#{cr})")
        fas = add(f"FILL_AREA_STYLE('',(#{fac}))")
        sfa = add(f"SURFACE_STYLE_FILL_AREA(#{fas})")
        sss = add(f"SURFACE_SIDE_STYLE('',(#{sfa}))")
        ssu = add(f"SURFACE_STYLE_USAGE(.BOTH.,#{sss})")
        psa_of[key] = add(f"PRESENTATION_STYLE_ASSIGNMENT((#{ssu}))")
        order.append("%.4f,%.4f,%.4f" % native)
    styled = [add(f"STYLED_ITEM('color',(#{psa_of[face_mat[i]]}),#{fid})")
              for i, fid in enumerate(face_ids)]
    # Ссылок тут столько же, сколько граней: разбиваем на строки, чтобы в
    # файле не появилось одной строки на сотню килобайт.
    refs = ",\n".join(",".join(f"#{s}" for s in styled[i:i + 12])
                      for i in range(0, len(styled), 12))
    add(f"MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION('',\n"
        f"({refs}),#{ctx})")
    tag = COLOR_TAG + "|".join(order)

    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    header = (
        "ISO-10303-21;\n"
        "HEADER;\n"
        f"FILE_DESCRIPTION(('{tag}'),'2;1');\n"
        f"FILE_NAME('{os.path.basename(step_path)}','{now}',(''),(''),"
        f"'GostLib','GostLib','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\n"
        "ENDSEC;\nDATA;\n")
    with open(step_path, "w", encoding="ascii", errors="replace") as f:
        f.write(header)
        f.write("\n".join(out))
        f.write("\nENDSEC;\nEND-ISO-10303-21;\n")
    if log:
        try:
            mb = os.path.getsize(step_path) / 1024 ** 2
        except OSError:
            mb = 0.0
        log(f"  STEP: {len(faces)} треугольников, {mb:.1f} МБ")
        if mb > 6.0:
            log("  внимание: модель крупная — Altium будет вставлять её "
                "в посадочное место долго. Уменьшите «подробность 3D» "
                "в настройках.")
    return step_path
