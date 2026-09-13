"""
Линтер DelphiScript — ловит то, на чём спотыкается компилятор Altium.

Проверить скрипт можно только внутри Altium, а цикл «поправил → перезапустил
Altium → увидел одно окошко с ошибкой» слишком дорогой. Поэтому здесь
проверяется всё, что можно проверить снаружи.

    python -m gostlib.altium.paslint            # проверить GostLibBuilder.pas
    python -m gostlib.altium.paslint файл.pas
"""
from __future__ import annotations

import os
import re
import sys
from typing import List, Tuple

# Зарезервированные слова Object Pascal
RESERVED = {
    "and", "array", "as", "asm", "begin", "case", "class", "const",
    "constructor", "destructor", "dispinterface", "div", "do", "downto",
    "else", "end", "except", "exports", "file", "finalization", "finally",
    "for", "function", "goto", "if", "implementation", "in", "inherited",
    "initialization", "inline", "interface", "is", "label", "library", "mod",
    "nil", "not", "object", "of", "or", "out", "packed", "procedure",
    "program", "property", "raise", "record", "repeat", "resourcestring",
    "set", "shl", "shr", "string", "then", "threadvar", "to", "try", "type",
    "unit", "until", "uses", "var", "while", "with", "xor",
}

# Операторные слова: их DelphiScript разбирает раньше идентификатора, поэтому
# имя переменной, начинающееся с такого слова, ломает компиляцию
# (`inC := False` читается как `in C := ...` -> «( expected»).
OPERATOR_WORDS = ("in", "is", "as", "or", "to", "of", "do", "div", "mod",
                  "not", "shl", "shr", "xor", "and", "at")

# Типы, которые точно есть в DelphiScript. Всё остальное в объявлениях
# переменных лучше не указывать вовсе — связывание будет поздним.
SAFE_TYPES = {"string", "integer", "boolean", "real", "double", "extended",
              "cardinal", "word", "byte", "char", "tstringlist", "tstrings",
              "variant", "tobject", "tcoord", "tpoint", "tlocation",
              # запись кеша площадки: её нельзя объявить нетипизированной,
              # это структура, а не интерфейс
              "tpadcache"}

OPEN_WORDS = ("begin", "case", "try", "record", "class", "object")

# Члены API Altium, подтверждённые рабочими скриптами из подборки
# community add-ons. DelphiScript связывает их поздно: неизвестное имя
# роняет скрипт уже НА ХОДУ, и Try..Except такое не ловит -- поэтому
# незнакомое имя лучше поймать здесь.
ATTESTED = {
    "Add", "AddFilter_ObjectSet", "AddPCBObject", "AddSchObject",
    "AvailableLibraryCount", "Board", "BotShape", "BotXSize", "BotYSize",
    "CloseDocument", "Color", "Comment", "ComponentCount", "ComponentDescription",
    "Corner", "Count", "Create", "CreatePCBLibComp", "CurrentPartID",
    "DM_OpenProject", "Description", "Designator", "DisplayMode",
    "DoFileSave", "Electrical", "EndAngle", "FirstPCBObject",
    "FirstSchObject", "FontId", "FontManager", "Free", "GetComponent",
    "GetCurrentPCBLibrary", "GetCurrentSchDocument", "GetDocumentByPath",
    "GraphicallyInvalidate", "Height", "HoleSize", "HoleType", "HoleWidth",
    "I_ObjectAddress", "InstallLibrary", "InstalledLibraryPath",
    "IsCurrent", "IsHidden", "IsSolid", "Justification", "Layer",
    "LibReference", "LibraryIterator_Create", "LibraryIterator_Destroy",
    "LineWidth", "LoadFromFile", "Location", "MidShape", "MidXSize",
    "MidYSize", "MirrorFlag", "Mode", "Model", "ModelFactory_FromFilename",
    "ModelName", "ModelType", "Modified", "MoveByXY", "Name",
    "NextPCBObject", "NextSchObject", "OpenDocument", "OpenNewDocument",
    "Orientation", "OwnerPartDisplayMode", "OwnerPartId",
    "PCBObjectFactory", "PartCount", "PinLength", "Plated", "PostProcess",
    "PreProcess", "Radius", "RegisterComponent", "RemoveComponent",
    "RobotManager", "Rotation", "SaveToFile", "SchIterator_Destroy",
    "SchLibIterator_Create", "SchObjectFactory", "SetState",
    "SetState_FilterAll", "SetState_FromModel", "ShowDesignator",
    "ShowDocument", "ShowName", "Size", "StandoffHeight", "StartAngle",
    "Text", "TopShape", "TopXSize", "TopYSize", "Transparent",
    "UninstallLibrary", "Width", "X", "X1", "X1Location", "X2",
    "X2Location", "XCenter", "XLocation", "Y", "Y1", "Y1Location", "Y2",
    "Y2Location", "YCenter", "YLocation",
}

