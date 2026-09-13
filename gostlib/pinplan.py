"""
Обмен раскладкой выводов с внешней ИИ.

Задача простая на словах и противная на деле: у микросхемы полторы сотни
выводов, и разложить их по УГО осмысленно (питание отдельно, шины
группами, тактовые входы слева) -- работа на полчаса. ИИ делает это за
минуту, но возвращает ответ как умеет: то в ```-заборе, то с
предисловием «Конечно! Вот раскладка:», то с колонками в другом порядке,
то с точкой с запятой вместо запятой.

Поэтому здесь два правила.

1. Отдаём СТРОГИЙ CSV с шапкой и промпт, где сказано ровно, что вернуть.
   CSV, а не Markdown: таблицу в трубах модель норовит «причесать»
   выравниванием, а из CSV причёсывать нечего.
2. Принимаем всё, что похоже на таблицу. Разделитель определяем сами,
   колонки ищем ПО ИМЕНАМ в шапке, забор и болтовню вокруг выбрасываем.
   Строгость -- в проверке содержимого (номера выводов, типы, стороны),
   а не в форме ответа: ругаться на лишнюю пустую строку -- значит
   заставлять человека править ответ руками, а он за этим и не ходил.
"""
from __future__ import annotations

import csv
import io
import os
import re
from collections import Counter
from typing import Dict, List, Optional, Sequence

from .ir import ETYPES, Component, SymPin

COLUMNS = ("number", "name", "etype", "unit", "side", "group")

# Как эти колонки может назвать модель (и как их называем мы сами).
ALIASES: Dict[str, str] = {
    "number": "number", "контакт": "number", "pin": "number",
    "номер": "number", "pin_number": "number", "№": "number",
    "name": "name", "имя": "name", "signal": "name", "сигнал": "name",
    "etype": "etype", "тип": "etype", "type": "etype",
    "electrical": "etype", "electrical_type": "etype",
    "unit": "unit", "секция": "unit", "part": "unit", "section": "unit",
    "side": "side", "сторона": "side",
    "group": "group", "группа": "group",
}

SIDES = ("L", "R", "T", "B")


class PinPlanError(ValueError):
    """Ответ ИИ неполон или содержит недопустимые значения."""


