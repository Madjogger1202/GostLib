"""
Правила группировки выводов микросхем.

Автоматика делит выводы на питание / управление / входы / выходы по
зашитым спискам имён, и на типовой микросхеме это работает. Но у каждой
серии свои привычки: где-то шину надо держать вместе, где-то
дифференциальную пару нельзя разрывать, где-то тактовые выводы просятся
наверх. Зашивать это в код бессмысленно -- проще дать текст, который
правится руками (или нейронкой) прямо в программе.

Формат -- одна группа на строку:

    имя группы | сторона | шаблоны через запятую  [+пара] [+низ]

  * сторона -- L (слева), R (справа), A (куда меньше: программа сама
    выравнивает левую и правую сторону); T и B принимаются, но сверху и
    снизу корпуса выводы пока не рисуются -- они уходят влево и вправо;
  * шаблоны сравниваются с ИМЕНЕМ вывода без учёта регистра; `*` и `?`
    -- как в именах файлов, `[A-K]` -- любая буква из диапазона;
  * если в имени есть альтернативные функции через «/» или пробел
    (PA9/USART1_TX), сравнивается ПЕРВОЕ имя: у микроконтроллера вывод --
    это прежде всего вывод порта. Черта над именем KiCad (~{RESET})
    снимается;
  * порядок строк = порядок групп сверху вниз, и первое подошедшее
    правило забирает вывод;
  * внутри группы выводы идут в порядке шаблонов, а при одном шаблоне --
    по имени, с числами по возрастанию: PA2 раньше PA10;
  * `+пара` -- выводы, отличающиеся только суффиксом P/N или +/-,
    встают подряд; `+низ` -- группа прижимается к низу своей стороны
    (так по ЕСКД ставят землю и неподключённые выводы);
  * `#` и всё после него -- примечание.

Всё, что не подошло ни под один шаблон, раскладывает прежняя автоматика.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

SIDES = ("L", "R", "A", "T", "B")

# Правила по умолчанию. Рассчитаны на микроконтроллеры (STM32, GD32, CH32,
# AT32, SAM, AVR, PIC, nRF, RP2040, ESP32, MSP430) и на типовую обвязку:
# мосты USB-UART, PHY, кодеки, датчики, АЦП, преобразователи, память.
#
# Порядок строк -- это и порядок блоков, и старшинство: земля и питание
# стоят первыми, чтобы их не перехватили общие шаблоны вроде USB*, но
# земля помечена «+низ» и в символе оказывается внизу слева, как по ЕСКД.
DEFAULT_RULES = """\
# Группы выводов: имя | сторона | шаблоны через запятую  [+пара] [+низ]
# Сторона: L слева, R справа, A -- куда меньше (выравнивается само).
# Шаблоны сравниваются с именем вывода (у PA9/USART1_TX -- с PA9);
# * и ? разрешены, [A-K] -- диапазон букв.
# Первое подошедшее правило забирает вывод; порядок строк = порядок блоков.
# «+пара» держит P/N (+/-) рядом, «+низ» прижимает блок к низу стороны.

