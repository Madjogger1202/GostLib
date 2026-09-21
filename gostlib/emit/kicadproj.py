"""
Выгрузка компонентов прямо в проект KiCad.

Не «импорт из KiCad», а обратное: компонент, собранный в GostLib (чаще
всего из LCSC/EasyEDA), дописывается в локальные библиотеки проекта KiCad
-- символ в <lib>.kicad_sym, посадка в <lib>.pretty, 3D-модель в
<lib>.3dshapes -- и библиотеки подключаются к проекту в sym-lib-table и
fp-lib-table. Открыл проект -- компонент на месте.

Правила, ради которых всё это написано аккуратно:

* **Дописываем, а не перезаписываем.** В библиотеке проекта уже могут
  лежать символы, нарисованные руками. Символ с тем же именем заменяется,
  остальные остаются байт в байт.
* **Пути -- через ${KIPRJMOD}**, если папка внутри проекта: проект можно
  перенести или положить в git, и ссылки не порвутся.
* **Таблицы библиотек не ломаем.** Запись добавляется, только если
  библиотеки с таким именем ещё нет. Если есть, но смотрит в другое место,
  -- ничего не трогаем и говорим об этом.
"""
from __future__ import annotations

import copy
import os
import re
import shutil
from typing import Dict, Iterable, List, Optional, Tuple

from ..ir import Component, Footprint
from . import kicadout as ko


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def kicad_path(path: str, project_dir: str) -> str:
    """Путь для KiCad: внутри проекта -- через ${KIPRJMOD}, иначе абсолютный."""
    path = os.path.abspath(path)
    root = os.path.abspath(project_dir)
    try:
        rel = os.path.relpath(path, root)
    except ValueError:              # другой диск на Windows
        rel = ""
    if rel and not rel.startswith(".."):
        return "${KIPRJMOD}/" + rel.replace("\\", "/")
    return path.replace("\\", "/")


def project_dir_of(path: str) -> str:
    """Папка проекта по пути к .kicad_pro или к самой папке."""
    path = os.path.abspath(path or "")
    if os.path.isfile(path):
        return os.path.dirname(path)
    return path


def find_project_file(project_dir: str) -> str:
    try:
        for n in sorted(os.listdir(project_dir)):
            if n.lower().endswith(".kicad_pro"):
                return os.path.join(project_dir, n)
    except OSError:
        pass
    return ""


# ------------------------------------------------------ символы: слияние ---

def _blocks(text: str) -> List[Tuple[int, int]]:
    """
    Границы верхнеуровневых выражений внутри корневого (kicad_symbol_lib …).

    Разбор по скобкам с учётом строк в кавычках: имя символа или его
    описание спокойно содержат скобки, и наивный подсчёт ломался бы.
    """
    out: List[Tuple[int, int]] = []
    depth = 0
    i, n = 0, len(text)
    start = -1
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "(":
            depth += 1
            if depth == 2:
                start = i
        elif ch == ")":
            if depth == 2 and start >= 0:
                out.append((start, i + 1))
                start = -1
            depth -= 1
        i += 1
    return out


_NAME_RE = re.compile(r'\(\s*symbol\s+"((?:[^"\\]|\\.)*)"')


def symbol_blocks(text: str) -> Dict[str, Tuple[int, int]]:
    """{имя символа: (начало, конец)} для верхнеуровневых (symbol "…")."""
    out: Dict[str, Tuple[int, int]] = {}
    for a, b in _blocks(text):
        m = _NAME_RE.match(text, a)
        if m:
            out[m.group(1)] = (a, b)
    return out


