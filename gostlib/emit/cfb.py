"""
Запись контейнера Compound File Binary (OLE2) -- того самого, в котором
Altium хранит .SchLib и .PcbLib.

Библиотеки вроде olefile умеют только читать, поэтому формат реализован
здесь: заголовок, FAT, mini-FAT, каталог с красно-чёрным деревом.
Версия 3 (сектор 512 байт), этого достаточно -- Altium пишет такие же.

Использование:
    write_cfb("lib.SchLib", {
        "FileHeader": b"...",
        "Storage":    b"...",
        "RP2040":     {"Data": b"..."},      # вложенное хранилище
    })
"""
from __future__ import annotations

import struct
from typing import Dict, List, Tuple, Union

Tree = Dict[str, Union[bytes, "Tree"]]

SECTOR = 512
MINISECTOR = 64
MINI_CUTOFF = 4096

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF

SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class _Entry:
    __slots__ = ("name", "kind", "data", "children", "index", "start", "size",
                 "left", "right", "child")

    def __init__(self, name: str, kind: int):
        self.name = name
        self.kind = kind            # 1 = storage, 2 = stream, 5 = root
        self.data = b""
        self.children: List["_Entry"] = []
        self.index = -1
        self.start = ENDOFCHAIN
        self.size = 0
        self.left = NOSTREAM
        self.right = NOSTREAM
        self.child = NOSTREAM


def _sort_key(e: _Entry):
    """Порядок имён в каталоге CFB: сначала по длине, потом по верхнему регистру."""
    return (len(e.name), e.name.upper())


def _build_tree(entries: List[_Entry]) -> int:
    """
    Собрать сбалансированное двоичное дерево из отсортированного списка.
    Возвращает индекс корня поддерева (NOSTREAM для пустого).
    """
    if not entries:
        return NOSTREAM
    mid = len(entries) // 2
    node = entries[mid]
    node.left = _build_tree(entries[:mid])
    node.right = _build_tree(entries[mid + 1:])
    return node.index


def _flatten(tree: Tree, root: _Entry, all_entries: List[_Entry]) -> None:
    """Разложить словарь в объекты каталога (порядок = порядок индексов)."""
    kids: List[_Entry] = []
    for name, value in tree.items():
        if isinstance(value, dict):
            e = _Entry(name, 1)
            all_entries.append(e)
            _flatten(value, e, all_entries)
        else:
            e = _Entry(name, 2)
            e.data = bytes(value)
            e.size = len(e.data)
            all_entries.append(e)
        kids.append(e)
    root.children = kids


def _assign_indices(root: _Entry, all_entries: List[_Entry]) -> None:
    for i, e in enumerate([root] + all_entries):
        e.index = i


def _link(root: _Entry) -> None:
    """Проставить left/right/child для всех узлов."""
    stack = [root]
    while stack:
        node = stack.pop()
        kids = sorted(node.children, key=_sort_key)
        node.child = _build_tree(kids) if kids else NOSTREAM
        stack.extend(node.children)


def _pack_name(name: str) -> Tuple[bytes, int]:
    if len(name) > 31:
        name = name[:31]
    raw = name.encode("utf-16-le") + b"\x00\x00"
    return raw.ljust(64, b"\x00"), len(raw)