# Задокументированы Altium, но в подборке скриптов не встречаются --
# без них не обойтись.
ATTESTED_EXTRA = {"AddSchComponent", "RemoveSchComponent",
                  # часть API пазов вместе с HoleType/HoleWidth;
                  # присваивается только когда угол ненулевой
                  "HoleRotation",
                  # Кеш площадки -- штатный способ задать зазоры маски и
                  # пасты. Прямое присваивание площадке роняет Altium
                  # (проверено тремя прогонами), а кеш читается целиком,
                  # правится и кладётся обратно. Сами имена полей ниже
                  # разрешены только на кеше: прямое обращение к площадке
                  # ловит список FATAL.
                  "GetState_Cache", "SetState_Cache",
                  "SolderMaskExpansion", "SolderMaskExpansionValid",
                  "PasteMaskExpansion", "PasteMaskExpansionValid"}

# Члены, которые ДОКАЗАННО роняют Altium. Проверено на 26.8.1 тремя
# прогонами: посадка без зазоров собирается целиком, посадка с зазором
# падает на первой же площадке -- Access violation по одному и тому же
# адресу в ScriptingSystem.DLL. Причина по сути: зазор маски у площадки
# разрешается через правила ПЛАТЫ, а в .PcbLib платы нет.
# Ключ -- НАЧАЛО выражения целиком, а не только имя члена: у площадки
# такого свойства нет, а у её кеша (TPadCache) есть, и через кеш всё
# работает. Отличать надо по получателю.
FATAL = {
    "Pad.SolderMaskExpansion": "у объекта площадки такого свойства нет; "
                               "правьте кеш: Cache := Pad.GetState_Cache",
    "Pad.PasteMaskExpansion": "то же самое -- только через кеш площадки",
    "Pad.SolderMaskExpansionValid": "у площадки этого члена нет",
    "Pad.PasteMaskExpansionValid": "у площадки этого члена нет",
}


def strip_code(text: str) -> List[str]:
    """Убрать комментарии и строковые литералы, сохранив разбивку по строкам."""
    out = []
    in_brace = False
    in_paren = False
    for raw in text.split("\n"):
        buf = []
        i = 0
        in_str = False
        while i < len(raw):
            ch = raw[i]
            nxt = raw[i + 1] if i + 1 < len(raw) else ""
            if in_brace:
                if ch == "}":
                    in_brace = False
                i += 1
                continue
            if in_paren:
                if ch == "*" and nxt == ")":
                    in_paren = False
                    i += 2
                    continue
                i += 1
                continue
            if in_str:
                if ch == "'":
                    if nxt == "'":
                        i += 2
                        continue
                    in_str = False
                i += 1
                continue
            if ch == "{":
                in_brace = True
                i += 1
                continue
            if ch == "(" and nxt == "*":
                in_paren = True
                i += 2
                continue
            if ch == "/" and nxt == "/":
                break
            if ch == "'":
                in_str = True
                buf.append("''")
                i += 1
                continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
    return out


