"""SVG-превью символа и посадочного места (для GUI и быстрой проверки)."""
from __future__ import annotations

import html
import math
from typing import List, Optional

from ..ir import Component, Footprint, Symbol
from ..gost.style import DEFAULT, PT2MIL, Style

# Имя шрифта в кавычках: без них Qt разбирает «GOST type B» как три
# семейства и берёт первое попавшееся, из-за чего кегль на превью не
# совпадает с тем, что получается в Altium.
FONT = "GOST type B"
# Запасное значение, если стиль пришёл без настройки. Altium отмеряет
# текст по высоте прописной буквы, а SVG -- по полной высоте кегля, отсюда
# множитель меньше единицы. Настраивается: Настройки -> «кегль превью».
COMPACT_FONT_SCALE = 0.72

# Превью рисуется теми же цветами, что уйдут в Altium: чёрным. Иначе
# «на превью красиво, а на схеме иначе».
SYM_COLORS = {
    "body": "#000000",
    "pin": "#000000",
    "text": "#000000",
    "num": "#000000",
    "desig": "#000000",
    "bg": "#ffffff",
    "grid": "#e8e8e8",
}

FP_LAYER_COLORS = {
    "top": "#c04040", "bottom": "#4060c0", "multi": "#909020",
    "silk": "#e0e0e0", "silk_bot": "#909090", "assy": "#c08040",
    "courtyard": "#d000d0", "mech": "#808080", "copper_top": "#c04040",
    "copper_bot": "#4060c0", "mask": "#7040a0", "paste": "#a0a0a0",
    "hole": "#202020",
}


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


# ------------------------------------------------------------------ символ ---

