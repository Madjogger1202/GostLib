"""
Правила группировки выводов микросхем.

Автоматика делит выводы на питание / управление / входы / выходы по
зашитым спискам имён, и на типовой микросхеме это работает. Но у каждой
серии свои привычки: где-то шину надо держать вместе, где-то
дифференциальную пару нельзя разрывать, где-то тактовые выводы просятся
наверх. Зашивать это в код бессмысленно -- проще дать текст, который
правится руками (или нейронкой) прямо в программе.

Формат -- одна группа на строку:

    имя группы | сторона | шаблоны через запятую

  * сторона -- L (слева), R (справа), T (сверху), B (снизу);
  * шаблоны сравниваются с ИМЕНЕМ вывода без учёта регистра,
    поддерживаются `*` и `?` (как в именах файлов);
  * порядок строк = порядок групп сверху вниз;
  * `#` и всё после него -- примечание;
  * после шаблонов можно добавить `+пара` -- тогда выводы, отличающиеся
    только суффиксом P/N или +/-, встают подряд.

Пример:

    Питание      | L | VDD*, VCC*, AVDD, VBAT, V+
    Земля        | L | GND*, VSS*, AGND, EP, PAD
    Сброс и такт | L | NRST, RESET, XIN, XOUT, OSC*
    USB          | R | USB_D*, D+, D-        +пара
    Интерфейсы   | R | SDA, SCL, MOSI, MISO, SCK, TX, RX

Всё, что не подошло ни под один шаблон, раскладывает прежняя автоматика.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

SIDES = ("L", "R", "T", "B")

# Правила по умолчанию: то же, что делала зашитая автоматика, но теперь
# это видно и правится.
DEFAULT_RULES = """\
# Группы выводов: имя | сторона | шаблоны через запятую
# Сторона: L слева, R справа, T сверху, B снизу.
# Шаблоны сравниваются с именем вывода, * и ? разрешены.
# «+пара» держит P/N (или +/-) рядом.

Питание       | L | VDD*, VCC*, AVDD*, DVDD*, IOVDD*, VBAT, VIN, V+, VREG*
Земля         | L | GND*, VSS*, AGND, DGND, PGND, EP, EPAD, PAD, V-
Сброс и такт  | L | NRST, RESET, ~RESET, RST, XIN, XOUT, OSC*, CLK, XTAL*
Отладка       | L | SWDIO, SWCLK, TCK, TMS, TDI, TDO, TEST*, BOOT*
USB           | R | USB_D*, USB*, D+, D-                        +пара
Интерфейсы    | R | SDA, SCL, MOSI, MISO, SCK, SS, CS*, TX*, RX*, CAN*
"""


@dataclass
class Rule:
    name: str
    side: str = "L"
    patterns: List[str] = field(default_factory=list)
    keep_pairs: bool = False

    def matches(self, pin_name: str) -> bool:
        n = (pin_name or "").strip().upper()
        if not n:
            return False
        return any(fnmatch.fnmatchcase(n, p) for p in self.patterns)


_PAIR_RE = re.compile(r"^(.*?)([PN]|\+|-)$", re.I)


def parse(text: str) -> List[Rule]:
    """Разобрать текст правил. Кривые строки пропускаются молча."""
    out: List[Rule] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        name, side, pats = parts[0], parts[1].upper()[:1], parts[2]
        if side not in SIDES:
            side = "L"
        keep = False
        if "+пара" in pats or "+pair" in pats.lower():
            keep = True
            pats = pats.replace("+пара", "").replace("+pair", "")
        patterns = [p.strip().upper() for p in pats.split(",") if p.strip()]
        if not name or not patterns:
            continue
        out.append(Rule(name=name, side=side, patterns=patterns,
                        keep_pairs=keep))
    return out


def check(text: str) -> List[str]:
    """Замечания к тексту правил -- показать человеку, а не молчать."""
    problems: List[str] = []
    seen = set()
    for i, raw in enumerate((text or "").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            problems.append(f"строка {i}: нужно три части через «|» — "
                            f"имя | сторона | шаблоны")
            continue
        if parts[1].upper()[:1] not in SIDES:
            problems.append(f"строка {i}: сторона «{parts[1]}» — "
                            f"допустимы L, R, T, B")
        if not [p for p in parts[2].split(",") if p.strip()]:
            problems.append(f"строка {i}: не задано ни одного шаблона")
        if parts[0] in seen:
            problems.append(f"строка {i}: группа «{parts[0]}» уже была")
        seen.add(parts[0])
    return problems


def _pair_key(name: str) -> Optional[str]:
    """'USB_DP' -> 'USB_D', 'D-' -> 'D'. None, если это не половина пары."""
    m = _PAIR_RE.match((name or "").strip())
    if not m or not m.group(1):
        return None
    return m.group(1).rstrip("_").upper()


def apply(pins: Sequence, rules: Sequence[Rule]) -> int:
    """
    Разложить выводы по правилам. Возвращает число попавших под правила.

    Группе даётся числовой ранг по порядку строк -- дальше раскладка
    сортирует группы по имени, и порядок правил становится порядком
    блоков сверху вниз. Имя группы начинается с «!»: это метка того, что
    сторону выбрал человек, а не автомат.
    """
    if not rules:
        return 0
    n = 0
    for rank, rule in enumerate(rules):
        # Восклицательный знак впереди -- метка «поставлено правилом».
        # Он же сортируется раньше цифр, поэтому группы из правил идут
        # выше встроенных, а раскладка знает, что эти стороны трогать
        # нельзя (см. symbolgen._balance и build_box).
        group = f"!{rank:02d}_{rule.name}"
        # Вывод, расставленный руками в таблице, правило не трогает:
        # иначе ручной порядок переписывался бы при каждой пересборке.
        hit = [p for p in pins if not p.group
               and not getattr(p, "manual", False)
               and rule.matches(p.name)]
        if not hit:
            continue
        if rule.keep_pairs:
            # половинки пары должны идти подряд: сортируем по общей части,
            # внутри -- P/плюс раньше N/минуса
            def key(p):
                base = _pair_key(p.name) or (p.name or "").upper()
                tail = (p.name or "").strip().upper()[-1:]
                return (base, 0 if tail in ("P", "+") else 1)
            hit.sort(key=key)
        for order, p in enumerate(hit):
            p.side = rule.side
            p.group = group
            p.order = order
            n += 1
    return n


def rules_for(comp, cfg_text: str = "") -> List[Rule]:
    """
    Правила для компонента: свои, если заданы, иначе общие, иначе
    встроенные. Пустой текст -- это «как раньше», а не «без группировки».
    """
    own = (getattr(comp, "pin_rules", "") or "").strip()
    if own:
        return parse(own)
    common = (cfg_text or "").strip()
    if common:
        return parse(common)
    return parse(DEFAULT_RULES)
