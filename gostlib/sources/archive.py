"""
Разбор произвольного архива с компонентом.

Понимает то, что реально отдают сайты:
  * Ultra Librarian / SnapEDA / Component Search Engine -- папка Altium
    с .SchLib и .PcbLib (+ .step рядом);
  * KiCad -- .kicad_sym и .kicad_mod (+ .step / .stp / .wrl);
  * просто .step без библиотек -- тогда компонент создаётся вручную.

Архив распаковывается во временную папку, файлы классифицируются,
дальше подключаются соответствующие импортёры.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..ir import Component, Model3D
from . import altium as al
from . import kicad as kc


@dataclass
class ArchiveScan:
    root: str = ""
    schlibs: List[str] = field(default_factory=list)
    pcblibs: List[str] = field(default_factory=list)
    kicad_syms: List[str] = field(default_factory=list)
    kicad_mods: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    wrls: List[str] = field(default_factory=list)
    others: List[str] = field(default_factory=list)
    tempdir: Optional[str] = None

    def kind(self) -> str:
        if self.schlibs or self.pcblibs:
            return "altium"
        if self.kicad_syms or self.kicad_mods:
            return "kicad"
        if self.steps:
            return "step"
        return "unknown"

    def cleanup(self):
        if self.tempdir and os.path.isdir(self.tempdir):
            shutil.rmtree(self.tempdir, ignore_errors=True)
            self.tempdir = None


_EXT = {
    ".schlib": "schlibs", ".pcblib": "pcblibs",
    ".kicad_sym": "kicad_syms", ".kicad_mod": "kicad_mods",
    # OBJ и STL тоже считаем моделями: конвертер переведёт их в STEP,
    # а иначе у компонента из архива просто не будет 3D
    ".step": "steps", ".stp": "steps", ".wrl": "wrls",
    ".obj": "steps", ".stl": "steps",
}


def scan_dir(root: str) -> ArchiveScan:
    sc = ArchiveScan(root=root)
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower()
            key = _EXT.get(ext)
            if key:
                getattr(sc, key).append(p)
            else:
                sc.others.append(p)
    for k in ("schlibs", "pcblibs", "kicad_syms", "kicad_mods", "steps", "wrls"):
        getattr(sc, k).sort()
    return sc


def open_archive(path: str) -> ArchiveScan:
    """Распаковать zip (или взять папку/файл как есть) и просканировать."""
    if os.path.isdir(path):
        return scan_dir(path)
    if zipfile.is_zipfile(path):
        tmp = tempfile.mkdtemp(prefix="gostlib_")
        with zipfile.ZipFile(path) as z:
            for m in z.namelist():
                if m.startswith("/") or ".." in m.replace("\\", "/").split("/"):
                    continue
                z.extract(m, tmp)
        sc = scan_dir(tmp)
        sc.tempdir = tmp
        return sc
    # одиночный файл
    tmp = tempfile.mkdtemp(prefix="gostlib_")
    dst = os.path.join(tmp, os.path.basename(path))
    shutil.copy2(path, dst)
    sc = scan_dir(tmp)
    sc.tempdir = tmp
    return sc


def _steps_map(sc: ArchiveScan) -> Dict[str, str]:
    """Сопоставить .step по имени файла; '*' -- единственная модель."""
    m: Dict[str, str] = {}
    for s in sc.steps:
        m[os.path.splitext(os.path.basename(s))[0]] = s
    if len(sc.steps) == 1:
        m["*"] = sc.steps[0]
    return m


def components(sc: ArchiveScan, log=None) -> List[Component]:
    """Собрать компоненты из просканированного архива."""
    log = log or (lambda *_: None)
    out: List[Component] = []
    steps = _steps_map(sc)

    if sc.schlibs:
        for sl in sc.schlibs:
            pl = _match_pcblib(sl, sc.pcblibs)
            try:
                cs = al.components_from_altium(sl, pl or "", steps)
            except Exception as e:
                log(f"Не удалось прочитать {os.path.basename(sl)}: {e}")
                continue
            log(f"{os.path.basename(sl)}: компонентов {len(cs)}"
                + (f", посадки из {os.path.basename(pl)}" if pl else ""))
            out.extend(cs)
        if out:
            return out
        # Архивы производителей (Ultra Librarian, SnapEDA) кладут один и
        # тот же компонент сразу для десятка САПР. Если библиотека Altium
        # не прочиталась -- это не повод возвращать пустоту: рядом, в том
        # же архиве, лежат файлы KiCad, и они читаются всегда.
        if sc.kicad_syms or sc.kicad_mods or sc.steps:
            log("Из библиотеки Altium ничего не вышло — беру то же самое "
                "из файлов KiCad в этом же архиве")

    if sc.kicad_syms:
        log(f"В архиве: символов KiCad {len(sc.kicad_syms)}, "
            f"посадок {len(sc.kicad_mods)}, моделей {len(sc.steps)}")
        for ks in sc.kicad_syms:
            try:
                names = list(kc.parse_kicad_sym(ks).keys())
            except Exception as e:
                log(f"Не удалось прочитать {os.path.basename(ks)}: {e}")
                continue
            for nm in names:
                try:
                    c = kc.component_from_kicad_sym(ks, nm)
                except Exception as e:
                    log(f"  {nm}: {e}")
                    continue
                _attach_kicad_fp(c, sc, steps, log)
                out.append(c)
        return out

    if sc.kicad_mods:
        log(f"В архиве только посадки KiCad: {len(sc.kicad_mods)} шт., "
            f"моделей {len(sc.steps)}")
        for km in sc.kicad_mods:
            try:
                fp = kc.parse_kicad_mod(km)
            except Exception as e:
                log(f"Не удалось прочитать {os.path.basename(km)}: {e}")
                continue
            c = Component(name=fp.name, ctype="other", source="kicad",
                          source_ref=os.path.basename(km))
            _attach_step(fp, steps)
            c.footprints.append(fp)
            out.append(c)
        return out

    if sc.steps:
        for s in sc.steps:
            nm = os.path.splitext(os.path.basename(s))[0]
            c = Component(name=nm, ctype="other", source="step", source_ref=s)
            from ..ir import Footprint
            fp = Footprint(name=nm, model=Model3D(path=s))
            c.footprints.append(fp)
            out.append(c)
    return out


def _match_pcblib(schlib: str, pcblibs: List[str]) -> str:
    if not pcblibs:
        return ""
    base = os.path.splitext(os.path.basename(schlib))[0].lower()
    same_dir = [p for p in pcblibs
                if os.path.dirname(p) == os.path.dirname(schlib)]
    for p in same_dir or pcblibs:
        if os.path.splitext(os.path.basename(p))[0].lower() == base:
            return p
    return (same_dir or pcblibs)[0]


def _key(name: str) -> str:
    """Имя без разделителей и регистра: 'SOIC-8_3.9x4.9mm' -> 'soic839x49mm'."""
    return re.sub(r"[^0-9a-zA-Zа-яА-Я]+", "", (name or "")).lower()


def _score(want: str, have: str) -> int:
    """
    Насколько имя посадки похоже на то, что просит символ.

    Точное совпадение важнее вхождения, вхождение важнее общего начала.
    Ноль -- не подходит совсем.
    """
    a, b = _key(want), _key(have)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        return 70 + min(20, len(min(a, b, key=len)) // 2)
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n if n >= 4 else 0


def _best_footprint(want: str, files: List[str]) -> Tuple[str, int]:
    """Самый похожий .kicad_mod из архива и его оценка."""
    best, score = "", 0
    for f in files:
        sc = _score(want, os.path.splitext(os.path.basename(f))[0])
        if sc > score:
            best, score = f, sc
    return best, score


def _attach_step(fp, steps: Dict[str, str]):
    if fp.model and fp.model.path and os.path.isfile(fp.model.path):
        return
    cand = steps.get(fp.name) or steps.get("*")
    if cand:
        if fp.model:
            fp.model.path = cand
        else:
            fp.model = Model3D(path=cand)


def _attach_kicad_fp(c: Component, sc: ArchiveScan, steps: Dict[str, str], log):
    """
    Подобрать символу посадочное место из архива.

    Раньше требовалось точное совпадение имени файла с полем Footprint --
    и если архив собран не по этому правилу (а так бывает чаще, чем нет),
    компонент приезжал без посадки: символ есть, 3D есть, а посадки нет.
    Теперь имена сравниваются без разделителей и регистра, а при полной
    неудаче берётся единственный кандидат или самый похожий, и об этом
    пишется в журнал -- чтобы было видно, что выбрано наугад.
    """
    if not sc.kicad_mods:
        if sc.pcblibs:
            log(f"  {c.name}: посадок KiCad в архиве нет, но есть "
                f"{os.path.basename(sc.pcblibs[0])} -- посадка не подставлена")
        else:
            log(f"  {c.name}: в архиве нет ни одного .kicad_mod")
        return
    want = (c.params.get("KiCadFootprint") or "").split(":")[-1]
    picked, score = _best_footprint(want, sc.kicad_mods) if want else ("", 0)
    if not picked:
        # символ не сказал, какая ему посадка -- пробуем по имени самого
        # компонента, потом по корпусу
        for guess in (c.name, c.params.get("Package", "")):
            picked, score = _best_footprint(guess, sc.kicad_mods)
            if picked:
                break
    if not picked and len(sc.kicad_mods) == 1:
        picked, score = sc.kicad_mods[0], 1
    if not picked:
        log(f"  {c.name}: посадка не подобралась (искали «{want}», "
            f"в архиве {len(sc.kicad_mods)} шт.)")
        return
    if score < 70:
        log(f"  {c.name}: посадка выбрана приблизительно -- "
            f"{os.path.basename(picked)} (искали «{want}»)")
    try:
        fp = kc.parse_kicad_mod(picked)
    except Exception as e:
        log(f"  посадка {os.path.basename(picked)}: {e}")
        return
    if fp.model and fp.model.path:
        r = kc.resolve_3d(fp.model.path)
        if r:
            fp.model.path = r
    _attach_step(fp, steps)
    c.footprints.append(fp)
    log(f"  {c.name}: посадка {fp.name}, площадок {len(fp.pads)}"
        + (", 3D есть" if fp.model and fp.model.path else ", без 3D"))
