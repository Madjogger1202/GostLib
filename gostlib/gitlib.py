"""
Текстовое зеркало библиотеки -- то, что кладётся в git.

Каталог GostLib живёт в SQLite. Для одного человека это удобно, а для
отдела -- нет: бинарный файл нельзя ни слить, ни посмотреть в pull
request, ни разложить по веткам. Поэтому библиотека умеет выкладываться
в папку обычных текстовых файлов и читаться обратно.

Устройство папки:

    <корень>/
        library.tsv                 -- оглавление: по строке на компонент
        components/<имя>.json       -- сам компонент, по файлу на штуку
        models/<имя>.step           -- 3D-модели (по желанию)
        .gitattributes              -- чтобы git не портил переводы строк

Главное требование -- УСТОЙЧИВОСТЬ: одна и та же библиотека обязана
давать байт в байт одинаковые файлы. Иначе каждая выгрузка -- это diff
на всю библиотеку, и смысл теряется. Отсюда:

  * ключи в JSON отсортированы, отступ фиксированный;
  * переводы строк всегда LF, кодировка UTF-8 без BOM;
  * никаких дат выгрузки внутри файлов;
  * имя файла считается от имени компонента, а не от порядка в базе.

Слияние идёт по uid: он не меняется за всю жизнь компонента, а имя
поменяться может.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

from .ir import Component

INDEX = "library.tsv"
COMPONENTS = "components"
MODELS = "models"

INDEX_HEAD = ("uid", "имя", "тип", "обозначение", "производитель", "MPN",
              "значение", "посадка", "файл")

GITATTRIBUTES = (
    "# GostLib: текстовое зеркало библиотеки\n"
    "*.json text eol=lf\n"
    "*.tsv  text eol=lf\n"
    "*.step text eol=lf\n"
)


def slug(name: str) -> str:
    """Имя файла из имени компонента: без экзотики, но узнаваемое."""
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._+-]+", "_", (name or "").strip())
    s = s.strip("._-") or "component"
    return s[:80]


def _dump(c: Component) -> str:
    """Компонент -> устойчивый JSON."""
    return json.dumps(c.to_dict(), ensure_ascii=False, indent=1,
                      sort_keys=True) + "\n"


def _write(path: str, text: str) -> bool:
    """
    Записать, если содержимое изменилось. Возвращает True, если файл
    тронут.

    Проверка перед записью нужна не ради скорости: git смотрит на
    содержимое, а вот на mtime смотрят синхронизаторы облачных папок.
    Лишний раз трогать файл -- лишний раз гонять его по сети.
    """
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


def file_names(comps: Iterable[Component]) -> Dict[str, str]:
    """
    uid -> имя файла. Совпавшие имена разводятся хвостом из uid, а не
    порядковым номером: порядок в базе меняется, и файлы бы прыгали.
    """
    out: Dict[str, str] = {}
    used: Dict[str, str] = {}
    for c in sorted(comps, key=lambda x: (x.name or "", x.uid)):
        base = slug(c.name or c.uid)
        if base in used and used[base] != c.uid:
            base = f"{base}__{c.uid[:6]}"
        used[base] = c.uid
        out[c.uid] = base + ".json"
    return out


def export_tree(comps: List[Component], root: str,
                with_models: bool = False, prune: bool = True,
                log=None) -> Dict[str, object]:
    """
    Выложить библиотеку в папку текстовых файлов.

    `prune` убирает файлы компонентов, которых в библиотеке больше нет,
    иначе удаление компонента в git выглядело бы как «ничего не
    изменилось».
    """
    comps = list(comps)
    cdir = os.path.join(root, COMPONENTS)
    os.makedirs(cdir, exist_ok=True)
    names = file_names(comps)
    changed: List[str] = []
    kept = set()

    rows = [ "\t".join(INDEX_HEAD) ]
    for c in sorted(comps, key=lambda x: (x.name or "", x.uid)):
        fn = names[c.uid]
        kept.add(fn)
        if _write(os.path.join(cdir, fn), _dump(c)):
            changed.append(f"{COMPONENTS}/{fn}")
        fp = c.footprints[0].name if c.footprints else ""
        rows.append("\t".join(str(v).replace("\t", " ") for v in (
            c.uid, c.name, c.ctype, c.designator, c.manufacturer, c.mpn,
            c.value, fp, fn)))
    if _write(os.path.join(root, INDEX), "\n".join(rows) + "\n"):
        changed.append(INDEX)
    if _write(os.path.join(root, ".gitattributes"), GITATTRIBUTES):
        changed.append(".gitattributes")

    removed: List[str] = []
    if prune and os.path.isdir(cdir):
        for fn in sorted(os.listdir(cdir)):
            if fn.endswith(".json") and fn not in kept:
                try:
                    os.remove(os.path.join(cdir, fn))
                    removed.append(f"{COMPONENTS}/{fn}")
                except OSError:
                    pass

    models: List[str] = []
    if with_models:
        mdir = os.path.join(root, MODELS)
        os.makedirs(mdir, exist_ok=True)
        for c in comps:
            for fp in (c.footprints or []):
                m = getattr(fp, "model", None)
                src = getattr(m, "path", "") if m else ""
                if not src or not os.path.isfile(src):
                    continue
                dst = os.path.join(mdir, os.path.basename(src))
                try:
                    same = (os.path.isfile(dst)
                            and os.path.getsize(dst) == os.path.getsize(src))
                    if not same:
                        with open(src, "rb") as a, open(dst, "wb") as b:
                            b.write(a.read())
                        models.append(f"{MODELS}/{os.path.basename(src)}")
                except OSError as e:
                    if log:
                        log(f"  модель не скопировалась: {src}: {e}")
    if log:
        log(f"Выложено компонентов: {len(comps)}; изменено файлов: "
            f"{len(changed)}; удалено: {len(removed)}"
            + (f"; моделей: {len(models)}" if with_models else ""))
    return {"total": len(comps), "changed": changed, "removed": removed,
            "models": models, "root": root}


def read_tree(root: str) -> List[Component]:
    """Прочитать папку обратно в компоненты. Битые файлы пропускаются."""
    cdir = os.path.join(root, COMPONENTS)
    out: List[Component] = []
    if not os.path.isdir(cdir):
        return out
    for fn in sorted(os.listdir(cdir)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(cdir, fn), encoding="utf-8") as f:
                out.append(Component.from_dict(json.load(f)))
        except Exception:                                   # noqa: BLE001
            continue
    return out


def diff(local: List[Component], remote: List[Component]
         ) -> Dict[str, List[Tuple[str, str]]]:
    """
    Чем папка отличается от библиотеки. Сравнение по содержимому, а не
    по датам: даты у файлов из git всё равно ничего не значат -- git их
    не хранит.

    Возвращает списки (uid, имя) по разделам: только_у_нас,
    только_в_папке, расходятся.
    """
    lo = {c.uid: c for c in local}
    ro = {c.uid: c for c in remote}
    ours = [(u, lo[u].name) for u in sorted(lo) if u not in ro]
    theirs = [(u, ro[u].name) for u in sorted(ro) if u not in lo]
    both = [(u, lo[u].name) for u in sorted(set(lo) & set(ro))
            if _dump(lo[u]) != _dump(ro[u])]
    return {"ours": ours, "theirs": theirs, "diff": both}
