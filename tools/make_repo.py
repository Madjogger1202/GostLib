#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Чистая копия проекта для выкладки в git.

Рабочая папка весит больше гигабайта: `.venv`, `build`, `dist` и кеши. В
репозиторий из этого не нужно ничего -- всё воспроизводится командой. Скрипт
собирает рядом папку только с тем, что действительно коммитится, и печатает,
что и почему выброшено.

Запуск:
    python tools/make_repo.py                  → ..\\GostLib-repo
    python tools/make_repo.py --out D:\\путь    своя папка
    python tools/make_repo.py --zip            плюс архив рядом
    python tools/make_repo.py --list           только показать список, не копировать

Папка назначения ОЧИЩАЕТСЯ, кроме каталога `.git`: так копию можно держать
рабочим клоном и просто пересобирать её после правок.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Каталоги, которых в репозитории не должно быть ни при каких условиях.
SKIP_DIRS = {
    ".venv", "venv", "env", "build", "dist", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".idea", ".vscode",
    "_test",              # результаты проверки кругового преобразования
    "Claude outputs",     # рабочие картинки, не часть проекта
}

SKIP_SUFFIXES = {".pyc", ".pyo", ".bak", ".log", ".spec", ".egg-info"}

SKIP_NAMES = {"Thumbs.db", "desktop.ini", ".DS_Store"}

# Двоичное, которое выглядит как мусор, но обязано быть в репозитории.
MUST_HAVE = [
    "gostlib/templates/empty.PcbLib",   # без него не работает запасной путь
    "gostlib/altium/GostLibBuilder.pas",
    "gostlib/altium/GostLibBuilder.PrjScr",
    "gostlib/gui/gostlib.ico",
    "installer/gostlib.iss",
    "requirements.txt",
    "README.md",
    "LICENSE",
    ".gitignore",
]


def skip(rel: Path) -> str:
    """Причина не брать файл, либо пустая строка."""
    for part in rel.parts[:-1]:
        if part in SKIP_DIRS:
            return f"каталог {part}"
    if rel.name in SKIP_NAMES:
        return "служебный файл системы"
    if rel.suffix.lower() in SKIP_SUFFIXES:
        return f"файл {rel.suffix}"
    if rel.parts and rel.parts[0] in SKIP_DIRS:
        return f"каталог {rel.parts[0]}"
    return ""


def human(n: int) -> str:
    if n > 1 << 20:
        return f"{n / (1 << 20):.1f} МБ"
    if n > 1 << 10:
        return f"{n / (1 << 10):.0f} КБ"
    return f"{n} Б"


def collect():
    take, drop = [], {}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        why = skip(rel)
        if why:
            drop.setdefault(why, []).append(path.stat().st_size)
        else:
            take.append(rel)
    return take, drop


def main() -> int:
    ap = argparse.ArgumentParser(description="Чистая копия проекта для git")
    ap.add_argument("--out", default="", help="папка назначения")
    ap.add_argument("--zip", action="store_true", help="сделать ещё и архив")
    ap.add_argument("--list", action="store_true",
                    help="только показать, что войдёт")
    a = ap.parse_args()

    out = Path(a.out).resolve() if a.out else ROOT.parent / "GostLib-repo"
    take, drop = collect()

    print(f"Проект:  {ROOT}")
    print(f"Копия:   {out}")
    print()
    total = sum((ROOT / r).stat().st_size for r in take)
    print(f"В репозиторий войдёт файлов: {len(take)}, всего {human(total)}")
    print()
    print("Не берём:")
    for why, sizes in sorted(drop.items(), key=lambda kv: -sum(kv[1])):
        print(f"  {why:24} {len(sizes):5} файлов   {human(sum(sizes))}")
    print()

    missing = [m for m in MUST_HAVE if not (ROOT / m).is_file()]
    if missing:
        print("ВНИМАНИЕ, не найдены обязательные файлы:")
        for m in missing:
            print(f"  {m}")
        print()

    if a.list:
        for rel in take:
            print(" ", rel.as_posix())
        return 0

    if out == ROOT:
        print("Папка назначения совпадает с проектом. Отмена.")
        return 1

    # Чистим всё, кроме .git: копию удобно держать рабочим клоном.
    if out.exists():
        for item in out.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                try:
                    item.unlink()
                except OSError:
                    pass
    out.mkdir(parents=True, exist_ok=True)

    for rel in take:
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    print(f"Скопировано файлов: {len(take)}")

    if a.zip:
        zpath = out.parent / (out.name + ".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in take:
                z.write(out / rel, str(Path(out.name) / rel))
        print(f"Архив: {zpath}  ({human(zpath.stat().st_size)})")

    print()
    print("Дальше:")
    print(f"  cd \"{out}\"")
    if (out / ".git").is_dir():
        # копия лежит в готовом клоне -- ни init, ни remote не нужны
        print("  git add -A")
        print('  git commit -m "..."')
        print("  git push")
    else:
        print("  git init")
        print("  git add .")
        print('  git commit -m "GostLib: первый коммит"')
        print("  git branch -M main")
        print("  git remote add origin "
              "https://github.com/<пользователь>/<репозиторий>.git")
        print("  git push -u origin main")
    print()
    print("Готовые сборки (exe, установщик, portable) на GitHub кладут не в "
          "репозиторий, а в Releases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