# ------------------------------------------------------------- задание ------
PROMPT = """\
Ты -- инженер-схемотехник. Раздели выводы микросхемы по функциям для
условного графического обозначения (УГО) по ЕСКД.

ГЛАВНОЕ, ЧТО ОТ ТЕБЯ НУЖНО, -- КЛАССИФИКАЦИЯ, А НЕ ГЕОМЕТРИЯ
Про размеры листа и длину столбиков не думай совсем: программа сама
разобьёт длинные стороны на секции и выровняет их. Твоя работа -- сказать,
что это за вывод, в какую группу он входит и в каком порядке идёт внутри
группы. Сделай это тщательно, остальное не твоя забота.

ЧТО СДЕЛАТЬ
1. Каждому выводу назначь группу (group) -- это самое важное поле.
2. Каждому выводу назначь функциональный блок (unit) и сторону (side).
3. Порядок СТРОК внутри группы задаёт порядок выводов сверху вниз.
4. Имя можно причесать (убрать мусор, развернуть сокращение), но смысл
   менять нельзя.

ГРУППЫ -- ОСНОВНОЕ ПРАВИЛО
Одна группа = один интерфейс или одно назначение. Именуй латиницей
коротко и единообразно: PWR, GND, CTRL, CLK, USB0, USB1, SPI0, I2C1,
UART2, SDMMC, EMMC, DDR, ADC, CODEC, GPIO0, GPIO1, ETH, JTAG, NC.

- Никогда не смешивай в одной группе два интерфейса.
- Вывод с несколькими функциями через «/» относи к ПЕРВОЙ, основной:
  `GPIO2_A0/UART0_RX/SPI0_MISO` -- это GPIO2.
- Внутри группы соблюдай естественный порядок номеров: D0, D1, D2 ... D15
  (а не D0, D10, D11) и A0..A7, B0..B7, C0..C7.
- Дифференциальную пару держи рядом: сначала P (DP, +), потом N (DM, -).

ПИТАНИЕ И ЗЕМЛЯ
- Всё питание -- группа PWR, вся земля -- группа GND. Не выдумывай
  PWR_CORE, VDD_DDR и подобное: разные напряжения различаются именем
  вывода, а группа у них одна.
- Земля -- это VSS, GND, AGND, DGND, AVSS, TVSS, EP, тепловой пятак.
  Не клади землю в PWR.
- Секцию для них выдели отдельную -- последнюю по номеру. Сколько там
  выводов, неважно: программа разложит их сама.

СТОРОНЫ И СЕКЦИИ
- Входы, сброс, тактирование, режимы -- слева (L). Выходы,
  двунаправленные, шины, порты -- справа (R).
- Секции (unit) -- по функциональным блокам, а не «чтобы поровну»: ядро
  и порты, аналоговая часть, кодек, память, питание. Меньше десяти секций
  -- нормально; программа при необходимости добавит ещё.
- NC и зарезервированные -- в самую последнюю секцию, группа NC.

ЧТО НЕЛЬЗЯ
- Добавлять или удалять строки. Сколько строк дано -- столько верни.
- Менять значения в колонке number. Это ключ, по нему всё сходится.
- Использовать типы, которых нет в списке ниже.
- Выравнивать стороны «на глаз» и резать группы пополам ради красоты.

ФОРМАТ ОТВЕТА -- ЭТО ВАЖНЕЕ ВСЕГО
Верни ТОЛЬКО CSV: первая строка -- шапка, дальше строки данных.
Разделитель -- запятая. Никакого текста до и после, никаких ```,
никаких пояснений, никаких итогов. Если хочется что-то пояснить --
не надо.

Шапка ровно такая:
number,name,etype,unit,side,group

Значения колонок:
- number -- как в задании, буква в букву;
- name   -- имя вывода;
- etype  -- одно из: {etypes};
- unit   -- целое число секции, 1..26;
- side   -- L, R, T или B;
- group  -- короткое имя группы латиницей (PWR, GND, CTRL, SPI, GPIOA...).

Пример правильного ответа целиком:
number,name,etype,unit,side,group
1,VDD,power,1,L,PWR
2,GND,power,1,L,GND
3,NRST,input,1,L,CTRL
4,SPI_MOSI,io,1,R,SPI

ДАННЫЕ
Компонент: {name}{extra}
Выводов: {count}, секций сейчас: {parts}

{table}
"""


def _cell(value) -> str:
    return (str(value or "").replace("\r", " ").replace("\n", " ").strip())


def format_plan(comp: Component) -> str:
    """Строгий CSV с текущей раскладкой -- то, что уходит в ИИ как данные."""
    source = comp.raw_pins or comp.symbol.pins
    live: Dict[str, SymPin] = {p.number: p for p in comp.symbol.pins}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    for pin in source:
        shown = live.get(pin.number, pin)
        w.writerow([
            _cell(pin.number), _cell(pin.name), _cell(pin.etype),
            max(1, int(shown.unit or pin.unit or 1)),
            _cell(shown.side or pin.side or "L").upper()[:1] or "L",
            _cell((shown.group or "").lstrip("!")),
        ])
    return buf.getvalue()


def build_prompt(comp: Component, datasheet: str = "") -> str:
    """
    Полное задание для ИИ: правила, формат ответа и сами данные.

    `datasheet` -- выжимка из документации; она сильно повышает качество
    имён и группировки, потому что по одному номеру вывода понять, что
    это за сигнал, невозможно.
    """
    source = comp.raw_pins or comp.symbol.pins
    extra = []
    if comp.mpn:
        extra.append(comp.mpn)
    if comp.manufacturer:
        extra.append(comp.manufacturer)
    if comp.description:
        extra.append(comp.description)
    text = PROMPT.format(
        etypes=", ".join(ETYPES),
        name=comp.name or "без имени",
        extra=(" (" + "; ".join(extra) + ")") if extra else "",
        count=len(source),
        parts=max([int(p.unit or 1) for p in source] or [1]),
        table=format_plan(comp),
    )
    ds = (datasheet or "").strip()
    if ds:
        text += ("\nВЫДЕРЖКА ИЗ ДОКУМЕНТАЦИИ (для имён и назначения "
                 "выводов; формат ответа она не меняет)\n" + ds + "\n")
    return text