# ---- левая сторона: питание, управление, входы -------------------------
Земля          | L | GND*, *GND, *_GND*, VSS*, *VSS, *VSSA*, AGND*, DGND*, PGND*, SGND*, EP, EPAD, PAD, EXPOSED*, THERMAL*, TAB, 0V, V-, VEE, SHIELD*   +низ
Не подключены  | R | NC, NC[0-9]*, DNC, N.C., RSVD*, RESERVED*, RFU*, NU, DNU   +низ
Питание        | L | VDD*, *VDD, *VDD_*, *VDD[0-9]*, VCC*, *VCC, *VCC_*, AVCC*, DVCC*, VDDA*, VDDIO*, VCCIO*, VBAT*, VIN*, PVIN*, AVIN*, VSYS*, VCAP*, VDDQ*, VPP, VREG*, V3P3*, V1P8*, V5*, V3V3*, 3V3*, 5V*, V+, VS, VS+, VDH, VDDH*
Опорное        | L | VREF*, *VREF*, REF, REFIN*, REFOUT*, VCOM, VCM*, *_VCM*, BIAS*, IREF*, REXT, *_REXT, RBIAS, ZQ*
Сброс          | L | NRST, *NRST*, RST*, *RSTN, *RESET*, RESETB, MCLR*, POR*, NPOR*, PWRON*, PWR_ON, RUN, EN, EN_*, CHIP_EN, CHIP_PU, CE, PD, SHDN*, SD, STBY*, STANDBY*, SLEEP*, WAKE*, ON, ON/OFF
Тактирование   | L | XIN*, XOUT*, XI, XO, XTAL*, *XTAL*, XTI, XTO, XC[0-9], XL[0-9], OSC*, *OSC_IN*, *OSC_OUT*, *OSC32*, X32*, XC32*, RTC_X*, CLKIN*, CLK_IN*, EXTCLK*, REFCLK_IN, MCO*, CLKOUT*
Режим          | L | BOOT[0-9]*, BOOTSEL, BOOT_SEL, MODE*, TEST*, TM, CFG*, CONF*, STRAP*, ADDR*, ADR*, SA0, SA1, A0_SEL
Отладка        | L | SWDIO, SWCLK, SWO, SWD*, *SWDIO*, *SWCLK*, TCK, TMS, TDI, TDO, TRST*, NTRST, SWC, SWD, JTAG*, *JTCK*, *JTMS*, *JTDI*, *JTDO*, DBG*, UPDI, PDI, PDI_DATA, SWIM, ICSPDAT, ICSPCLK, PGC*, PGD*
Входы          | L | IN, IN+, IN-, +IN*, -IN*, IN[0-9]*, INP*, INN*, INA*, INB*, AIN*, VINP, VINN, SENSE*, ISENSE*, ISNS*, CS+, CS-, FB*, VFB, ADJ, COMP, ILIM*, RT, RT/CLK, SYNC*

# ---- правая сторона: интерфейсы, выходы, порты -------------------------
USB            | R | USB*, *USB_D*, *USB_DP*, *USB_DM*, D+, D-, DP, DM, UD+, UD-, UDP, UDM, VBUS*, *VBUS*, USB_ID, CC1, CC2, SBU1, SBU2   +пара
Ethernet       | R | ETH*, *ETH_*, RMII*, *RMII*, RGMII*, *RGMII*, MII_*, MDC, MDIO, *_MDC, *_MDIO, TXP, TXN, RXP, RXN, TX+, TX-, RX+, RX-, TD+, TD-, RD+, RD-, TXEN, TX_EN, RXER, RX_ER, CRS_DV, *REF_CLK*, REFCLK*   +пара
Радио          | R | ANT*, RF*, *_RF*, RFIO*, LNA*, NFC*, NF[0-9]
MIPI           | R | CSI_*, DSI_*, *MIPI*, *_CSI_*, *_DSI_*   +пара
SD / SDIO      | R | SD_*, SDIO*, *SDIO*, SDMMC*, *SDMMC*, SDC_*, MMC*, CMD, *_CMD, DAT[0-9]*, CDDAT*, SD_D[0-9]*, CD, DET*
QSPI / флеш    | R | QSPI*, *QSPI*, OSPI*, *OSPI*, FLASH*, SPIFLASH*, SPI_FLASH*, SFLASH*, *_SIO[0-3], HOLD*, WP*
SPI            | R | MOSI*, *MOSI*, MISO*, *MISO*, SCK*, *_SCK*, SCLK*, *SCLK*, NSS*, *_NSS*, *SPI*, CS, CSN, CS#, NCS, CSB, SS, SSN, CS[0-9]*, *_CS, *_CS[0-9]*, SDI, SDO, SIMO*, SOMI*, CLK, DI, DO, SI, SO
I2C            | R | SDA*, *SDA*, SCL*, *SCL*, *I2C*, TWI*, SMB*, SMBCLK, SMBDAT
UART           | R | TX, RX, TXD*, RXD*, *_TX, *_RX, *_TXD, *_RXD, *UART*, *USART*, *LPUART*, U[0-9]TX*, U[0-9]RX*, RTS*, CTS*, *_RTS, *_CTS, DTR*, DSR*, DCD*, RI
CAN / LIN      | R | CAN*, *CAN_*, *FDCAN*, CANH, CANL, TXCAN, RXCAN, LIN*, TXD_CAN, RXD_CAN   +пара
I2S / аудио    | R | CODEC*, *CODEC*, I2S*, *I2S*, BCLK*, BCK*, SCLK_I2S, LRCK*, LRCLK*, WS, *_WS, FS, FSYNC*, MCLK*, SDIN*, SDOUT*, DACDAT, ADCDAT, *PDM*, SPDIF*, HPL, HPR, HPOUT*, HP_*, LOUT*, ROUT*, SPK*, MIC*, MOUT*, LINEIN*, LINEOUT*   +пара
DDR            | R | DDR*, *_DDR*, *DDR_*   +пара
АЦП / ЦАП      | R | ADC*, *ADC*, AN[0-9]*, AI[0-9]*, *_AIN*, DAC*, *DAC*, AOUT*
ШИМ / таймеры  | R | PWM*, *PWM*, TIM[0-9]*, *TIM[0-9]*, CCP*, ENC*, QEI*, CAPTURE*
Состояние      | R | INT, INT[0-9]*, INTN, NINT, *_INT, IRQ*, *IRQ*, DRDY*, RDY*, READY, BUSY*, ALERT*, FAULT*, FLT*, PG, PGOOD*, PWRGD*, POK, CHG*, STAT*, LED*, ACT*, LINK*
Выходы         | R | OUT, OUT[0-9]*, OUT+, OUT-, OUTA*, OUTB*, *_OUT, VOUT*, VO, VO[0-9]*, Q, Q[0-9]*, Y[0-9]*, SW, SW[0-9]*, LX*, PH, BST, BOOT, CB, CBOOT   +пара