def symbol_svg(comp: Component, st: Optional[Style] = None,
               width: int = 900, dark: bool = False, part: int = 0) -> str:
    """
    Превью символа. `part` -- номер секции (0 -- все сразу).

    У многосекционного символа «все сразу» -- это куча наложенных друг на
    друга корпусов: в Altium секции живут на разных листах и вместе никогда
    не видны. Поэтому окно превью показывает одну секцию.
    """
    st = (st or DEFAULT).for_component(comp)
    # QSvgRenderer корректно принимает семейство целиком как значение
    # атрибута; CSS-списки с запасными шрифтами он, напротив, обрабатывает
    # нестабильно. Используем ровно то имя, которое уйдёт в Altium.
    font = _esc(st.font or FONT)
    font_scale = float(getattr(st, "preview_font_scale", 0) or
                       COMPACT_FONT_SCALE)
    sym = comp.symbol
    part = int(part or 0)
    if part:
        sym_pins = [p for p in sym.pins if int(p.unit or 1) == part]
        sym_prims = [pr for pr in sym.prims
                     if int(pr.unit or 1) in (part, 0)]
    else:
        sym_pins, sym_prims = list(sym.pins), list(sym.prims)
    parts: List[str] = []
    xs, ys = [0.0], [0.0]

    def track(x, y):
        xs.append(x)
        ys.append(y)

    pin_end = []
    for p in sym_pins:
        dx = {0: 1, 90: 0, 180: -1, 270: 0}[p.rotation % 360]
        dy = {0: 0, 90: 1, 180: 0, 270: -1}[p.rotation % 360]
        ex, ey = p.x + dx * p.length, p.y + dy * p.length
        pin_end.append((p, ex, ey))
        track(p.x, p.y)
        track(ex, ey)
    for pr in sym_prims:
        for pt in pr.pts:
            track(pt[0] - pr.radius, pt[1] - pr.radius)
            track(pt[0] + pr.radius, pt[1] + pr.radius)
            if pr.kind == "text":
                tw = st.text_w(pr.text, pr.size)
                if pr.justify == 2:
                    track(pt[0] - tw, pt[1])
                elif pr.justify == 1:
                    track(pt[0] - tw / 2, pt[1])
                    track(pt[0] + tw / 2, pt[1])
                else:
                    track(pt[0] + tw, pt[1])

    # Штатные Designator и Comment не входят в prims, но участвуют в
    # габарите превью. Иначе длинный партномер мог обрезаться краем SVG.
    dx, dy = sym.designator_pos
    track(dx + st.text_w(comp.designator, st.size_desig), dy)
    from .. import classify
    label = classify.label_for(comp)
    cx, cy = sym.comment_pos
    ctw = st.text_w(label, st.size_type)
    track(cx - ctw / 2, cy)
    track(cx + ctw / 2, cy)

    pad = 150
    minx, maxx = min(xs) - pad, max(xs) + pad
    miny, maxy = min(ys) - pad, max(ys) + pad
    w = max(1.0, maxx - minx)
    h = max(1.0, maxy - miny)

    body = SYM_COLORS["body"]
    txt = SYM_COLORS["text"]
    if dark:
        body, txt = "#ff8080", "#88b0ff"

    def X(v):
        return v - minx

    def Y(v):
        return maxy - v

    for pr in sym_prims:
        sw = 4 + 4 * max(0, pr.width - 1)
        if pr.kind == "line" and len(pr.pts) >= 2:
            parts.append(
                f'<line x1="{X(pr.pts[0][0]):.1f}" y1="{Y(pr.pts[0][1]):.1f}" '
                f'x2="{X(pr.pts[1][0]):.1f}" y2="{Y(pr.pts[1][1]):.1f}" '
                f'stroke="{body}" stroke-width="{sw}"/>')
        elif pr.kind == "rect" and len(pr.pts) >= 2:
            x1, y1 = pr.pts[0]
            x2, y2 = pr.pts[1]
            parts.append(
                f'<rect x="{X(min(x1,x2)):.1f}" y="{Y(max(y1,y2)):.1f}" '
                f'width="{abs(x2-x1):.1f}" height="{abs(y2-y1):.1f}" '
                f'fill="{"#fffbe6" if pr.filled else "none"}" stroke="{body}" '
                f'stroke-width="{sw}"/>')
        elif pr.kind == "poly" and len(pr.pts) >= 2:
            # Незалитая ломаная -- это именно ЛОМАНАЯ, а не многоугольник:
            # <polygon> дорисовывает отрезок от конца к началу, и открытые
            # скобки кварца превращались в прямоугольники. В Altium такие
            # фигуры уходят отрезками и всегда были правильными -- ошибка
            # была только в превью.
            pts = " ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in pr.pts)
            tag = "polygon" if pr.filled else "polyline"
            parts.append(
                f'<{tag} points="{pts}" fill="{body if pr.filled else "none"}" '
                f'stroke="{body}" stroke-width="{sw}" '
                f'stroke-linejoin="round"/>')
        elif pr.kind == "arc" and pr.pts:
            cx, cy = pr.pts[0]
            a1, a2 = math.radians(pr.a1), math.radians(pr.a2)
            x1, y1 = cx + pr.radius * math.cos(a1), cy + pr.radius * math.sin(a1)
            x2, y2 = cx + pr.radius * math.cos(a2), cy + pr.radius * math.sin(a2)
            large = 1 if ((pr.a2 - pr.a1) % 360) > 180 else 0
            parts.append(
                f'<path d="M {X(x1):.1f} {Y(y1):.1f} A {pr.radius:.1f} '
                f'{pr.radius:.1f} 0 {large} 0 {X(x2):.1f} {Y(y2):.1f}" '
                f'fill="none" stroke="{body}" stroke-width="{sw}"/>')
        elif pr.kind == "ellipse" and pr.pts:
            ecx, ecy = pr.pts[0]      # не cx/cy: см. примечание выше
            parts.append(
                f'<circle cx="{X(ecx):.1f}" cy="{Y(ecy):.1f}" r="{pr.radius:.1f}" '
                f'fill="{"#fffbe6" if pr.filled else "none"}" stroke="{body}" '
                f'stroke-width="{sw}"/>')
        elif pr.kind == "text" and pr.pts:
            x, y = pr.pts[0]
            anchor = {0: "start", 1: "middle", 2: "end"}.get(pr.justify, "start")
            fs = pr.size * PT2MIL * font_scale
            # ВНИМАНИЕ: имя не dy. Раньше здесь была именно dy, и она
            # затирала координату позиционного обозначения, взятую выше, --
            # «R?» в превью рисовалось в середине корпуса у ЛЮБОГО
            # компонента. В Altium при этом всё было на месте, потому что
            # туда координата уходит из задания, а не отсюда.
            voff = {0: 0.0, 1: fs * 0.33,
                    2: fs * 0.78}.get(pr.vjustify, fs * 0.33)
            rot = ""
            if pr.rotation:
                rot = f' transform="rotate({-pr.rotation} {X(x):.1f} {Y(y):.1f})"'
            parts.append(
                f'<text x="{X(x):.1f}" y="{Y(y) + voff:.1f}" fill="{txt}" '
                f'font-size="{fs:.1f}" text-anchor="{anchor}"{rot} '
                f'font-family="{font}">{_esc(pr.text)}</text>')

    for p, ex, ey in pin_end:
        parts.append(
            f'<line x1="{X(p.x):.1f}" y1="{Y(p.y):.1f}" x2="{X(ex):.1f}" '
            f'y2="{Y(ey):.1f}" stroke="{body}" stroke-width="4"/>')
        parts.append(f'<circle cx="{X(ex):.1f}" cy="{Y(ey):.1f}" r="8" '
                     f'fill="none" stroke="{body}" stroke-width="3"/>')
        if p.show_number and p.number:
            fs = st.size_pin_num * PT2MIL * font_scale
            mx, my = (p.x + ex) / 2, (p.y + ey) / 2
            if p.rotation % 180 == 0:
                parts.append(
                    f'<text x="{X(mx):.1f}" y="{Y(my) - 18:.1f}" fill="{body}" '
                    f'font-size="{fs:.1f}" text-anchor="middle" '
                    f'font-family="{font}">'
                    f'{_esc(p.number)}</text>')
            else:
                parts.append(
                    f'<text x="{X(mx) + 18:.1f}" y="{Y(my):.1f}" fill="{body}" '
                    f'font-size="{fs:.1f}" text-anchor="start" '
                    f'font-family="{font}">'
                    f'{_esc(p.number)}</text>')
        if p.show_name and p.name:
            fs = st.size_pin * PT2MIL * font_scale
            anchor = "end" if p.rotation % 360 == 180 else "start"
            ox = -20 if anchor == "end" else 20
            parts.append(
                f'<text x="{X(ex) + ox:.1f}" y="{Y(ey) + fs*0.33:.1f}" '
                f'fill="{txt}" font-size="{fs:.1f}" text-anchor="{anchor}" '
                f'font-family="{font}">{_esc(p.name)}</text>')

    fsd = st.size_desig * PT2MIL * font_scale
    dx, dy = sym.designator_pos        # именно здесь, а не за сто строк до
    cx, cy = sym.comment_pos
    parts.append(
        f'<text x="{X(dx):.1f}" y="{Y(dy):.1f}" fill="{SYM_COLORS["desig"]}" '
        f'font-size="{fsd:.1f}" font-family="{font}">'
        f'{_esc(comp.designator)}</text>')
    fsc = st.size_type * PT2MIL * font_scale
    parts.append(
        f'<text x="{X(cx):.1f}" y="{Y(cy) + fsc*0.33:.1f}" fill="#a00000" '
        f'font-size="{fsc:.1f}" text-anchor="middle" '
        f'font-family="{font}">{_esc(label)}</text>')

    bg = "#1e1e1e" if dark else "#ffffff"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" '
            f'width="{width}" preserveAspectRatio="xMidYMid meet">'
            f'<rect width="100%" height="100%" fill="{bg}"/>'
            + "".join(parts) + "</svg>")