# ------------------------------------------------------------- разбор -------
def _strip_fences(text: str) -> str:
    """Выбросить ```-заборы: модель ставит их почти всегда."""
    out = []
    for line in (text or "").splitlines():
        if line.strip().startswith("```"):
            continue
        out.append(line)
    return "\n".join(out)


def _delimiter(line: str) -> str:
    """Чем разделены колонки. Считаем по шапке -- там точно есть все."""
    best, n = ",", 0
    for d in (",", ";", "\t", "|"):
        k = line.count(d)
        if k > n:
            best, n = d, k
    return best


def _norm_key(s: str) -> str:
    return re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ_№]", "", (s or "").strip().lower())


def _find_header(lines: List[str]):
    """
    Найти шапку и разобрать её в отображение «колонка -> номер».

    Ищем именно шапку, а не «первую строку с запятыми»: перед таблицей
    модель любит написать пару фраз, и первая строка с запятой запросто
    окажется предложением.
    """
    for i, raw in enumerate(lines):
        line = raw.strip().strip("|")
        if not line:
            continue
        d = _delimiter(line)
        cells = [_norm_key(c) for c in line.split(d)]
        cols = {}
        for j, c in enumerate(cells):
            key = ALIASES.get(c)
            if key and key not in cols:
                cols[key] = j
        if "number" in cols and len(cols) >= 3:
            return i, d, cols
    return -1, ",", {}


def _rows_from(lines: List[str], start: int, d: str, cols: Dict[str, int]):
    body = "\n".join(l.strip().strip("|") for l in lines[start:] if l.strip())
    rdr = csv.reader(io.StringIO(body), delimiter=d,
                     skipinitialspace=True)
    out = []
    for cells in rdr:
        if not cells or not any(c.strip() for c in cells):
            continue
        # строка-разделитель Markdown вида |---|---|
        if all(set(c.strip()) <= set("-: ") and c.strip() for c in cells):
            continue
        # Болтовня вокруг таблицы («Надеюсь, помогло!») в колонки не
        # разбивается -- по числу ячеек её и отсеиваем. Иначе такая
        # строка приезжает как «лишний вывод», и человек ищет ошибку в
        # раскладке, которой там нет.
        if len(cells) < 3:
            continue
        out.append(cells)
    return out


