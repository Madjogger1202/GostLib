"""
Стилевые константы ЕСКД/ГОСТ для генератора символов.

Все размеры -- в милах (1 mil = 0.0254 мм). Сетка Altium -- 100 mil.
5 мм ЕСКД ≈ 197 mil, поэтому вертикальный шаг выводов принят 100 mil
(2.54 мм), а длина вывода -- 300 mil, как принято в Altium-библиотеках.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

PT2MIL = 1000.0 / 72.0          # 1 пункт = 13.889 mil
GRID = 100


def snap(v: float, g: int = GRID) -> int:
    return int(round(v / g) * g)


def snap_up(v: float, g: int = GRID) -> int:
    import math
    return int(math.ceil(v / g) * g)


@dataclass
class Style:
    # --- шрифты ---
    font: str = "GOST type B"
    size_pin: int = 7            # имена выводов
    size_pin_num: int = 6        # номера выводов
    size_desig: int = 10         # позиционное обозначение
    size_type: int = 10          # тип микросхемы в основном поле
    size_table: int = 7          # текст в таблице разъёма
    size_value: int = 10         # номинал

    # --- геометрия ---
    pin_pitch: int = 100
    pin_length: int = 300
    group_gap: int = 100         # зазор между логическими группами выводов
    body_margin: int = 100       # отступ первого вывода от верхней кромки
    text_pad: int = 40           # отступ текста от кромки поля
    num_offset: int = 25         # подъём номера вывода над линией
    min_main_field: int = 300    # минимальная ширина основного поля ИС
    min_side_field: int = 200    # минимальная ширина дополнительного поля

    # --- линии (Altium LineWidth: 0=Smallest,1=Small,2=Medium,3=Large) ---
    lw_body: int = 1
    lw_inner: int = 1
    lw_symbol: int = 1
    # «палки»: пластины конденсатора, катод диода, база транзистора, канал
    # полевика. По ГОСТ 2.728 они той же толщины, что и остальная графика;
    # Medium рядом со Small выглядит втрое жирнее.
    lw_bar: int = 1

    # --- цвета (как их понимает Altium: целое BGR, 0 = чёрный) ---
    color_graphic: int = 0       # корпус, линии полей, графика УГО
    color_text: int = 0          # имена выводов, тип, позиционное обозначение
    color_pin_num: int = 0       # номера выводов
    color_pin: int = 0           # сами линии выводов

    # --- дискретные элементы ---
    # Базовые размеры по ГОСТ 2.728 (резистор 10x4 мм). Множитель
    # passive_scale уменьшает или увеличивает всю двухвыводную графику
    # разом -- на плотной схеме 1.0 часто великовато.
    passive_scale: float = 1.0
    # Кегль текста в превью относительно Altium (см. Config). Только
    # для показа: в задание уходит настоящий кегль.
    preview_font_scale: float = 0.72
    res_w: int = 400             # 10 мм
    res_h: int = 160             # 4 мм
    cap_plate: int = 300
    cap_gap: int = 100
    lead: int = 200              # длина вывода у дискретных элементов

    # --- разъём-таблица ---
    table_header: int = 150
    table_row: int = 100
    table_col_net: int = 500     # колонка «Цепь»
    table_min_col_pin: int = 300  # колонка «Конт.»
    table_head_net: str = "Цепь"
    table_head_pin: str = "Конт."
    table_cell_source: str = "name"   # name | number  -- что писать в «Конт.»
    table_row_lines: bool = False
    table_sort: str = "source"        # source | number -- порядок строк

    # --- прочее ---
    show_power_marks: bool = True     # римские цифры мощности в резисторе
    # Подпись типа/номинала рисуется НЕ графикой, а штатным Comment
    # Altium -- его можно двигать и править на листе, и он попадает в
    # спецификацию. Включать имеет смысл, только если Comment мешает.
    draw_type_label: bool = False
    ic_fields: bool = True            # рисовать доп. поля (вертикальные линии)
    pin_numbers_as_text: bool = True  # номера выводов -- собственный текст
                                      # (гарантирует шрифт ГОСТ)
    show_pin_numbers: bool = True     # показывать номера выводов вообще
    group_gap_rows: int = 1           # пустых строк между группами выводов
    # Ширина имён считается по экранной метрике Altium/GOST type B, корпус
    # округляется до электрической сетки, а боковые поля остаются равными,
    # чтобы центральное поле располагалось строго по оси УГО.
    compact_symbols: bool = True

    # --- размеры дискретной графики с учётом масштаба ---
    def sc(self, v: float) -> int:
        """Размер двухвыводной графики с учётом passive_scale, по сетке."""
        k = self.passive_scale if self.passive_scale > 0 else 1.0
        return snap(v * k, 50) or int(round(v * k))

    @property
    def s_res_w(self) -> int:
        return self.sc(self.res_w)

    @property
    def s_res_h(self) -> int:
        return self.sc(self.res_h)

    @property
    def s_cap_plate(self) -> int:
        return self.sc(self.cap_plate)

    @property
    def s_cap_gap(self) -> int:
        return self.sc(self.cap_gap)

    @property
    def s_lead(self) -> int:
        return self.sc(self.lead)

    # --- сетка и шаг выводов ---
    auto_pitch: bool = True      # поднимать шаг под кегль текста

    def eff_pitch(self) -> int:
        """
        Рабочий шаг выводов: кратен сетке 100 mil (2.54 мм) и такой, чтобы
        имена и номера выводов не наезжали друг на друга. При кегле 17 pt
        строка занимает ~170 mil, и на шаге 100 mil номера сливаются --
        поэтому шаг поднимается автоматически.
        """
        # конфликтуют соседние строки, а не имя с номером: имя рисуется
        # внутри поля, номер -- снаружи над линией вывода
        base = max(GRID, snap_up(self.pin_pitch, GRID))
        if not self.auto_pitch:
            return base
        need = max(self.text_h(self.size_pin), self.text_h(self.size_pin_num))
        return max(base, snap_up(need * 1.05, GRID))

    def with_rules(self, comp, cfg_text: str = "") -> "Style":
        """Стиль, несущий правила группировки для этого компонента."""
        import dataclasses
        from . import pingroups
        st = dataclasses.replace(self)
        st._pin_rules = pingroups.rules_for(comp, cfg_text)
        return st

    def for_component(self, comp) -> "Style":
        """
        Стиль с учётом личных настроек компонента (`Component.style_over`).

        Нужно, чтобы одну пассивку можно было ужать или спрятать у неё
        номера выводов, не трогая общие настройки библиотеки.
        """
        import dataclasses
        over = getattr(comp, "style_over", None) or {}
        keep = {k: v for k, v in over.items()
                if k in Style.__dataclass_fields__}
        if not keep:
            return self
        st = dataclasses.replace(self)
        for k, v in keep.items():
            cur = getattr(st, k)
            try:
                setattr(st, k, type(cur)(v) if cur is not None else v)
            except (TypeError, ValueError):
                setattr(st, k, v)
        return st

    def on_grid(self, v: float, g: int = GRID) -> bool:
        return abs(v - round(v / g) * g) < 1e-6

    def char_w(self, size: int) -> float:
        """Средняя ширина знака шрифта ГОСТ тип Б."""
        # 0.32 em оказалось ровно на границе: самая длинная строка RK3308
        # заходила примерно на полклетки за разделитель. 0.34 переводит
        # такое поле на следующий шаг сетки, не возвращая прежнее раздутие.
        ratio = 0.34 if self.compact_symbols else 0.60
        return size * PT2MIL * ratio

    def text_w(self, s: str, size: int) -> float:
        return len(s or "") * self.char_w(size)

    def text_h(self, size: int) -> float:
        return size * PT2MIL * 0.72


DEFAULT = Style()

# Обозначение мощности резистора по ГОСТ 2.728-74 (внутри прямоугольника).
# Ключ -- мощность в ваттах, значение -- условное обозначение.
POWER_MARKS: Dict[float, str] = {
    0.125: "/",
    0.25: "//",
    0.5: "///",
    1.0: "I",
    2.0: "II",
    5.0: "V",
    10.0: "X",
}

# Имена выводов, которые считаются питанием/землёй
PWR_NAMES = ("VCC", "VDD", "VDDA", "VDDIO", "AVDD", "DVDD", "IOVDD", "VBAT",
             "VIN", "VDDCORE", "VCCIO", "VREF", "VREFP", "VSUP", "V+", "VS",
             "VCORE", "VBUS", "VDDD", "VPP", "VDD33", "VDD18", "USB_VDD",
             "VREG_VOUT", "VREG_VIN", "ADC_AVDD", "PVDD")
GND_NAMES = ("GND", "VSS", "AGND", "DGND", "PGND", "AVSS", "DVSS", "EP",
             "EPAD", "GND1", "GND2", "V-", "VEE", "SGND", "GNDA", "THERMAL",
             "PAD", "EXPOSED_PAD")

CTRL_NAMES = ("RESET", "NRST", "RST", "NRESET", "EN", "ENABLE", "CE", "CS",
              "NCS", "SS", "OE", "WE", "RE", "CLK", "SCK", "SCL", "SDA",
              "MOSI", "MISO", "RX", "TX", "SWDIO", "SWCLK", "TDI", "TDO",
              "TMS", "TCK", "TESTEN", "BOOT", "BOOT0", "MODE", "RUN",
              "XIN", "XOUT", "OSC_IN", "OSC_OUT", "NMI", "WAKE", "INT",
              "IRQ", "ALERT", "TEST", "STBY", "SHDN", "PWRDN")