# ---- выводы портов: сторона A -- делятся между левой и правой ----------
GPIO           | A | GPIO*, IO[0-9]*, GP[0-9]*, PIO[0-9]*, P[0-9], P[0-9][0-9]
Порт A / 0     | A | PA[0-9]*, RA[0-9]*, P0.*, P0_*, P00*
Порт B / 1     | A | PB[0-9]*, RB[0-9]*, P1.*, P1_*, P01*
Порт C / 2     | A | PC[0-9]*, RC[0-9]*, P2.*, P2_*, P02*
Порт D / 3     | A | PD[0-9]*, RD[0-9]*, P3.*, P3_*, P03*
Порт E / 4     | A | PE[0-9]*, RE[0-9]*, P4.*, P4_*
Порт F / 5     | A | PF[0-9]*, RF[0-9]*, P5.*, P5_*
Порт G / 6     | A | PG[0-9]*, RG[0-9]*, P6.*, P6_*
Порт H / 7     | A | PH[0-9]*, RH[0-9]*, P7.*, P7_*
Порт I / 8     | A | PI[0-9]*, P8.*, P8_*
Порт J–K       | A | PJ[0-9]*, PK[0-9]*, P9.*, P9_*
Адрес          | L | A[0-9], A[0-9][0-9], BA[0-9]*
Управление ОЗУ | L | CK, CK#, CKE*, CAS*, RAS*, WE, WE#, ODT*
Данные         | R | DQ*, DQS*, DM[0-9]*, DML, DMU, D[0-9], D[0-9][0-9]   +пара
"""

# Прежние встроенные правила. Кто хоть раз нажал «Применить», тот
# сохранил их в настройки -- и навсегда остался бы на старом скудном
# наборе. Такой текст считаем «встроенным» и подменяем новым.
_LEGACY_DEFAULTS = (
    """\
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
""",
)


def _canon(text: str) -> str:
    """Текст правил без примечаний и пробелов -- для сравнения."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0]
        line = re.sub(r"\s+", "", line)
        if line:
            out.append(line)
    return "\n".join(out)


_LEGACY_CANON = {_canon(t) for t in _LEGACY_DEFAULTS}


def is_legacy_default(text: str) -> bool:
    """Это прежние встроенные правила, просто сохранённые в настройки?"""
    c = _canon(text)
    return bool(c) and c in _LEGACY_CANON