def parse_plan(text: str, known_pins: Sequence[SymPin]) -> List[dict]:
    """
    Разобрать ответ ИИ. Возвращает строки для ``Service.apply_pins``.

    Кидает PinPlanError с ПОНЯТНЫМ списком проблем: человек должен по
    сообщению понять, что именно переспросить у модели.
    """
    lines = _strip_fences(text or "").splitlines()
    hi, d, cols = _find_header(lines)
    if hi < 0:
        raise PinPlanError(
            "В ответе не нашлась таблица с шапкой.\n"
            "Нужна первая строка ровно такая:\n"
            "number,name,etype,unit,side,group\n"
            "и дальше строки данных, без пояснений и без ```.")
    missing_cols = [c for c in ("number", "name", "etype") if c not in cols]
    if missing_cols:
        raise PinPlanError("В шапке нет колонок: " + ", ".join(missing_cols))

    rows: List[dict] = []
    problems: List[str] = []
    for k, cells in enumerate(_rows_from(lines, hi + 1, d, cols), 1):
        def get(key, default=""):
            j = cols.get(key, -1)
            return cells[j].strip() if 0 <= j < len(cells) else default

        number = get("number")
        if not number:
            continue                    # хвостовой мусор вроде пустых строк
        name = get("name")
        etype = get("etype").lower().replace("-", "_").replace(" ", "_")
        if etype in ("bidirectional", "bidir", "inout"):
            etype = "io"
        if etype in ("pwr", "power_in", "power_out"):
            etype = "power"
        if not etype:
            etype = "passive"
        side = (get("side", "L").upper() or "L")[:1]
        group = get("group")
        try:
            unit = int(float(get("unit", "1") or 1))
        except ValueError:
            unit = 0
        if etype not in ETYPES:
            problems.append(f"вывод {number}: неизвестный тип «{etype}»")
        if not 1 <= unit <= 26:
            problems.append(f"вывод {number}: секция должна быть 1..26")
        if side not in SIDES:
            problems.append(f"вывод {number}: сторона должна быть L, R, T "
                            f"или B")
        rows.append({"number": number, "name": name, "etype": etype,
                     "unit": unit, "side": side, "group": group})

    expected = Counter(p.number for p in known_pins)
    actual = Counter(r["number"] for r in rows)
    missing = list((expected - actual).elements())
    extra = list((actual - expected).elements())
    if missing:
        problems.append("ИИ потерял выводы: " + ", ".join(missing[:12])
                        + (" …" if len(missing) > 12 else ""))
    if extra:
        problems.append("лишние или повторяющиеся выводы: "
                        + ", ".join(extra[:12])
                        + (" …" if len(extra) > 12 else ""))
    if not rows and not problems:
        problems.append("таблица пустая")
    if problems:
        raise PinPlanError("\n".join(problems[:20]))
    return rows


# ------------------------------------------------- приведение в порядок -----
#
# Ни одна модель не знает, сколько выводов влезает в столбик на листе, и
# ни одна не удержит в голове, что у BGA-355 двести пятьдесят земель.
# Поэтому логику («это питание, это шина SPI, эти идут подряд») спрашиваем
# у ИИ, а компоновку доводим сами -- иначе результат зависит от того, чей
# сегодня чат, а он не должен.

# Сколько выводов помещается в один столбик. 32 вывода при шаге 100 mil --
# это 3200 mil ≈ 81 мм: секция такой высоты нормально живёт на A3 вместе с
# рамкой и соседями. Больше -- и лист приходится тянуть.
SIDE_CAP = 32

PWR_RE = re.compile(
    r"^(V(CC|DD|BAT|IN|OUT|REF|SS|DDA?|CORE)|A?VDD|AVCC|VBUS|PWR|"
    r"\+?\d+V\d*|DDR_VDD|CORE_VDD|LOGIC_VDD|USB_VDD|OTP_VCC|PLL_AVDD|"
    r"SADC_AVDD|CODEC_AVDD|USB_AVDD|VCCIO\d*)", re.I)
GND_RE = re.compile(
    r"^(GND|A?GND|VSS|VSSA|TVSS|PLL_VSS|CODEC_AVSS|EP|EPAD|PAD|"
    r"THERMAL|DGND|AGND|PGND)", re.I)


def kind_of(row: dict) -> str:
    """'gnd' | 'pwr' | 'sig' -- по имени, а не по типу вывода.

    Тип у земли и питания в источниках один и тот же (`power`), а
    разносить их надо по разным сторонам.
    """
    name = (row.get("name") or "").strip()
    if GND_RE.match(name):
        return "gnd"
    if PWR_RE.match(name) or (row.get("etype") == "power"):
        return "pwr"
    return "sig"


def _natkey(s: str):
    """'B12' -> ('B', 12): чтобы B2 шло перед B12, а не после."""
    m = re.match(r"^([A-Za-z]*)(\d*)(.*)$", (s or "").strip())
    if not m:
        return ((s or ""), 0, "")
    return (m.group(1).upper(), int(m.group(2) or 0), m.group(3))


