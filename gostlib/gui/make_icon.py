#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Генератор иконки приложения: рисует gostlib.ico рядом с этим файлом.

Иконка не хранится в репозитории как бинарник -- её проще пересобрать
из кода, чем править в редакторе. Знак условно-графический по ЕСКД:
чёрный прямоугольник корпуса с выводами по бокам на светлом поле.

    python gostlib/gui/make_icon.py

На машине без графической сессии (CI, RDP без рабочего стола) Qt всё
равно нужен «экран», поэтому платформу заранее переключаем на offscreen.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# Ставим до импорта Qt: после создания QGuiApplication переключать поздно.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import (QColor, QGuiApplication, QPainter,  # noqa: E402
                           QPen, QPixmap)

SIZES = (16, 32, 48, 64, 128, 256)

BG = QColor("#f0f2f6")     # светлое поле -- знак читается и на тёмной панели
INK = QColor("#14161c")    # почти чёрный: чистый #000 на мелких размерах
                           # после сглаживания выглядит грязно


def draw(size: int) -> QPixmap:
    """Нарисовать знак в квадрате size x size."""
    pm = QPixmap(size, size)
    pm.fill(BG)
    p = QPainter(pm)
    # На 16-32 px сглаживание размывает однопиксельные линии в серую кашу,
    # поэтому включаем его только на крупных размерах.
    p.setRenderHint(QPainter.Antialiasing, size >= 48)

    bx = round(size * 0.27)
    by = round(size * 0.17)
    bw = round(size * 0.46)
    bh = round(size * 0.66)
    pin_len = round(size * 0.20)

    body_w = max(1, round(size / 16))
    pin_w = max(1, round(size / 22))

    # На 16 px три вывода с каждой стороны сливаются в пятно -- рисуем два.
    n_pins = 2 if size < 32 else 3

    p.setPen(QPen(INK, body_w, Qt.SolidLine, Qt.FlatCap, Qt.MiterJoin))
    p.drawRect(bx, by, bw, bh)

    p.setPen(QPen(INK, pin_w, Qt.SolidLine, Qt.FlatCap))
    for i in range(n_pins):
        # Выводы распределяем по высоте корпуса с отступом от углов,
        # как принято в ЕСКД -- вывод не выходит из угла рамки.
        y = by + round(bh * (i + 1) / (n_pins + 1))
        p.drawLine(bx - pin_len, y, bx, y)
        p.drawLine(bx + bw, y, bx + bw + pin_len, y)
    p.end()
    return pm


def dib_entry(pm: QPixmap) -> bytes:
    """Уложить картинку в BMP-запись формата ICO (32 бита, BGRA).

    Windows понимает и PNG внутри .ico, но классический DIB принимают
    вообще все, включая ресурсный компилятор PyInstaller и старые
    просмотрщики, поэтому пишем именно его.
    """
    src = pm.toImage()
    img = src.convertToFormat(src.Format.Format_ARGB32)
    w, h = img.width(), img.height()

    # BITMAPINFOHEADER: высота удвоена, потому что за цветными данными
    # обязана идти маска прозрачности, даже когда она не нужна.
    xor_size = w * h * 4
    mask_stride = ((w + 31) // 32) * 4
    and_size = mask_stride * h
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0,
                         xor_size + and_size, 0, 0, 0, 0)

    rows = []
    for y in range(h - 1, -1, -1):  # DIB хранит строки снизу вверх
        row = bytearray()
        for x in range(w):
            c = img.pixelColor(x, y)
            row += bytes((c.blue(), c.green(), c.red(), c.alpha()))
        rows.append(bytes(row))
    # Знак непрозрачный целиком, поэтому маска -- сплошные нули.
    return header + b"".join(rows) + b"\x00" * and_size


def build_ico(path: Path) -> Path:
    images = [(s, dib_entry(draw(s))) for s in SIZES]

    # ICONDIR + по 16 байт на каждую запись каталога
    offset = 6 + 16 * len(images)
    directory = bytearray()
    blob = bytearray()
    for size, data in images:
        # 256 в поле размера не помещается в байт -- по спецификации
        # такой размер кодируется нулём.
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                 len(data), offset)
        blob += data
        offset += len(data)

    path.write_bytes(struct.pack("<HHH", 0, 1, len(images))
                     + bytes(directory) + bytes(blob))
    return path


def main() -> int:
    # QPixmap без QGuiApplication не создать -- нужен графический стек Qt.
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    out = Path(__file__).resolve().parent / "gostlib.ico"
    try:
        build_ico(out)
    except OSError as exc:
        print(f"Не удалось записать {out}: {exc}")
        return 1
    print(f"Готово: {out}  ({out.stat().st_size} байт, "
          f"размеры: {', '.join(str(s) for s in SIZES)})")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