@dataclass
class Rule:
    name: str
    side: str = "L"
    patterns: List[str] = field(default_factory=list)
    keep_pairs: bool = False
    bottom: bool = False

    def match_index(self, pin_name: str) -> int:
        """Номер первого подошедшего шаблона или -1."""
        key = pin_key(pin_name)
        if not key:
            return -1
        for i, p in enumerate(self.patterns):
            if fnmatch.fnmatchcase(key, p):
                return i
        return -1

    def matches(self, pin_name: str) -> bool:
        return self.match_index(pin_name) >= 0


_PAIR_RE = re.compile(r"^(.*?)([PN]|\+|-)$", re.I)
_SEP_RE = re.compile(r"[/\s,(|;]")


def pin_key(name: str) -> str:
    """
    Имя вывода в том виде, в каком его сравнивают с шаблонами.

    Верхний регистр, без черты KiCad (~{RESET} -> RESET) и без хвостового
    «#»; если в имени перечислены альтернативные функции (PA9/USART1_TX,
    «IO21 (SDA)»), берётся первое имя.
    """
    n = (name or "").strip().upper()
    if not n:
        return ""
    n = n.replace("~{", "").replace("{", "").replace("}", "")
    n = n.replace("N/C", "NC").replace("ON/OFF", "ON")
    first = _SEP_RE.split(n, 1)[0].strip()
    n = first or n
    n = n.lstrip("~!")
    if len(n) > 1:
        n = n.rstrip("#")
    return n


def natural_key(name: str) -> Tuple:
    """PA2 раньше PA10: числа в имени сравниваются как числа."""
    parts = re.split(r"(\d+)", pin_key(name) or "")
    return tuple(int(p) if p.isdigit() else p for p in parts)