def merge_symbols(lib_path: str, new_text: str) -> Tuple[List[str], List[str]]:
    """
    Дописать символы из new_text (целая библиотека) в lib_path.

    Возвращает (добавленные, заменённые). Остальное содержимое файла --
    включая символы, нарисованные руками, и их форматирование -- не
    меняется.
    """
    new_blocks = symbol_blocks(new_text)
    if not os.path.isfile(lib_path):
        with open(lib_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_text if new_text.endswith("\n") else new_text + "\n")
        return sorted(new_blocks), []
    with open(lib_path, encoding="utf-8") as f:
        old = f.read()
    have = symbol_blocks(old)
    added: List[str] = []
    replaced: List[str] = []
    # замены -- с конца файла, чтобы смещения ещё не тронутых блоков
    # оставались верными
    for name in sorted(have, key=lambda k: -have[k][0]):
        if name in new_blocks:
            a, b = have[name]
            na, nb = new_blocks[name]
            old = old[:a] + new_text[na:nb] + old[b:]
            replaced.append(name)
    tail = old.rstrip()
    if not tail.endswith(")"):
        raise ValueError(f"{os.path.basename(lib_path)}: не похоже на "
                         f"библиотеку символов KiCad")
    extra = [new_text[a:b] for name, (a, b) in new_blocks.items()
             if name not in have]
    added = [name for name in new_blocks if name not in have]
    if extra:
        tail = tail[:-1].rstrip() + "\n  " + "\n  ".join(extra) + "\n)"
    with open(lib_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(tail + "\n")
    return added, sorted(replaced)


# ------------------------------------------------- таблицы библиотек -------

def ensure_lib_table(table_path: str, kind: str, name: str, uri: str) -> str:
    """
    Подключить библиотеку в sym-lib-table / fp-lib-table проекта.

    kind -- "sym" или "fp". Возвращает "added" | "present" | "conflict:<uri>".
    """
    root = "sym_lib_table" if kind == "sym" else "fp_lib_table"
    entry = (f'  (lib (name {ko._q(name)})(type "KiCad")(uri {ko._q(uri)})'
             f'(options "")(descr "GostLib"))')
    if not os.path.isfile(table_path):
        with open(table_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"({root}\n  (version 7)\n{entry}\n)\n")
        return "added"
    with open(table_path, encoding="utf-8") as f:
        text = f.read()
    # имя в старых таблицах бывает и без кавычек
    m = re.search(r'\(lib\s+\(name\s+"?' + re.escape(name) +
                  r'"?\)(.*?)\)\s*(?:\n|$)', text, re.S)
    if m:
        u = re.search(r'\(uri\s+"((?:[^"\\]|\\.)*)"\)', m.group(0))
        cur = u.group(1) if u else ""
        if cur.replace("\\", "/").rstrip("/") == uri.replace("\\", "/").rstrip("/"):
            return "present"
        return "conflict:" + cur
    tail = text.rstrip()
    if not tail.endswith(")"):
        raise ValueError(f"{os.path.basename(table_path)}: не похоже на "
                         f"таблицу библиотек KiCad")
    tail = tail[:-1].rstrip() + "\n" + entry + "\n)"
    with open(table_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(tail + "\n")
    return "added"


# ---------------------------------------------------------- выгрузка -------

def default_dirs(project_dir: str, lib: str, split: bool = False
                 ) -> Tuple[str, str, str]:
    """(папка символов, папка .pretty, папка 3D) по умолчанию."""
    base = os.path.join(project_dir, lib)
    if split:
        return (os.path.join(project_dir, "symbols"),
                os.path.join(project_dir, "footprints", f"{lib}.pretty"),
                os.path.join(project_dir, "3dmodels", f"{lib}.3dshapes"))
    return (base, os.path.join(base, f"{lib}.pretty"),
            os.path.join(base, f"{lib}.3dshapes"))


def export_project(comps: Iterable[Component], project_dir: str,
                   lib: str = "GostLib", sym_dir: str = "", fp_dir: str = "",
                   model_dir: str = "", register: bool = True,
                   copy_models: bool = True, log=None) -> Dict[str, object]:
    """
    Дописать компоненты в библиотеки проекта KiCad.

    Возвращает сводку: что добавлено, что заменено, куда легло и что с
    таблицами библиотек.
    """
    log = log or (lambda *_: None)
    project_dir = project_dir_of(project_dir)
    if not os.path.isdir(project_dir):
        raise FileNotFoundError(f"нет папки проекта: {project_dir}")
    lib = ko._safe(lib or "GostLib", "GostLib")
    d_sym, d_fp, d_3d = default_dirs(project_dir, lib)
    sym_dir = sym_dir or d_sym
    fp_dir = fp_dir or d_fp
    model_dir = model_dir or d_3d
    for d in (sym_dir, fp_dir):
        os.makedirs(d, exist_ok=True)

    comps = [c for c in comps if c and c.symbol and c.symbol.pins]
    work: List[Component] = []
    models: List[str] = []
    mods: List[str] = []
    done_fp = set()
    for c in comps:
        c2 = copy.deepcopy(c)
        for fp in c2.footprints:
            m = fp.model
            if m is not None and m.path and os.path.isfile(m.path) \
                    and copy_models:
                os.makedirs(model_dir, exist_ok=True)
                dst = os.path.join(model_dir, os.path.basename(m.path))
                if _norm(dst) != _norm(m.path):
                    shutil.copy2(m.path, dst)
                models.append(dst)
                m.path = kicad_path(dst, project_dir)
            elif m is not None and m.path:
                m.path = kicad_path(m.path, project_dir)
            if not fp.pads:
                continue
            nm = ko._safe(fp.name, "FP")
            if nm in done_fp:
                continue
            done_fp.add(nm)
            mods.append(ko.write_kicad_mod(
                os.path.join(fp_dir, nm + ".kicad_mod"), fp))
        work.append(c2)

    # символы: пишем во временный файл и вливаем в библиотеку проекта
    lib_path = os.path.join(sym_dir, f"{lib}.kicad_sym")
    tmp = lib_path + ".gostlib-tmp"
    ko.write_kicad_sym(tmp, work, fp_lib=lib)
    try:
        with open(tmp, encoding="utf-8") as f:
            new_text = f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    added, replaced = merge_symbols(lib_path, new_text)

    tables: Dict[str, str] = {}
    if register:
        tables["sym"] = ensure_lib_table(
            os.path.join(project_dir, "sym-lib-table"), "sym", lib,
            kicad_path(lib_path, project_dir))
        tables["fp"] = ensure_lib_table(
            os.path.join(project_dir, "fp-lib-table"), "fp", lib,
            kicad_path(fp_dir, project_dir))

    log(f"KiCad: {lib_path}")
    log(f"  символов добавлено {len(added)}, заменено {len(replaced)}; "
        f"посадок {len(mods)}; 3D-моделей {len(models)}")
    for kind, st in tables.items():
        what = "символов" if kind == "sym" else "посадок"
        if st == "added":
            log(f"  библиотека {what} «{lib}» подключена к проекту")
        elif st.startswith("conflict:"):
            log(f"  ВНИМАНИЕ: в таблице {what} уже есть «{lib}», но она "
                f"смотрит в {st[9:]} — таблицу не трогал")
    return {"lib": lib_path, "sym_dir": sym_dir, "fp_dir": fp_dir,
            "model_dir": model_dir, "added": added, "replaced": replaced,
            "footprints": mods, "models": models, "tables": tables,
            "project": find_project_file(project_dir) or project_dir}


def kicad_origin(c: Component) -> Optional[Dict[str, str]]:
    """
    Откуда компонент взят в KiCad -- для предупреждения перед выгрузкой.

    None, если компонент не из KiCad.
    """
    if (getattr(c, "source", "") or "").lower() != "kicad":
        return None
    ref = getattr(c, "source_ref", "") or ""
    lib, _, sym = ref.partition("::")
    fp = (c.params or {}).get("KiCadFootprint", "") or ""
    if not fp and c.footprints:
        fp = c.footprints[0].name
    model = ""
    for f in c.footprints:
        if f.model is not None and f.model.path:
            model = f.model.path
            break
    return {"symbol": f"{os.path.splitext(lib)[0]}:{sym}" if sym else ref,
            "footprint": fp, "model": model}