def tidy(rows: List[dict], cap: int = SIDE_CAP) -> Tuple[List[dict], List[str]]:
    """
    Довести раскладку от ИИ до пригодной.

    Что делаем и почему:

    * **питание и земля -- в свои секции.** Их всегда много, они всегда
      одинаковые, и в одной секции с сигналами они мешают читать схему;
    * **земля -- справа, питание -- слева** в этих секциях: так рисуют
      силовую секцию все, и провода на листе не пересекаются;
    * **не больше `cap` выводов в столбике.** Лишнее уходит в следующую
      секцию того же назначения. Двести пятьдесят земель одной колонкой --
      это два листа A3 в высоту, и никакой промпт этого не предотвратит;
    * **перекос сторон выравнивается.** Модель любит свалить все GPIO
      направо; целые группы переносятся налево, пока стороны не сравняются;
    * **внутри питания и земли -- порядок по имени и номеру.** У сигналов
      порядок строк оставляем как есть: он осмысленный, его модель и
      расставляла.

    Возвращает (строки, что_поправили).
    """
    rows = [dict(r) for r in rows]
    notes: List[str] = []
    if not rows:
        return rows, notes

    sig = [r for r in rows if kind_of(r) == "sig"]
    pwr = [r for r in rows if kind_of(r) == "pwr"]
    gnd = [r for r in rows if kind_of(r) == "gnd"]

    # --- сигнальные секции: сохраняем деление модели, чиним перекос
    units: Dict[int, List[dict]] = {}
    for r in sig:
        units.setdefault(int(r.get("unit", 1) or 1), []).append(r)

    out: List[dict] = []
    unit_no = 0
    for u in sorted(units):
        items = units[u]
        # группы в порядке первого появления -- это и есть порядок модели
        groups: Dict[str, List[dict]] = {}
        for r in items:
            groups.setdefault((r.get("group") or "MISC").strip() or "MISC",
                              []).append(r)
        order = list(groups)
        # Перекос: гоняем группы на менее загруженную сторону, начиная с
        # самых больших -- так меньше групп приходится трогать.
        def side_count(s):
            return sum(len(groups[g]) for g in order
                       if (groups[g][0].get("side") or "L").upper() == s)

        moved = 0
        for g in sorted(order, key=lambda k: -len(groups[k])):
            l, r_ = side_count("L"), side_count("R")
            if abs(l - r_) <= max(2, cap // 8):
                break
            heavy = "L" if l > r_ else "R"
            light = "R" if heavy == "L" else "L"
            grp = groups[g]
            if (grp[0].get("side") or "L").upper() != heavy:
                continue
            # Перенос группы меняет разницу сторон на две её длины,
            # поэтому польза есть только пока группа меньше половины
            # перекоса. Без этого группа из 20 выводов при перекосе 20
            # уезжала целиком, и пустой оставалась уже другая сторона.
            if 2 * len(grp) > abs(l - r_):
                continue
            for r2 in grp:
                r2["side"] = light
            moved += len(grp)
        if moved:
            notes.append(f"секция {u}: перенёс {moved} выводов на "
                         f"другую сторону — стороны были перекошены")

        # Разбиение по вместимости: секция держит cap выводов на сторону.
        per_side: Dict[str, List[str]] = {"L": [], "R": []}
        for g in order:
            per_side[(groups[g][0].get("side") or "L").upper()].append(g)
        # Раскладываем стороны НЕЗАВИСИМО по «корзинам» на cap выводов, а
        # потом сшиваем корзины попарно. Если складывать подряд, левая
        # сторона переполнится первой, начнётся новая секция -- и правая
        # половина каждой секции останется пустой.
        def bins(names):
            out_bins: List[List[str]] = []
            fill = 0
            for g in names:
                n = len(groups[g])
                if not out_bins or (fill and fill + n > cap):
                    out_bins.append([])
                    fill = 0
                out_bins[-1].append(g)
                fill += n
            return out_bins

        bl, br = bins(per_side["L"]), bins(per_side["R"])
        chunks: List[List[Tuple[str, str]]] = []
        for i in range(max(len(bl), len(br))):
            ch: List[Tuple[str, str]] = []
            if i < len(bl):
                ch += [("L", g) for g in bl[i]]
            if i < len(br):
                ch += [("R", g) for g in br[i]]
            chunks.append(ch)
        if len(chunks) > 1:
            notes.append(f"секция {u}: не влезала в столбик, разбил на "
                         f"{len(chunks)}")
        for ch in chunks:
            if not ch:
                continue
            unit_no += 1
            for side, g in ch:
                for r2 in groups[g]:
                    r2["unit"] = unit_no
                    r2["side"] = side
                    out.append(r2)

    # --- силовые секции: питание слева, земля справа, по cap на столбик
    def sort_power(items):
        return sorted(items, key=lambda r: ((r.get("name") or "").upper(),
                                            _natkey(r.get("number", ""))))

    pwr, gnd = sort_power(pwr), sort_power(gnd)
    if pwr or gnd:
        def cut(items, tag):
            return [(items[i:i + cap], tag)
                    for i in range(0, len(items), cap)]

        pb, gb = cut(pwr, "PWR"), cut(gnd, "GND")
        # Пары столбиков: пока есть и питание, и земля -- питание слева,
        # земля справа. Дальше остаток бьётся по два столбика в секцию:
        # у BGA земель бывает вчетверо больше, чем питаний, и держать
        # пустую левую колонку в шести секциях подряд незачем.
        pairs: List[Tuple[list, list]] = []
        while pb and gb:
            pairs.append((pb.pop(0), gb.pop(0)))
        rest = pb + gb
        while rest:
            a = rest.pop(0)
            b = rest.pop(0) if rest else ([], "")
            pairs.append((a, b))
        notes.append(
            f"питание ({len(pwr)}) и земля ({len(gnd)}) вынесены в "
            f"{len(pairs)} отдельн"
            f"{'ую секцию' if len(pairs) == 1 else 'ые секции'}: "
            f"питание слева, земля справа")
        for (litems, ltag), (ritems, rtag) in pairs:
            unit_no += 1
            for r in litems:
                r["unit"], r["side"], r["group"] = unit_no, "L", ltag
                out.append(r)
            for r in ritems:
                r["unit"], r["side"], r["group"] = unit_no, "R", rtag
                out.append(r)

    if unit_no > max(int(r.get("unit", 1) or 1) for r in rows):
        notes.append(f"итого секций: {unit_no}")
    return out, notes


# --------------------------------------------------------- документация -----
PIN_HINT = re.compile(
    r"(pin|вывод|contact|ball|signal|описание|description|function)",
    re.I)


def datasheet_text(path: str, max_pages: int = 40,
                   max_chars: int = 24000) -> str:
    """
    Выжимка из PDF документации для промпта.

    Целиком datasheet в промпт не влезет и не нужен: почти всё в нём --
    электрические характеристики и графики. Нужны страницы с описанием
    выводов, их и оставляем -- по ключевым словам в тексте страницы.

    Читалку берём ту, что есть в системе; ни одной нет -- честно говорим
    об этом, а не молчим.
    """
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)
    pages: List[str] = []
    try:                                    # самый быстрый вариант
        import fitz                         # PyMuPDF
        with fitz.open(path) as doc:
            for i, page in enumerate(doc):
                if i >= max_pages:
                    break
                pages.append(page.get_text())
    except ImportError:
        try:
            from pdfminer.high_level import extract_text
            whole = extract_text(path, maxpages=max_pages)
            pages = whole.split("\f")
        except ImportError:
            raise RuntimeError(
                "Нет модуля для чтения PDF. Установите один из них:\n"
                "    pip install pymupdf\n"
                "или  pip install pdfminer.six\n"
                "Либо скопируйте таблицу выводов из документации текстом.")
    good = [p for p in pages if p and len(PIN_HINT.findall(p)) >= 2]
    text = "\n".join(good or pages)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… (документация обрезана)"
    return text
