"""Минимальный парсер S-выражений KiCad (.kicad_sym, .kicad_mod, *-lib-table)."""
from __future__ import annotations

from typing import Any, List, Union

Node = Union[str, list]


def parse(text: str) -> list:
    """Разобрать весь файл. Возвращает список верхнеуровневых выражений."""
    i = 0
    n = len(text)
    stack: List[list] = [[]]

    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "(":
            new: list = []
            stack[-1].append(new)
            stack.append(new)
            i += 1
            continue
        if ch == ")":
            if len(stack) > 1:
                stack.pop()
            i += 1
            continue
        if ch == '"':
            i += 1
            buf = []
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r",
                                '"': '"', "\\": "\\"}.get(nxt, nxt))
                    i += 2
                    continue
                if c == '"':
                    i += 1
                    break
                buf.append(c)
                i += 1
            stack[-1].append("".join(buf))
            continue
        # атом
        j = i
        while j < n and text[j] not in ' \t\r\n()"':
            j += 1
        stack[-1].append(text[i:j])
        i = j

    return stack[0]


def parse_file(path: str) -> list:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return parse(f.read())


def name_of(node: Node) -> str:
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return ""


def find_all(node: list, key: str) -> List[list]:
    return [c for c in node if isinstance(c, list) and name_of(c) == key]


def find(node: list, key: str):
    for c in node:
        if isinstance(c, list) and name_of(c) == key:
            return c
    return None


def val(node: list, key: str, default=None, idx: int = 1):
    c = find(node, key)
    if c is None or len(c) <= idx:
        return default
    return c[idx]


def fnum(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def xy(node: list, key: str = "at"):
    c = find(node, key)
    if not c:
        return (0.0, 0.0, 0.0)
    x = fnum(c[1]) if len(c) > 1 else 0.0
    y = fnum(c[2]) if len(c) > 2 else 0.0
    r = fnum(c[3]) if len(c) > 3 else 0.0
    return (x, y, r)