def parse(text: str) -> List[Rule]:
    """Разобрать текст правил. Кривые строки пропускаются молча."""
    out: List[Rule] = []
    for raw in (text or "").splitlines():
        # «#» внутри шаблона (CS#) -- не примечание: примечание начинается
        # с «#» в начале строки или после пробела
        if raw.lstrip().startswith("#"):
            continue
        line = re.split(r"\s#", raw, 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        name, side, pats = parts[0], parts[1].upper()[:1], "|".join(parts[2:])
        if side not in SIDES:
            side = "L"
        flags = pats.lower()
        keep = "+пара" in flags or "+pair" in flags
        bottom = "+низ" in flags or "+bottom" in flags
        for f in ("+пара", "+pair", "+низ", "+bottom"):
            pats = re.sub(re.escape(f), "", pats, flags=re.I)
        patterns = [p.strip().upper() for p in pats.split(",") if p.strip()]
        if not name or not patterns:
            continue
        out.append(Rule(name=name, side=side, patterns=patterns,
                        keep_pairs=keep, bottom=bottom))
    return out


def check(text: str) -> List[str]:
    """Замечания к тексту правил -- показать человеку, а не молчать."""
    problems: List[str] = []
    seen = set()
    for i, raw in enumerate((text or "").splitlines(), 1):
        if raw.lstrip().startswith("#"):
            continue
        line = re.split(r"\s#", raw, 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            problems.append(f"строка {i}: нужно три части через «|» — "
                            f"имя | сторона | шаблоны")
            continue
        if parts[1].upper()[:1] not in SIDES:
            problems.append(f"строка {i}: сторона «{parts[1]}» — "
                            f"допустимы L, R, A (и T, B)")
        if not [p for p in parts[2].split(",") if p.strip()
                and not p.strip().lower().startswith("+")]:
            problems.append(f"строка {i}: не задано ни одного шаблона")
        if parts[0] in seen:
            problems.append(f"строка {i}: группа «{parts[0]}» уже была")
        seen.add(parts[0])
    return problems


def _pair_key(name: str) -> Optional[str]:
    """'USB_DP' -> 'USB_D', 'D-' -> 'D'. None, если это не половина пары."""
    m = _PAIR_RE.match(pin_key(name))
    if not m or not m.group(1):
        return None
    return m.group(1).rstrip("_").upper()


def group_name(rank: int, rule: Rule) -> str:
    """
    Имя группы для раскладки.

    «!» -- сторону выбрал человек, раскладка её не пересматривает; «~» --
    сторона «A»: группу разрешено перенести туда, где выводов меньше.
    «!~» прижимает группу к низу стороны: тильда сортируется после цифр.
    """
    if rule.side == "A":
        return f"~{rank:02d}_{rule.name}"
    if rule.bottom:
        return f"!~{rank:02d}_{rule.name}"
    return f"!{rank:02d}_{rule.name}"


def apply(pins: Sequence, rules: Sequence[Rule]) -> int:
    """
    Разложить выводы по правилам. Возвращает число попавших под правила.

    Группе даётся числовой ранг по порядку строк -- дальше раскладка
    сортирует группы по имени, и порядок правил становится порядком
    блоков сверху вниз.
    """
    if not rules:
        return 0
    n = 0
    for rank, rule in enumerate(rules):
        group = group_name(rank, rule)
        # Вывод, расставленный руками в таблице, правило не трогает:
        # иначе ручной порядок переписывался бы при каждой пересборке.
        hit = []
        for p in pins:
            if p.group or getattr(p, "manual", False):
                continue
            k = rule.match_index(p.name)
            if k >= 0:
                hit.append((k, p))
        if not hit:
            continue
        if rule.keep_pairs:
            # половинки пары подряд: по общей части, внутри -- P/плюс раньше
            def key(kp):
                k, p = kp
                base = _pair_key(p.name) or pin_key(p.name)
                tail = pin_key(p.name)[-1:]
                return (k, natural_key(base),
                        0 if tail in ("P", "+") else 1)
        else:
            # порядок шаблонов, внутри шаблона -- по имени с числами
            def key(kp):
                return (kp[0], natural_key(kp[1].name))
        hit.sort(key=key)
        side = rule.side if rule.side in ("L", "R") else \
            {"T": "L", "B": "R", "A": "R"}[rule.side]
        for order, (_k, p) in enumerate(hit):
            p.side = side
            p.group = group
            p.order = order
            n += 1
    return n


def explain(pins: Sequence, rules: Sequence[Rule]
            ) -> Tuple[List[Tuple[str, str, List[str]]], List[str]]:
    """
    Что под какое правило попало -- без изменения самих выводов.

    Возвращает ([(группа, сторона, [имена])], [не попавшие ни под одно]).
    Нужна окну правил: сразу видно, какие выводы остались автоматике.
    """
    class _P:
        __slots__ = ("name", "group", "side", "order", "manual")

        def __init__(self, name):
            self.name, self.group, self.side = name, "", ""
            self.order, self.manual = 0, False

    tmp = [_P(getattr(p, "name", "") or "") for p in pins]
    apply(tmp, rules)
    by: Dict[str, List[_P]] = {}
    for p in tmp:
        if p.group:
            by.setdefault(p.group, []).append(p)
    out = []
    for rank, rule in enumerate(rules):
        g = group_name(rank, rule)
        items = sorted(by.get(g, []), key=lambda p: p.order)
        if items:
            out.append((rule.name, rule.side, [p.name for p in items]))
    rest = [p.name for p in tmp if not p.group]
    return out, rest


def rules_for(comp, cfg_text: str = "") -> List[Rule]:
    """
    Правила для компонента: свои, если заданы, иначе общие, иначе
    встроенные. Пустой текст -- это «как раньше», а не «без группировки».
    Прежние встроенные правила, сохранённые в настройки как есть,
    считаются встроенными и заменяются нынешними.
    """
    own = (getattr(comp, "pin_rules", "") or "").strip()
    if own:
        return parse(own)
    common = (cfg_text or "").strip()
    if common and not is_legacy_default(common):
        return parse(common)
    return parse(DEFAULT_RULES)


def effective_text(cfg_text: str = "") -> str:
    """Текст общих правил, который реально действует."""
    common = (cfg_text or "").strip()
    if common and not is_legacy_default(common):
        return common
    return DEFAULT_RULES