def check(text: str) -> List[str]:
    problems: List[str] = []
    lines = text.split("\n")
    code = strip_code(text)

    # --- 1. кавычки и скобки в каждой строке -------------------------------
    for n, raw in enumerate(lines, 1):
        if raw.count("'") % 2:
            # многострочных литералов в Паскале нет
            problems.append(f"строка {n}: нечётное число апострофов")
    depth = 0
    for n, c in enumerate(code, 1):
        depth += c.count("(") - c.count(")")
        if depth < 0:
            problems.append(f"строка {n}: лишняя закрывающая скобка")
            depth = 0
    if depth:
        problems.append(f"скобки не закрыты: остаток {depth}")

    # --- 2. баланс Begin/End ------------------------------------------------
    words = []
    for n, c in enumerate(code, 1):
        for m in re.finditer(r"\b([A-Za-z_]\w*)\b", c):
            w = m.group(1).lower()
            if w in OPEN_WORDS or w == "end":
                words.append((n, w))
    depth = 0
    for n, w in words:
        depth += 1 if w in OPEN_WORDS else -1
        if depth < -1:
            problems.append(f"строка {n}: лишний End")
            depth = -1
    if depth != -1:
        problems.append(f"баланс Begin/End = {depth}, ожидался -1 "
                        f"(последний End. закрывает модуль)")

    # --- 3. имена, начинающиеся с зарезервированного слова -----------------
    declared: List[Tuple[int, str]] = []
    for n, c in enumerate(code, 1):
        # объявления переменных: «имя[, имя] [: Тип];»
        m = re.match(r"^\s{2,}([A-Za-z_][\w, ]*?)\s*(?::\s*([A-Za-z_]\w*))?\s*;\s*$", c)
        if m:
            for nm in m.group(1).split(","):
                nm = nm.strip()
                if nm and nm.lower() not in RESERVED:
                    declared.append((n, nm))
            t = (m.group(2) or "").lower()
            if t and t not in SAFE_TYPES:
                problems.append(
                    f"строка {n}: тип '{m.group(2)}' лучше не указывать — "
                    f"оставьте переменную без типа (позднее связывание)")
        # параметры процедур
        m = re.match(r"^\s*(?:Procedure|Function)\s+\w+\s*\(([^)]*)\)", c, re.I)
        if m:
            for part in m.group(1).split(";"):
                for nm in part.split(":")[0].split(","):
                    nm = nm.strip()
                    if nm and nm.lower() not in RESERVED:
                        declared.append((n, nm))

    seen = set()
    for n, nm in declared:
        low = nm.lower()
        if low in RESERVED:
            problems.append(f"строка {n}: '{nm}' — зарезервированное слово")
            continue
        for w in OPERATOR_WORDS:
            if low.startswith(w) and len(low) > len(w) and (nm, w) not in seen:
                seen.add((nm, w))
                problems.append(
                    f"строка {n}: имя '{nm}' начинается с оператора '{w}' — "
                    f"компилятор прочитает его как оператор и потребует "
                    f"скобку; переименуйте")
                break

    # --- 4. процедуры и функции: объявление до использования ---------------
    procs = {}
    funcs = {}
    for n, c in enumerate(code, 1):
        m = re.match(r"^\s*Procedure\s+([A-Za-z_]\w*)", c, re.I)
        if m:
            procs[m.group(1).lower()] = n
        m = re.match(r"^\s*Function\s+([A-Za-z_]\w*)", c, re.I)
        if m:
            funcs[m.group(1).lower()] = n

    known = set(procs) | set(funcs)
    for n, c in enumerate(code, 1):
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*[(;]", c):
            nm = m.group(1).lower()
            if nm not in known:
                continue
            if re.match(r"^\s*(Procedure|Function)\s", c, re.I):
                continue
            decl = procs.get(nm, funcs.get(nm))
            if decl and decl > n:
                problems.append(f"строка {n}: '{m.group(1)}' вызывается до "
                                f"объявления (объявлено на {decl})")

    # функция, вызванная как процедура: результат некуда девать
    for n, c in enumerate(code, 1):
        m = re.match(r"^\s*([A-Za-z_]\w*)\s*(\([^)]*\))?\s*;\s*$", c)
        if not m:
            continue
        nm = m.group(1).lower()
        if nm in funcs and funcs[nm] < n:
            problems.append(f"строка {n}: функция '{m.group(1)}' вызвана как "
                            f"процедура — сделайте её Procedure или "
                            f"присвойте результат")

    # --- 5. члены API, которых может не быть -------------------------------
    known_api = ATTESTED | ATTESTED_EXTRA
    reported = set()
    for n, c in enumerate(code, 1):
        for m in re.finditer(r"\b[A-Za-z_]\w*\s*\.\s*([A-Za-z_]\w*)", c):
            nm = m.group(1)
            if nm in known_api or nm in reported:
                continue
            reported.add(nm)
            problems.append(
                f"строка {n}: '{nm}' не встречается в рабочих скриптах Altium "
                f"— если такого члена нет, скрипт упадёт на ходу "
                f"(Try..Except это не ловит)")

    # --- 6. кодировка -------------------------------------------------------
    bad = [n for n, c in enumerate(code, 1)
           if any(ord(ch) > 127 for ch in c)]
    if bad:
        problems.append(f"не-ASCII вне комментариев и строк: строки {bad[:5]}")

    # --- 7. Var внутри Begin ------------------------------------------------
    in_body = False
    for n, c in enumerate(code, 1):
        if re.match(r"^\s*Begin\b", c, re.I):
            in_body = True
        if re.match(r"^\s*(Procedure|Function)\b", c, re.I):
            in_body = False
        if in_body and re.match(r"^\s*Var\b", c, re.I):
            problems.append(f"строка {n}: Var внутри Begin — не поддерживается")

    # --- 7b. заведомо смертельные члены --------------------------------------
    for n, c in enumerate(code, 1):
        for expr, why in FATAL.items():
            if re.search(re.escape(expr) + r"\s*:=", c):
                problems.append(
                    f"строка {n}: '{expr}' роняет Altium — {why}")

    # --- 8. вложенные процедуры ---------------------------------------------
    # DelphiScript не даёт вложенной процедуре обращаться к переменным
    # внешней: Altium показывает окно «Can-t access top level variable», и
    # это не компиляция, а падение на ходу. Компилятора у нас нет, ловим
    # здесь: любая процедура с отступом -- уже подозрительна.
    for n, c in enumerate(code, 1):
        m = re.match(r"^(\s+)(Procedure|Function)\s+(\w+)", c, re.I)
        if m and m.group(1).strip() == "":
            problems.append(
                f"строка {n}: вложенная {m.group(2).lower()} '{m.group(3)}' — "
                f"в DelphiScript она не видит переменные внешней процедуры "
                f"(«Can-t access top level variable»); вынесите её на "
                f"верхний уровень, а общие данные — в глобальные Var")

    return problems


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv:
        paths = argv
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        paths = [os.path.join(here, "GostLibBuilder.pas")]
    bad = 0
    for p in paths:
        raw = open(p, "rb").read()
        for enc in ("utf-8", "cp1251"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            print(f"{p}: не удалось прочитать")
            bad += 1
            continue
        problems = check(text)
        print(f"{os.path.basename(p)}: замечаний {len(problems)}")
        for x in problems:
            print("  " + x)
        bad += len(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
