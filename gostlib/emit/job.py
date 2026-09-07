"""
Задание для скрипта внутри Altium.

Библиотеки GostLib пишет сам, поэтому скрипту остаётся только доставка:
закрыть открытые копии, подменить занятые файлы и подключить библиотеки.
Формат простой -- строка на библиотеку:

    LIB<TAB>C:\\...\\GOST_Lib.SchLib

Только ASCII: пути в Windows и так ASCII, а кириллица скрипту не нужна.
"""
from __future__ import annotations

import os
from typing import Iterable, List

VERSION = 2


def write_deploy(path: str, libraries: Iterable[str],
                 log_path: str = "") -> str:
    lines: List[str] = [f"VER\t{VERSION}"]
    if log_path:
        lines.append(f"LOG\t{log_path}")
    seen = set()
    for lib in libraries:
        if not lib:
            continue
        key = os.path.normcase(os.path.abspath(lib))
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"LIB\t{lib}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="ascii", errors="replace",
              newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


def write_pointer(root: str, job_path: str) -> str:
    """Файл-указатель, который читает скрипт внутри Altium."""
    p = os.path.join(root, "current_job.txt")
    os.makedirs(root, exist_ok=True)
    with open(p, "w", encoding="ascii", errors="replace") as f:
        f.write(job_path + "\n")
    return p