def write_cfb(path: str, tree: Tree) -> str:
    root = _Entry("Root Entry", 5)
    all_entries: List[_Entry] = []
    _flatten(tree, root, all_entries)
    _assign_indices(root, all_entries)
    _link(root)

    streams = [e for e in all_entries if e.kind == 2]
    big = [e for e in streams if e.size >= MINI_CUTOFF]
    small = [e for e in streams if 0 < e.size < MINI_CUTOFF]

    sectors: List[bytes] = []          # содержимое обычных секторов
    fat: List[int] = []                # запись FAT для каждого сектора

    def alloc_chain(data: bytes) -> int:
        """Разложить данные по секторам, вернуть номер первого."""
        if not data:
            return ENDOFCHAIN
        first = len(sectors)
        n = (len(data) + SECTOR - 1) // SECTOR
        for i in range(n):
            chunk = data[i * SECTOR:(i + 1) * SECTOR]
            sectors.append(chunk.ljust(SECTOR, b"\x00"))
            fat.append(first + i + 1)
        fat[-1] = ENDOFCHAIN
        return first

    # --- большие потоки ---
    for e in big:
        e.start = alloc_chain(e.data)

    # --- мини-поток: маленькие потоки подряд по 64 байта ---
    mini_blob = bytearray()
    minifat: List[int] = []
    for e in small:
        e.start = len(mini_blob) // MINISECTOR
        n = (e.size + MINISECTOR - 1) // MINISECTOR
        base = e.start
        for i in range(n):
            minifat.append(base + i + 1)
        minifat[-1] = ENDOFCHAIN
        chunk = e.data.ljust(n * MINISECTOR, b"\x00")
        mini_blob.extend(chunk)
    for e in streams:
        if e.size == 0:
            e.start = ENDOFCHAIN

    root.size = len(mini_blob)
    root.start = alloc_chain(bytes(mini_blob))

    # --- mini-FAT ---
    if minifat:
        mf = b"".join(struct.pack("<I", v) for v in minifat)
        pad = (-len(mf)) % SECTOR
        mf += b"\xff" * pad
        first_minifat = alloc_chain(mf)
        n_minifat = len(mf) // SECTOR
    else:
        first_minifat = ENDOFCHAIN
        n_minifat = 0

    # --- каталог ---
    dir_entries = [root] + all_entries
    dir_blob = bytearray()
    for e in dir_entries:
        name_raw, name_len = _pack_name(e.name)
        dir_blob += name_raw
        dir_blob += struct.pack("<HBB", name_len, e.kind, 1)   # 1 = чёрный
        dir_blob += struct.pack("<III", e.left, e.right, e.child)
        dir_blob += b"\x00" * 16                                # CLSID
        dir_blob += struct.pack("<I", 0)                        # StateBits
        dir_blob += b"\x00" * 16                                # времена
        dir_blob += struct.pack("<I", e.start if e.kind != 1 else 0)
        dir_blob += struct.pack("<Q", e.size if e.kind != 1 else 0)
    pad = (-len(dir_blob)) % SECTOR
    if pad:
        # незанятые записи каталога должны быть помечены как свободные
        blank = bytearray(b"\x00" * 128)
        struct.pack_into("<HBB", blank, 64, 0, 0, 1)
        struct.pack_into("<III", blank, 68, NOSTREAM, NOSTREAM, NOSTREAM)
        dir_blob += bytes(blank) * (pad // 128)
    first_dir = alloc_chain(bytes(dir_blob))
    n_dir = len(dir_blob) // SECTOR

    # --- FAT: должен описывать и самого себя ---
    n_data = len(sectors)
    n_fat = 1
    while True:
        total = n_data + n_fat
        need = (total + 127) // 128
        if need <= n_fat:
            break
        n_fat = need
    fat_start = n_data
    for i in range(n_fat):
        fat.append(FATSECT)
    total_sectors = n_data + n_fat
    fat_table = list(fat) + [FREESECT] * (n_fat * 128 - len(fat))
    fat_bytes = b"".join(struct.pack("<I", v) for v in fat_table)

    if n_fat > 109:
        raise ValueError("библиотека слишком большая для CFB без DIFAT-секторов")

    # --- заголовок ---
    hdr = bytearray(SECTOR)
    hdr[0:8] = SIGNATURE
    struct.pack_into("<HH", hdr, 24, 62, 3)         # minor, major
    struct.pack_into("<H", hdr, 28, 0xFFFE)         # little endian
    struct.pack_into("<HH", hdr, 30, 9, 6)          # sector/mini shift
    struct.pack_into("<I", hdr, 40, 0)              # число секторов каталога (v3)
    struct.pack_into("<I", hdr, 44, n_fat)
    struct.pack_into("<I", hdr, 48, first_dir)
    struct.pack_into("<I", hdr, 52, 0)              # транзакция
    struct.pack_into("<I", hdr, 56, MINI_CUTOFF)
    struct.pack_into("<I", hdr, 60, first_minifat)
    struct.pack_into("<I", hdr, 64, n_minifat)
    struct.pack_into("<I", hdr, 68, ENDOFCHAIN)     # DIFAT не нужен
    struct.pack_into("<I", hdr, 72, 0)
    for i in range(109):
        v = fat_start + i if i < n_fat else FREESECT
        struct.pack_into("<I", hdr, 76 + i * 4, v)

    with open(path, "wb") as f:
        f.write(bytes(hdr))
        for s in sectors:
            f.write(s)
        f.write(fat_bytes)
    return path
