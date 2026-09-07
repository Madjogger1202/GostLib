"""
Сведения о посадочном месте: габариты, шаг, отверстия, 3D.

Нужно, чтобы не открывать .PcbLib в Altium ради вопроса «какой у неё
размер». Всё считается по IR (мм), ничего не читается с диска, кроме
проверки наличия файла модели.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .ir import Footprint, Pad

# слои, по которым считается «контур корпуса»
BODY_LAYERS = ("courtyard", "assy", "silk", "mech")


def _pad_corners(p: Pad) -> List[Tuple[float, float]]:
    """Углы площадки с учётом её поворота."""
    hw, hh = p.w / 2.0, p.h / 2.0
    a = math.radians(p.rot or 0.0)
    ca, sa = math.cos(a), math.sin(a)
    out = []
    for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        out.append((p.x + dx * ca - dy * sa, p.y + dx * sa + dy * ca))
    return out


def _bbox(points) -> Optional[Tuple[float, float, float, float]]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _layer_bbox(fp: Footprint, layer: str):
    pts: List[Tuple[float, float]] = []
    for pr in fp.prims:
        if pr.layer != layer:
            continue
        if pr.kind in ("arc", "circle") and pr.pts:
            cx, cy = pr.pts[0]
            r = pr.radius
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
        else:
            pts += [(pt[0], pt[1]) for pt in pr.pts]
    return _bbox(pts)


def _common_pitch(values: List[float]) -> float:
    """Наиболее часто встречающийся шаг между соседними координатами."""
    vs = sorted(set(round(v, 3) for v in values))
    if len(vs) < 2:
        return 0.0
    diffs: Dict[float, int] = {}
    for a, b in zip(vs, vs[1:]):
        d = round(b - a, 3)
        if d <= 0.001:
            continue
        diffs[d] = diffs.get(d, 0) + 1
    if not diffs:
        return 0.0
    best = max(diffs.items(), key=lambda kv: (kv[1], -kv[0]))
    return best[0]


@dataclass
class FpInfo:
    name: str = ""
    description: str = ""
    pad_count: int = 0
    smd: int = 0
    tht: int = 0
    npth: int = 0
    pads_bbox: Optional[Tuple[float, float, float, float]] = None
    body_bbox: Optional[Tuple[float, float, float, float]] = None
    body_source: str = ""            # courtyard / assy / silk / pads
    pitch_x: float = 0.0
    pitch_y: float = 0.0
    pad_sizes: List[Tuple[float, float]] = field(default_factory=list)
    holes: List[float] = field(default_factory=list)
    height: float = 0.0
    model_path: str = ""
    tape_rot: float = 0.0
    model_ok: bool = False
    model_size: int = 0
    external: str = ""               # путь к вендорской .PcbLib, если посадка чужая
    layers: List[str] = field(default_factory=list)

    # --- производные размеры ---
    @property
    def pads_size(self) -> Tuple[float, float]:
        b = self.pads_bbox
        return (0.0, 0.0) if not b else (b[2] - b[0], b[3] - b[1])

    @property
    def body_size(self) -> Tuple[float, float]:
        b = self.body_bbox
        return (0.0, 0.0) if not b else (b[2] - b[0], b[3] - b[1])

    def lines(self) -> List[str]:
        """Готовые строки для карточки в интерфейсе."""
        out = [f"Посадочное место: {self.name}"]
        if self.description:
            out.append(self.description)
        if self.external:
            out.append(f"Источник: вендорская библиотека "
                       f"{os.path.basename(self.external)}")
        bw, bh = self.body_size
        pw, ph = self.pads_size
        if bw or bh:
            out.append(f"Габарит ({self.body_source}): "
                       f"{bw:.2f} x {bh:.2f} мм")
        if pw or ph:
            out.append(f"По площадкам: {pw:.2f} x {ph:.2f} мм")
        if self.height:
            out.append(f"Высота: {self.height:.2f} мм")
        parts = [f"площадок {self.pad_count}"]
        if self.smd:
            parts.append(f"SMD {self.smd}")
        if self.tht:
            parts.append(f"выводных {self.tht}")
        if self.npth:
            parts.append(f"без металлизации {self.npth}")
        out.append(", ".join(parts))
        if self.pitch_x or self.pitch_y:
            px = f"{self.pitch_x:.3f}".rstrip("0").rstrip(".") if self.pitch_x else "-"
            py = f"{self.pitch_y:.3f}".rstrip("0").rstrip(".") if self.pitch_y else "-"
            out.append(f"Шаг: X {px} мм, Y {py} мм")
        if self.pad_sizes:
            sizes = ", ".join(f"{w:.2f}x{h:.2f}" for w, h in self.pad_sizes[:4])
            out.append(f"Размер площадок: {sizes}"
                       + (" ..." if len(self.pad_sizes) > 4 else ""))
        if self.holes:
            hs = ", ".join(f"{d:.2f}" for d in self.holes[:4])
            out.append(f"Отверстия: {hs} мм"
                       + (" ..." if len(self.holes) > 4 else ""))
        if self.model_path:
            state = "есть" if self.model_ok else "ФАЙЛ НЕ НАЙДЕН"
            out.append(f"3D-модель: {os.path.basename(self.model_path)} ({state})")
            if self.tape_rot:
                out.append(f"Угол в ленте поставщика: {self.tape_rot:g}°")
        else:
            out.append("3D-модель: нет")
        if self.layers:
            out.append("Слои графики: " + ", ".join(self.layers))
        return out

    def text(self) -> str:
        return "\n".join(self.lines())


def describe(fp: Footprint) -> FpInfo:
    info = FpInfo(name=fp.name, description=fp.description,
                  height=float(fp.height or 0.0))
    info.external = fp.source_pcblib or ""

    corners: List[Tuple[float, float]] = []
    sizes = []
    holes = []
    xs, ys = [], []
    for p in fp.pads:
        corners += _pad_corners(p)
        key = (round(p.w, 3), round(p.h, 3))
        if key not in sizes:
            sizes.append(key)
        if p.hole > 0:
            holes.append(round(p.hole, 3))
            if p.plated:
                info.tht += 1
            else:
                info.npth += 1
        else:
            info.smd += 1
        xs.append(p.x)
        ys.append(p.y)
    info.pad_count = len(fp.pads)
    info.pads_bbox = _bbox(corners)
    info.pad_sizes = sizes
    info.holes = sorted(set(holes))
    info.pitch_x = _common_pitch(xs)
    info.pitch_y = _common_pitch(ys)

    for layer in BODY_LAYERS:
        b = _layer_bbox(fp, layer)
        if b:
            info.body_bbox = b
            info.body_source = {"courtyard": "область установки",
                                "assy": "сборочный слой",
                                "silk": "шелкография",
                                "mech": "механический слой"}.get(layer, layer)
            break
    if not info.body_bbox:
        info.body_bbox = info.pads_bbox
        info.body_source = "по площадкам"

    seen = []
    for pr in fp.prims:
        if pr.layer not in seen:
            seen.append(pr.layer)
    info.layers = seen

    if fp.model and fp.model.path:
        info.model_path = fp.model.path
        info.model_ok = os.path.isfile(fp.model.path)
        if info.model_ok:
            try:
                info.model_size = os.path.getsize(fp.model.path)
            except OSError:
                info.model_size = 0
    return info


def describe_component(comp) -> List[FpInfo]:
    return [describe(fp) for fp in comp.footprints]
