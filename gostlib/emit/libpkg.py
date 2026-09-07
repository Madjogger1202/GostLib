"""
Пакет библиотек .LibPkg — из него Altium компилирует .IntLib.

Файл обычный ini: раздел [Design] и по разделу на каждый документ. Altium
пишет туда десятки ключей, но для компиляции достаточно путей; остальное
он подставит сам при первом открытии.

Пути пишутся относительными, чтобы папку библиотеки можно было перенести
или положить в git и открыть на другой машине.
"""
from __future__ import annotations

import os
from typing import List, Sequence

DESIGN = [
    ("Version", "1.0"),
    ("HierarchyMode", "0"),
    ("ChannelRoomNamingStyle", "0"),
    ("ChannelDesignatorFormatString", "$Component_$RoomName"),
    ("UseCustomSchematicPageSize", "0"),
]

DOC = [
    ("AnnotationEnabled", "1"),
    ("AnnotateStartValue", "1"),
    ("AnnotationIndexControlEnabled", "0"),
    ("AnnotateSuffix", ""),
    ("AnnotateScope", "All"),
    ("AnnotateOrder", "-1"),
    ("DoLibraryUpdate", "1"),
    ("DoDatabaseUpdate", "1"),
    ("ClassGenCCAutoEnabled", "1"),
    ("ClassGenCCAutoRoomEnabled", "1"),
    ("ClassGenNCAutoScope", "None"),
    ("ClassGenNCAutoScopeSubName", ""),
    ("DItemRevisionGUID", ""),
    ("GenerateClassCluster", "0"),
]


def build(documents: Sequence[str], base_dir: str) -> str:
    """Текст .LibPkg для перечисленных библиотек."""
    lines: List[str] = ["[Design]"]
    for k, v in DESIGN:
        lines.append(f"{k}={v}")
    for i, path in enumerate(documents, 1):
        try:
            rel = os.path.relpath(path, base_dir)
        except ValueError:
            rel = path
        lines.append("")
        lines.append(f"[Document{i}]")
        lines.append(f"DocumentPath={rel}")
        for k, v in DOC:
            lines.append(f"{k}={v}")
    return "\r\n".join(lines) + "\r\n"


def write(path: str, documents: Sequence[str]) -> str:
    base = os.path.dirname(os.path.abspath(path))
    os.makedirs(base, exist_ok=True)
    text = build([d for d in documents if d], base)
    with open(path, "w", encoding="cp1251", errors="replace", newline="") as f:
        f.write(text)
    return path