# --------------------------------------------------------------- посадка -----

def footprint_svg(fp: Footprint, width: int = 700, dark: bool = True) -> str:
    parts: List[str] = []
    xs, ys = [0.0], [0.0]
    # Фигуры на слоях маски и пасты не показываем: Altium эти окна делает
    # сам из площадок, а нарисованные источником -- дубликат, который в
    # превью выглядит рамкой вокруг каждой площадки.
    prims = [pr for pr in fp.prims if pr.layer not in ("paste", "mask")]
    for p in fp.pads:
        xs += [p.x - p.w, p.x + p.w]
        ys += [p.y - p.h, p.y + p.h]
    for pr in prims:
        for pt in pr.pts:
            xs += [pt[0] - pr.radius, pt[0] + pr.radius]
            ys += [pt[1] - pr.radius, pt[1] + pr.radius]
    pad = 1.0
    minx, maxx = min(xs) - pad, max(xs) + pad
    miny, maxy = min(ys) - pad, max(ys) + pad
    w, h = max(0.5, maxx - minx), max(0.5, maxy - miny)
    K = 40.0   # масштаб мм -> у.е. SVG

    def X(v):
        return (v - minx) * K

    def Y(v):
        return (maxy - v) * K

    order = {"courtyard": 0, "assy": 1, "mech": 2, "silk_bot": 3, "silk": 4}
    for pr in sorted(prims, key=lambda p: order.get(p.layer, 5)):
        col = FP_LAYER_COLORS.get(pr.layer, "#808080")
        sw = max(1.0, pr.width * K)
        if pr.kind == "line" and len(pr.pts) >= 2:
            parts.append(f'<line x1="{X(pr.pts[0][0]):.1f}" y1="{Y(pr.pts[0][1]):.1f}"'
                         f' x2="{X(pr.pts[1][0]):.1f}" y2="{Y(pr.pts[1][1]):.1f}"'
                         f' stroke="{col}" stroke-width="{sw:.1f}" '
                         f'stroke-linecap="round"/>')
        elif pr.kind == "rect" and len(pr.pts) >= 2:
            x1, y1 = pr.pts[0]
            x2, y2 = pr.pts[1]
            parts.append(f'<rect x="{X(min(x1,x2)):.1f}" y="{Y(max(y1,y2)):.1f}" '
                         f'width="{abs(x2-x1)*K:.1f}" height="{abs(y2-y1)*K:.1f}" '
                         f'fill="none" stroke="{col}" stroke-width="{sw:.1f}"/>')
        elif pr.kind == "circle" and pr.pts:
            parts.append(f'<circle cx="{X(pr.pts[0][0]):.1f}" '
                         f'cy="{Y(pr.pts[0][1]):.1f}" r="{pr.radius*K:.1f}" '
                         f'fill="none" stroke="{col}" stroke-width="{sw:.1f}"/>')
        elif pr.kind == "arc" and pr.pts:
            cx, cy = pr.pts[0]
            a1, a2 = math.radians(pr.a1), math.radians(pr.a2)
            x1, y1 = cx + pr.radius * math.cos(a1), cy + pr.radius * math.sin(a1)
            x2, y2 = cx + pr.radius * math.cos(a2), cy + pr.radius * math.sin(a2)
            large = 1 if ((pr.a2 - pr.a1) % 360) > 180 else 0
            parts.append(f'<path d="M {X(x1):.1f} {Y(y1):.1f} A {pr.radius*K:.1f} '
                         f'{pr.radius*K:.1f} 0 {large} 0 {X(x2):.1f} {Y(y2):.1f}" '
                         f'fill="none" stroke="{col}" stroke-width="{sw:.1f}"/>')
        elif pr.kind == "poly" and len(pr.pts) >= 3:
            pts = " ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in pr.pts)
            parts.append(f'<polygon points="{pts}" fill="{col}" fill-opacity="0.5" '
                         f'stroke="{col}" stroke-width="1"/>')

    # Контур площадки считается один раз в милиметрах и одинаково для
    # превью и для просмотра 3D. Раньше здесь рисовали «на глаз»: овал
    # выходил эллипсом (у настоящего obround борта прямые),
    # восьмиугольник -- прямоугольником, а паз -- круглым отверстием
    # вдвое короче настоящего.
    from ..ir import hole_polygon, pad_polygon

    for p in fp.pads:
        col = FP_LAYER_COLORS.get(p.layer, "#c04040")
        cx, cy = X(p.x), Y(p.y)
        pw, ph = p.w * K, p.h * K
        if (p.shape or "rect").lower() == "round" and abs(p.w - p.h) < 1e-9:
            parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" '
                         f'r="{pw / 2:.1f}" fill="{col}"/>')
        else:
            pts = " ".join(f"{X(a):.1f},{Y(b):.1f}"
                           for a, b in pad_polygon(p))
            parts.append(f'<polygon points="{pts}" fill="{col}"/>')
        if p.hole > 0:
            hp = hole_polygon(p)
            hc = FP_LAYER_COLORS["hole"]
            if len(hp) >= 3:
                pts = " ".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in hp)
                parts.append(f'<polygon points="{pts}" fill="{hc}"/>')
            else:
                parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" '
                             f'r="{p.hole * K / 2:.1f}" fill="{hc}"/>')
        parts.append(f'<text x="{cx:.1f}" y="{cy + min(pw,ph)*0.18:.1f}" '
                     f'fill="#ffffff" font-size="{max(6, min(pw,ph)*0.5):.1f}" '
                     f'text-anchor="middle" font-family="Arial">'
                     f'{_esc(p.number)}</text>')

    bg = "#101018" if dark else "#ffffff"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {w*K:.0f} {h*K:.0f}" width="{width}" '
            f'preserveAspectRatio="xMidYMid meet">'
            f'<rect width="100%" height="100%" fill="{bg}"/>'
            + "".join(parts) + "</svg>")
