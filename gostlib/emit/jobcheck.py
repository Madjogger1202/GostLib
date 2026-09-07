"""
Проверка задания для Altium ровно так, как его читает DelphiScript.

Скрипт внутри Altium разбирает файл посимвольно и молча проглатывает
кривые строки, поэтому дешевле проверить всё здесь: число полей, целость
чисел, парность FP/ENDFP и COMP/ENDCOMP, чистый ASCII.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

# тег -> (минимум полей, имена полей после тега)
SPEC: Dict[str, Tuple[int, str]] = {
    "VER": (2, "версия"),
    "LOG": (2, "путь"),
    "SCHLIB": (2, "путь"),
    "PCBLIB": (2, "путь"),
    "FONT": (2, "шрифт"),
    "INSTALL": (2, "0/1"),
    "FRESH": (2, "0/1"),
    "PINLOC": (2, "0/1"),
    "INTLIB": (2, "0/1"),
    "LIBPKG": (2, "путь"),
    "COLGRAPH": (2, "цвет"),
    "COLTEXT": (2, "цвет"),
    "COLPIN": (2, "цвет"),
    "HIDECMT": (2, "0/1"),
    "VENDOR": (2, "путь"),
    "FP": (4, "имя описание высота"),
    "PAD": (12, "номер x y w h форма поворот слой отверстие металлиз паз [угол_паза]"),
    "TRK": (7, "слой ширина x1 y1 x2 y2"),
    "FARC": (8, "слой ширина cx cy r угол1 угол2"),
    "FTXT": (9, "слой x y высота поворот ширина зеркало текст"),
    "FILL": (6, "слой x1 y1 x2 y2"),
    "BODY": (8, "путь dx dy standoff rx ry rz"),
    "ENDFP": (1, ""),
    "COMP": (6, "имя обозначение описание комментарий частей"),
    "DESIG": (3, "x y"),
    "CMTPOS": (3, "x y"),
    "PARAM": (4, "имя значение скрытый"),
    "FPREF": (2, "имя библиотека"),
    "PIN": (12, "часть номер имя тип x y длина поворот флаги xэл yэл"),
    "SLINE": (7, "часть толщина x1 y1 x2 y2 [цвет]"),
    "SRECT": (8, "часть толщина x1 y1 x2 y2 залит [цвет]"),
    "SARC": (8, "часть толщина cx cy r угол1 угол2"),
    "SELL": (7, "часть толщина cx cy r залит"),
    "SPOLY": (5, "часть толщина залит N ..."),
    "STEXT": (10, "часть x y кегль поворот выключка жирный текст цвет"),
    "ENDCOMP": (1, ""),
    "END": (3, "компонентов посадок"),
}

# в каких полях обязаны быть целые числа (индексы с 1, тег -- поле 1)
INTS: Dict[str, List[int]] = {
    "FP": [4],
    "PAD": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
    "TRK": [2, 3, 4, 5, 6, 7],
    "FARC": [2, 3, 4, 5, 6, 7, 8],
    "FTXT": [2, 3, 4, 5, 6, 7, 8],
    "FILL": [2, 3, 4, 5, 6],
    "BODY": [3, 4, 5, 6, 7, 8],
    "COMP": [6],
    "DESIG": [2, 3],
    "CMTPOS": [2, 3],
    "PARAM": [4],
    "PIN": [2, 5, 6, 7, 8, 9, 10, 11, 12],
    "SLINE": [2, 3, 4, 5, 6, 7, 8],
    "SRECT": [2, 3, 4, 5, 6, 7, 8, 9],
    "SARC": [2, 3, 4, 5, 6, 7, 8],
    "SELL": [2, 3, 4, 5, 6, 7],
    "SPOLY": [2, 3, 4, 5],
    "STEXT": [2, 3, 4, 5, 6, 7, 8, 10],
    "END": [2, 3],
}


def check(text: str) -> List[str]:
    """Вернуть список замечаний. Пустой список -- задание в порядке."""
    problems: List[str] = []
    in_fp = False
    in_comp = False
    n_fp = 0
    n_comp = 0
    n_pad = 0
    n_pin = 0
    comp_name = ""
    pin_pos = {}
    pin_num = {}
    pin_hid = []
    seen_ver = False
    seen_end = False

    for ln, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            raw.encode("cp1251")
        except UnicodeEncodeError:
            problems.append(f"строка {ln}: символы вне CP1251 -- "
                            f"скрипт прочитает мусор")
        f = raw.split("\t")
        tag = f[0]
        if tag not in SPEC:
            problems.append(f"строка {ln}: неизвестный тег '{tag}'")
            continue
        need, names = SPEC[tag]
        if len(f) < need:
            problems.append(f"строка {ln}: {tag} -- полей {len(f)}, "
                            f"нужно {need} ({names})")
            continue
        for idx in INTS.get(tag, []):
            if idx - 1 >= len(f):
                continue
            v = f[idx - 1].strip()
            if v in ("", "-"):
                problems.append(f"строка {ln}: {tag} поле {idx} пустое")
                continue
            try:
                int(v)
            except ValueError:
                problems.append(f"строка {ln}: {tag} поле {idx} = '{v}' "
                                f"-- не целое число")
        if tag == "SPOLY":
            n = int(f[4]) if f[4].strip().lstrip("-").isdigit() else 0
            if len(f) < 5 + 2 * n:
                problems.append(f"строка {ln}: SPOLY обещает {n} точек, "
                                f"а полей хватает на {(len(f) - 5) // 2}")
        if tag == "VER":
            seen_ver = True
            if ln != 1:
                problems.append("VER должна быть первой строкой")
        elif tag == "FP":
            if in_fp:
                problems.append(f"строка {ln}: FP внутри FP -- нет ENDFP")
            if in_comp:
                problems.append(f"строка {ln}: FP внутри COMP")
            in_fp = True
            n_fp += 1
            if not f[1].strip():
                problems.append(f"строка {ln}: у посадки пустое имя")
        elif tag == "ENDFP":
            if not in_fp:
                problems.append(f"строка {ln}: ENDFP без FP")
            in_fp = False
        elif tag == "COMP":
            if in_comp:
                problems.append(f"строка {ln}: COMP внутри COMP -- нет ENDCOMP")
            if in_fp:
                problems.append(f"строка {ln}: COMP внутри FP")
            in_comp = True
            n_comp += 1
            comp_name = f[1].strip()
            pin_pos = {}
            pin_num = {}
            pin_hid = []
            if not f[1].strip():
                problems.append(f"строка {ln}: у компонента пустое имя")
        elif tag == "ENDCOMP":
            if not in_comp:
                problems.append(f"строка {ln}: ENDCOMP без COMP")
            # Альтиум рисует выводы, попавшие в одну точку, друг поверх
            # друга -- на экране остаётся один, и кажется, что часть
            # выводов пропала. То же с одинаковыми обозначениями.
            for (_u, x, y), nums in pin_pos.items():
                if len(nums) > 1:
                    problems.append(
                        f"{comp_name}: выводы {', '.join(nums)} стоят в одной "
                        f"точке ({x}, {y}) -- в Альтиуме будет виден один")
            for (_u, num), cnt in pin_num.items():
                if cnt > 1:
                    problems.append(
                        f"{comp_name}: обозначение вывода '{num}' встречается "
                        f"{cnt} раз -- обозначения должны быть разными")
            if pin_hid:
                problems.append(
                    f"{comp_name}: выводы {', '.join(pin_hid)} помечены "
                    f"скрытыми -- Altium их не рисует и подключиться к ним "
                    f"нельзя")
            in_comp = False
            pin_pos = {}
            pin_num = {}
            pin_hid = []
        elif tag == "PAD":
            n_pad += 1
            if not in_fp:
                problems.append(f"строка {ln}: PAD вне FP")
        elif tag == "PIN":
            n_pin += 1
            if not in_comp:
                problems.append(f"строка {ln}: PIN вне COMP")
            else:
                unit = f[1].strip()
                num = f[2].strip()
                pin_pos.setdefault(
                    (unit, f[5].strip(), f[6].strip()), []).append(num or "?")
                pin_num[(unit, num)] = pin_num.get((unit, num), 0) + 1
                try:
                    if int(f[9]) & 4:
                        pin_hid.append(num or "?")
                except ValueError:
                    pass
        elif tag == "END":
            seen_end = True
            if int(f[1]) != n_comp:
                problems.append(f"END обещает компонентов {f[1]}, "
                                f"а в файле {n_comp}")
            if int(f[2]) != n_fp:
                problems.append(f"END обещает посадок {f[2]}, "
                                f"а в файле {n_fp}")

    if in_fp:
        problems.append("последняя посадка не закрыта ENDFP")
    if in_comp:
        problems.append("последний компонент не закрыт ENDCOMP")
    if not seen_ver:
        problems.append("нет строки VER -- скрипт примет задание за старое")
    if not seen_end:
        problems.append("нет строки END -- задание записалось не до конца")
    return problems


def stats(text: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        tag = raw.split("\t")[0]
        out[tag] = out.get(tag, 0) + 1
    return out


def check_file(path: str) -> List[str]:
    raw = open(path, "rb").read()
    for enc in ("cp1251", "utf-8"):
        try:
            return check(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return ["не удалось прочитать задание ни в CP1251, ни в UTF-8"]
