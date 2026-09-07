"""
Классификация компонентов и схема параметров.

CTYPES -- типы компонентов (они же категории в каталоге).
PARAM_SCHEMA -- какие параметры имеет смысл заполнять для каждого типа.
guess_type() -- эвристика определения типа по имени/описанию/пинам.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------- типы --------
# (код, русское имя, префикс позиционного обозначения по ГОСТ 2.710)
CTYPES: List[Tuple[str, str, str]] = [
    ("resistor",     "Резистор",                    "R"),
    ("resistor_var", "Резистор переменный",         "R"),
    ("resistor_net", "Резисторная сборка",          "RN"),
    ("capacitor",    "Конденсатор",                 "C"),
    ("capacitor_pol","Конденсатор полярный",        "C"),
    ("inductor",     "Катушка индуктивности",       "L"),
    ("ferrite",      "Ферритовая бусина",           "L"),
    ("transformer",  "Трансформатор",               "T"),
    ("diode",        "Диод",                        "VD"),
    ("zener",        "Стабилитрон",                 "VD"),
    ("schottky",     "Диод Шоттки",                 "VD"),
    ("tvs",          "Супрессор (TVS)",             "VD"),
    ("led",          "Светодиод",                   "HL"),
    ("bridge",       "Диодный мост",                "VD"),
    ("bjt",          "Транзистор биполярный",       "VT"),
    ("mosfet",       "Транзистор полевой",          "VT"),
    ("igbt",         "IGBT",                        "VT"),
    ("thyristor",    "Тиристор",                    "VS"),
    ("opto",         "Оптопара",                    "U"),
    ("ic",           "Микросхема",                  "DD"),
    ("mcu",          "Микроконтроллер",             "DD"),
    ("memory",       "Память",                      "DD"),
    ("logic",        "Логика",                      "DD"),
    ("opamp",        "Операционный усилитель",      "DA"),
    ("comparator",   "Компаратор",                  "DA"),
    ("adc_dac",      "АЦП/ЦАП",                     "DA"),
    ("ldo",          "Линейный стабилизатор",       "DA"),
    ("dcdc",         "Импульсный преобразователь",  "DA"),
    ("driver",       "Драйвер",                     "DA"),
    ("sensor",       "Датчик",                      "BK"),
    ("module",       "Модуль",                      "A"),
    ("rf",           "СВЧ-компонент",               "Z"),
    ("antenna",      "Антенна",                     "W"),
    ("crystal",      "Кварцевый резонатор",         "ZQ"),
    ("oscillator",   "Генератор",                   "G"),
    ("connector",    "Соединитель",                 "X"),
    ("switch",       "Переключатель",               "SA"),
    ("button",       "Кнопка",                      "SB"),
    ("relay",        "Реле",                        "K"),
    ("fuse",         "Предохранитель",              "FU"),
    ("varistor",     "Варистор",                    "RU"),
    ("battery",      "Батарея",                     "GB"),
    ("buzzer",       "Излучатель звука",            "HA"),
    ("motor",        "Двигатель",                   "M"),
    ("display",      "Индикатор/дисплей",           "HG"),
    ("testpoint",    "Контрольная точка",           "XT"),
    ("mount",        "Крепёж/отверстие",            "MH"),
    ("other",        "Прочее",                      "U"),
]

CTYPE_NAME = {c: n for c, n, _ in CTYPES}
CTYPE_PREFIX = {c: p for c, _, p in CTYPES}
CTYPE_CODES = [c for c, _, _ in CTYPES]

# Как рисовать символ (см. gost/symbolgen.py)
SYMBOL_SHAPE = {
    "resistor": "resistor", "resistor_net": "rnet", "resistor_var": "resistor_var",
    "capacitor": "capacitor", "capacitor_pol": "capacitor_pol",
    "inductor": "inductor", "ferrite": "inductor", "transformer": "box",
    "diode": "diode", "zener": "zener", "schottky": "schottky", "tvs": "tvs",
    "led": "led", "bridge": "box",
    "bjt": "bjt", "mosfet": "mosfet", "igbt": "box", "thyristor": "box",
    "opto": "opto",
    "crystal": "crystal", "oscillator": "box",
    "connector": "connector", "testpoint": "testpoint", "mount": "mount",
    "fuse": "fuse", "varistor": "varistor", "battery": "battery",
    "buzzer": "buzzer", "switch": "switch", "button": "button", "relay": "box",
    "antenna": "antenna", "motor": "box", "display": "box",
}

# -------------------------------------------------------- схема параметров ----
# (ключ, подпись, единица, тип: 'str'|'num'|'enum'|'multi', варианты)
P = lambda k, l, u="", t="str", opts=None: {
    "key": k, "label": l, "unit": u, "type": t, "options": opts or []
}

COMMON_PARAMS = [
    P("Manufacturer", "Производитель"),
    P("MPN", "Парт-номер"),
    P("Value", "Номинал"),
    P("Description", "Описание"),
    P("Datasheet", "Документация"),
    P("Package", "Корпус"),
    P("Mounting", "Монтаж", "", "enum", ["SMD", "THT", "Press-fit", "Панель"]),
    P("PinCount", "Число выводов", "шт", "num"),
    P("Height", "Высота", "мм", "num"),
    P("TempMin", "Т мин", "°C", "num"),
    P("TempMax", "Т макс", "°C", "num"),
    P("LCSC", "LCSC"),
    P("Supplier", "Поставщик"),
    P("SupplierPN", "Парт-номер поставщика"),
    P("RoHS", "RoHS", "", "enum", ["Да", "Нет", "Не указано"]),
    P("Lifecycle", "Статус", "", "enum",
      ["Серийный", "NRND", "EOL", "Preview"]),
    # ЕСКД
    P("Наименование", "Наименование (для спецификации)"),
    P("Обозначение", "Обозначение (децимальный номер)"),
    P("Примечание", "Примечание"),
]

PARAM_SCHEMA: Dict[str, List[dict]] = {
    "resistor": [
        P("Resistance", "Сопротивление", "Ом"),
        P("Tolerance", "Допуск", "%", "enum", ["0.01", "0.1", "0.5", "1", "2", "5", "10"]),
        P("Power", "Мощность", "Вт", "enum",
          ["0.05", "0.0625", "0.1", "0.125", "0.25", "0.5", "1", "2", "3"]),
        P("TCR", "ТКС", "ppm/°C", "num"),
        P("MaxVoltage", "Макс. напряжение", "В", "num"),
        P("Technology", "Технология", "", "enum",
          ["Толстоплёночный", "Тонкоплёночный", "Проволочный", "Металлофольговый", "Шунт"]),
    ],
    "resistor_net": [
        P("Resistance", "Сопротивление", "Ом"),
        P("Tolerance", "Допуск", "%"),
        P("Power", "Мощность на элемент", "Вт"),
        P("Elements", "Число элементов", "шт", "num"),
        P("Topology", "Схема", "", "enum", ["Изолированные", "Общая шина"]),
    ],
    "capacitor": [
        P("Capacitance", "Ёмкость", "Ф"),
        P("Tolerance", "Допуск", "%", "enum", ["1", "2", "5", "10", "20", "-20/+80"]),
        P("VoltageRating", "Напряжение", "В", "num"),
        P("Dielectric", "Диэлектрик", "", "enum",
          ["C0G/NP0", "X5R", "X7R", "X7S", "X8R", "Y5V", "Плёнка", "Слюда"]),
        P("ESR", "ESR", "Ом", "num"),
        P("Type", "Тип", "", "enum", ["Керамический", "Плёночный", "Танталовый",
                                      "Алюминиевый", "Суперконденсатор"]),
    ],
    "capacitor_pol": [
        P("Capacitance", "Ёмкость", "Ф"),
        P("VoltageRating", "Напряжение", "В", "num"),
        P("Tolerance", "Допуск", "%"),
        P("ESR", "ESR", "Ом", "num"),
        P("RippleCurrent", "Ток пульсаций", "А", "num"),
        P("Lifetime", "Ресурс", "ч", "num"),
        P("Type", "Тип", "", "enum", ["Алюминиевый", "Танталовый", "Полимерный"]),
    ],
    "inductor": [
        P("Inductance", "Индуктивность", "Гн"),
        P("Tolerance", "Допуск", "%"),
        P("Isat", "Ток насыщения", "А", "num"),
        P("Irms", "Ток СКЗ", "А", "num"),
        P("DCR", "Сопротивление пост. току", "Ом", "num"),
        P("SRF", "Собств. резонанс", "Гц"),
        P("Shielded", "Экранированная", "", "enum", ["Да", "Нет"]),
    ],
    "ferrite": [
        P("Impedance", "Импеданс @100МГц", "Ом", "num"),
        P("CurrentRating", "Ток", "А", "num"),
        P("DCR", "Сопротивление", "Ом", "num"),
    ],
    "diode": [
        P("Vf", "Прямое падение", "В", "num"),
        P("If", "Прямой ток", "А", "num"),
        P("Vr", "Обратное напряжение", "В", "num"),
        P("Ir", "Обратный ток", "А", "num"),
        P("Trr", "Время восстановления", "с"),
        P("Cj", "Ёмкость перехода", "Ф"),
    ],
    "zener": [
        P("Vz", "Напряжение стабилизации", "В", "num"),
        P("Pd", "Рассеиваемая мощность", "Вт", "num"),
        P("Iz", "Ток стабилизации", "А", "num"),
        P("Zz", "Динамическое сопротивление", "Ом", "num"),
        P("Tolerance", "Допуск", "%"),
    ],
    "tvs": [
        P("Vrwm", "Рабочее напряжение", "В", "num"),
        P("Vbr", "Напряжение пробоя", "В", "num"),
        P("Vc", "Напряжение ограничения", "В", "num"),
        P("Ppp", "Импульсная мощность", "Вт", "num"),
        P("Direction", "Направленность", "", "enum",
          ["Однонаправленный", "Двунаправленный"]),
        P("Cj", "Ёмкость", "Ф"),
    ],
    "led": [
        P("Color", "Цвет", "", "enum",
          ["Красный", "Зелёный", "Синий", "Жёлтый", "Белый", "RGB", "ИК", "УФ"]),
        P("Wavelength", "Длина волны", "нм", "num"),
        P("Vf", "Прямое падение", "В", "num"),
        P("If", "Номинальный ток", "А", "num"),
        P("Iv", "Сила света", "мкд", "num"),
        P("ViewAngle", "Угол обзора", "град", "num"),
    ],
    "bjt": [
        P("Polarity", "Структура", "", "enum", ["NPN", "PNP", "NPN+NPN", "PNP+PNP"]),
        P("Vceo", "Uкэ макс", "В", "num"),
        P("Ic", "Iк макс", "А", "num"),
        P("hFE", "h21э", "", "num"),
        P("Pd", "Рассеиваемая мощность", "Вт", "num"),
        P("Ft", "Граничная частота", "Гц"),
    ],
    "mosfet": [
        P("Channel", "Канал", "", "enum", ["N", "P", "N+N", "P+P", "N+P"]),
        P("Vds", "Uси макс", "В", "num"),
        P("Id", "Iс макс", "А", "num"),
        P("Rds_on", "Rси откр.", "Ом", "num"),
        P("Vgs_th", "Пороговое Uзи", "В", "num"),
        P("Qg", "Заряд затвора", "Кл"),
        P("Pd", "Рассеиваемая мощность", "Вт", "num"),
    ],
    "ic": [
        P("Function", "Функция"),
        P("SupplyMin", "Питание мин", "В", "num"),
        P("SupplyMax", "Питание макс", "В", "num"),
        P("Icc", "Ток потребления", "А", "num"),
        P("Interfaces", "Интерфейсы", "", "multi",
          ["SPI", "I2C", "UART", "USB", "CAN", "CAN-FD", "I2S", "SDIO", "QSPI",
           "Ethernet", "RS-485", "LIN", "1-Wire", "SWD", "JTAG", "PCIe", "MIPI",
           "LVDS", "Parallel"]),
        P("LogicLevel", "Логические уровни", "", "enum",
          ["1.8 В", "2.5 В", "3.3 В", "5 В", "LVDS", "LVCMOS", "TTL"]),
    ],
    "mcu": [
        P("Core", "Ядро", "", "enum",
          ["Cortex-M0", "Cortex-M0+", "Cortex-M3", "Cortex-M4", "Cortex-M7",
           "Cortex-M33", "Cortex-A", "RISC-V", "AVR", "PIC", "8051", "Xtensa"]),
        P("Cores", "Число ядер", "шт", "num"),
        P("ClockMax", "Тактовая частота", "Гц"),
        P("Flash", "Flash", "КБ", "num"),
        P("RAM", "ОЗУ", "КБ", "num"),
        P("GPIO", "Число GPIO", "шт", "num"),
        P("ADC", "АЦП", "", "str"),
        P("DAC", "ЦАП", "", "str"),
        P("Timers", "Таймеры"),
        P("SupplyMin", "Питание мин", "В", "num"),
        P("SupplyMax", "Питание макс", "В", "num"),
        P("Interfaces", "Интерфейсы", "", "multi",
          ["SPI", "I2C", "UART", "USB", "USB-OTG", "CAN", "CAN-FD", "I2S",
           "SDIO", "QSPI", "Ethernet", "RS-485", "1-Wire", "SWD", "JTAG",
           "PIO", "PWM", "DMA", "RTC", "Wi-Fi", "BLE"]),
    ],
    "memory": [
        P("MemType", "Тип памяти", "", "enum",
          ["NOR Flash", "NAND Flash", "EEPROM", "FRAM", "SRAM", "SDRAM", "DDR", "PSRAM"]),
        P("Density", "Объём", "бит"),
        P("Organization", "Организация"),
        P("Interface", "Интерфейс", "", "enum",
          ["SPI", "QSPI", "Octal SPI", "I2C", "Parallel", "ONFI", "eMMC"]),
        P("SpeedMHz", "Частота", "МГц", "num"),
        P("SupplyVoltage", "Питание", "В", "num"),
    ],
    "logic": [
        P("Family", "Серия", "", "enum",
          ["74HC", "74HCT", "74LVC", "74AHC", "74LS", "CD4000", "SN74AUP", "К155", "К561"]),
        P("Function", "Функция"),
        P("Channels", "Число каналов", "шт", "num"),
        P("SupplyMin", "Питание мин", "В", "num"),
        P("SupplyMax", "Питание макс", "В", "num"),
        P("PropDelay", "Задержка", "с"),
    ],
    "opamp": [
        P("Channels", "Число каналов", "шт", "num"),
        P("GBW", "Полоса", "Гц"),
        P("SlewRate", "Скорость нарастания", "В/мкс", "num"),
        P("Vos", "Смещение нуля", "В"),
        P("Ib", "Входной ток", "А"),
        P("SupplyMin", "Питание мин", "В", "num"),
        P("SupplyMax", "Питание макс", "В", "num"),
        P("RailToRail", "Rail-to-Rail", "", "enum", ["Нет", "Вход", "Выход", "Вход+выход"]),
        P("NoiseDensity", "Шум", "нВ/√Гц", "num"),
    ],
    "comparator": [
        P("Channels", "Число каналов", "шт", "num"),
        P("PropDelay", "Задержка", "с"),
        P("OutputType", "Выход", "", "enum", ["Push-Pull", "Открытый сток", "Открытый коллектор"]),
        P("SupplyMin", "Питание мин", "В", "num"),
        P("SupplyMax", "Питание макс", "В", "num"),
        P("Hysteresis", "Гистерезис", "В"),
    ],
    "adc_dac": [
        P("Kind", "Тип", "", "enum", ["АЦП", "ЦАП", "АЦП+ЦАП", "Кодек"]),
        P("Bits", "Разрядность", "бит", "num"),
        P("SampleRate", "Частота выборки", "Гц"),
        P("Channels", "Каналов", "шт", "num"),
        P("Interface", "Интерфейс", "", "enum", ["SPI", "I2C", "Parallel", "LVDS", "I2S"]),
        P("Architecture", "Архитектура", "", "enum",
          ["SAR", "Sigma-Delta", "Pipeline", "Flash", "R-2R"]),
    ],
    "ldo": [
        P("VinMin", "Uвх мин", "В", "num"),
        P("VinMax", "Uвх макс", "В", "num"),
        P("Vout", "Uвых", "В"),
        P("Iout", "Ток нагрузки", "А", "num"),
        P("Dropout", "Падение", "В", "num"),
        P("Iq", "Ток покоя", "А"),
        P("PSRR", "PSRR", "дБ", "num"),
        P("Adjustable", "Регулируемый", "", "enum", ["Да", "Нет"]),
    ],
    "dcdc": [
        P("Topology", "Топология", "", "enum",
          ["Понижающий", "Повышающий", "Инвертирующий", "SEPIC", "Обратноходовой",
           "Прямоходовой", "Полумост", "Мост", "Зарядовый насос"]),
        P("VinMin", "Uвх мин", "В", "num"),
        P("VinMax", "Uвх макс", "В", "num"),
        P("Vout", "Uвых", "В"),
        P("Iout", "Ток нагрузки", "А", "num"),
        P("Fsw", "Частота преобразования", "Гц"),
        P("Efficiency", "КПД", "%", "num"),
        P("Sync", "Синхронный", "", "enum", ["Да", "Нет"]),
        P("IntegratedFET", "Ключ внутри", "", "enum", ["Да", "Нет"]),
    ],
    "driver": [
        P("DriverType", "Тип", "", "enum",
          ["MOSFET/IGBT", "Мотор", "Светодиод", "Реле", "Шаговый", "Полумост", "Мост"]),
        P("Channels", "Каналов", "шт", "num"),
        P("PeakCurrent", "Пиковый ток", "А", "num"),
        P("Vsupply", "Напряжение питания", "В"),
        P("Isolation", "Изоляция", "В", "num"),
    ],
    "sensor": [
        P("Measurand", "Измеряемая величина", "", "enum",
          ["Температура", "Влажность", "Давление", "Ускорение", "Угловая скорость",
           "Магнитное поле", "Ток", "Освещённость", "Газ", "Расстояние", "Влага", "pH"]),
        P("Range", "Диапазон"),
        P("Accuracy", "Погрешность"),
        P("Interface", "Интерфейс", "", "enum",
          ["I2C", "SPI", "UART", "Аналоговый", "PWM", "1-Wire", "CAN"]),
        P("SupplyMin", "Питание мин", "В", "num"),
        P("SupplyMax", "Питание макс", "В", "num"),
        P("Icc", "Потребление", "А"),
    ],
    "module": [
        P("Function", "Назначение"),
        P("Protocols", "Протоколы", "", "multi",
          ["Wi-Fi", "BLE", "Bluetooth", "LoRa", "Zigbee", "Thread", "GSM", "LTE",
           "NB-IoT", "GNSS", "NFC", "UWB", "Sub-GHz"]),
        P("Interface", "Интерфейс к хосту", "", "enum",
          ["UART", "SPI", "I2C", "USB", "SDIO", "Ethernet"]),
        P("SupplyMin", "Питание мин", "В", "num"),
        P("SupplyMax", "Питание макс", "В", "num"),
        P("Antenna", "Антенна", "", "enum", ["Встроенная", "Разъём", "Площадка"]),
        P("TxPower", "Мощность передатчика", "дБм", "num"),
    ],
    "connector": [
        P("Positions", "Число контактов", "шт", "num"),
        P("Rows", "Число рядов", "шт", "num"),
        P("Pitch", "Шаг", "мм", "num"),
        P("Gender", "Тип", "", "enum", ["Вилка", "Розетка", "Гнездо", "Штырь"]),
        P("Orientation", "Ориентация", "", "enum",
          ["Вертикальный", "Угловой", "Кабельный", "Краевой"]),
        P("CurrentRating", "Ток на контакт", "А", "num"),
        P("VoltageRating", "Напряжение", "В", "num"),
        P("MatingCycles", "Число сочленений", "циклов", "num"),
        P("Plating", "Покрытие", "", "enum", ["Золото", "Олово", "Никель", "Серебро"]),
        P("Shielded", "Экранированный", "", "enum", ["Да", "Нет"]),
        P("Locking", "Фиксация", "", "enum", ["Нет", "Защёлка", "Винт", "Байонет"]),
        P("Standard", "Стандарт", "", "enum",
          ["USB-A", "USB-B", "USB-C", "micro-USB", "JST-SH", "JST-PH", "JST-GH",
           "Molex Picoblade", "PLS/PBS 2.54", "IDC", "RJ45", "SMA", "U.FL",
           "DB9", "Barrel Jack", "Terminal Block", "FFC/FPC", "M12", "SD/microSD"]),
    ],
    "crystal": [
        P("Frequency", "Частота", "Гц"),
        P("LoadCap", "Ёмкость нагрузки", "пФ", "num"),
        P("Stability", "Стабильность", "ppm", "num"),
        P("ESR", "ЭПС", "Ом", "num"),
        P("Mode", "Гармоника", "", "enum", ["Основная", "3-я", "5-я"]),
    ],
    "oscillator": [
        P("Frequency", "Частота", "Гц"),
        P("Stability", "Стабильность", "ppm", "num"),
        P("OutputType", "Выход", "", "enum", ["CMOS", "LVDS", "LVPECL", "HCSL", "Синус"]),
        P("SupplyVoltage", "Питание", "В", "num"),
        P("Icc", "Потребление", "А"),
        P("Jitter", "Джиттер", "с"),
    ],
    "switch": [
        P("Poles", "Число полюсов", "шт", "num"),
        P("Throws", "Число положений", "шт", "num"),
        P("SwitchType", "Тип", "", "enum",
          ["Тактовая кнопка", "Движковый", "Клавишный", "Поворотный", "DIP",
           "Энкодер", "Микропереключатель", "Геркон"]),
        P("CurrentRating", "Ток", "А", "num"),
        P("VoltageRating", "Напряжение", "В", "num"),
        P("ActuationForce", "Усилие", "Н", "num"),
        P("Life", "Ресурс", "циклов", "num"),
    ],
    "button": [
        P("SwitchType", "Тип", "", "enum", ["Тактовая", "Мембранная", "Пьезо"]),
        P("CurrentRating", "Ток", "А", "num"),
        P("ActuationForce", "Усилие", "Н", "num"),
        P("Travel", "Ход", "мм", "num"),
        P("Life", "Ресурс", "циклов", "num"),
    ],
    "relay": [
        P("CoilVoltage", "Напряжение катушки", "В", "num"),
        P("CoilResistance", "Сопротивление катушки", "Ом", "num"),
        P("ContactForm", "Контактная группа", "", "enum", ["SPST", "SPDT", "DPST", "DPDT"]),
        P("ContactCurrent", "Ток контактов", "А", "num"),
        P("ContactVoltage", "Напряжение контактов", "В", "num"),
        P("RelayType", "Тип", "", "enum", ["Электромеханическое", "Твердотельное", "Герконовое"]),
    ],
    "fuse": [
        P("CurrentRating", "Номинальный ток", "А", "num"),
        P("HoldCurrent", "Ток удержания", "А", "num"),
        P("TripCurrent", "Ток срабатывания", "А", "num"),
        P("VoltageRating", "Напряжение", "В", "num"),
        P("FuseType", "Тип", "", "enum", ["PPTC", "Быстрый", "Инерционный", "Термо"]),
        P("BreakingCapacity", "Отключающая способность", "А", "num"),
    ],
    "opto": [
        P("Channels", "Каналов", "шт", "num"),
        P("CTR", "Коэф. передачи тока", "%", "num"),
        P("Viso", "Напряжение изоляции", "В", "num"),
        P("OutputType", "Выход", "", "enum",
          ["Транзистор", "Дарлингтон", "Логический", "Симистор", "MOSFET"]),
        P("Speed", "Скорость", "бит/с"),
    ],
    "battery": [
        P("Chemistry", "Химия", "", "enum", ["Li-ion", "LiPo", "LiFePO4", "CR (Li-Mn)", "NiMH", "Alkaline"]),
        P("NominalVoltage", "Напряжение", "В", "num"),
        P("Capacity", "Ёмкость", "мА·ч", "num"),
        P("Rechargeable", "Перезаряжаемая", "", "enum", ["Да", "Нет"]),
    ],
    "display": [
        P("DisplayType", "Тип", "", "enum",
          ["OLED", "TFT LCD", "Монохромный LCD", "E-Paper", "7-сегментный", "Матрица"]),
        P("Resolution", "Разрешение"),
        P("Diagonal", "Диагональ", "дюйм", "num"),
        P("Interface", "Интерфейс", "", "enum", ["SPI", "I2C", "RGB", "MIPI DSI", "Parallel"]),
        P("Controller", "Контроллер"),
    ],
    "antenna": [
        P("FreqRange", "Диапазон частот"),
        P("Gain", "Усиление", "дБи", "num"),
        P("Impedance", "Импеданс", "Ом", "num"),
        P("VSWR", "КСВ", "", "num"),
        P("AntType", "Тип", "", "enum", ["Чип", "PCB", "Штырь", "Внешняя"]),
    ],
    "testpoint": [P("Diameter", "Диаметр", "мм", "num")],
    "mount": [
        P("HoleDiameter", "Диаметр отверстия", "мм", "num"),
        P("ScrewSize", "Крепёж", "", "enum", ["M2", "M2.5", "M3", "M4", "M5", "#4-40"]),
        P("Plated", "Металлизировано", "", "enum", ["Да", "Нет"]),
    ],
}


def params_for(ctype: str) -> List[dict]:
    """Полный список параметров для типа: общие + специфичные."""
    return COMMON_PARAMS + PARAM_SCHEMA.get(ctype, [])


# ------------------------------------------------------------ эвристика -------

# --- 1. Префикс позиционного обозначения (самый надёжный признак) ------------
# EasyEDA/LCSC отдают его в поле c_para.pre, KiCad -- в property Reference.
PREFIX_TYPE = {
    "R": "resistor", "RN": "resistor_net", "RP": "resistor_net",
    "RV": "varistor", "VR": "resistor_var", "POT": "resistor_var",
    "C": "capacitor", "CE": "capacitor_pol", "CP": "capacitor_pol",
    "L": "inductor", "FB": "ferrite", "FL": "ferrite",
    "T": "transformer", "TR": "transformer",
    "D": "diode", "VD": "diode", "ZD": "zener", "TVS": "tvs",
    "LED": "led", "HL": "led", "DS": "display", "LCD": "display",
    "Q": "bjt", "VT": "bjt", "M": "motor",
    "U": "ic", "IC": "ic", "DD": "ic", "DA": "ic", "N": "ic",
    "J": "connector", "JP": "connector", "P": "connector", "CN": "connector",
    "CON": "connector", "X": "connector", "XS": "connector", "XP": "connector",
    "USB": "connector", "HDR": "connector", "SIM": "connector",
    "SW": "switch", "S": "switch", "SA": "switch", "SB": "button",
    "K": "relay", "RLY": "relay", "KL": "relay",
    "F": "fuse", "FU": "fuse", "FS": "fuse",
    "Y": "crystal", "ZQ": "crystal", "XTAL": "crystal", "OSC": "oscillator",
    "G": "oscillator", "BT": "battery", "BAT": "battery", "GB": "battery",
    "LS": "buzzer", "SP": "buzzer", "BZ": "buzzer", "HA": "buzzer",
    "TP": "testpoint", "XT": "testpoint",
    "H": "mount", "MH": "mount", "MK": "mount",
    "E": "antenna", "ANT": "antenna", "W": "antenna",
    "OK": "opto", "ISO": "opto", "OC": "opto",
    "BK": "sensor", "A": "module", "AT": "antenna",
}

# --- 2. Корпус --------------------------------------------------------------
PACKAGE_RULES = [
    (r"^C\d{3,4}(_|$|[A-Z])", "capacitor"),
    (r"^CAP", "capacitor"),
    (r"^(C|CASE)[-_]?(A|B|C|D)\b.*TANT|TANTAL", "capacitor_pol"),
    (r"^(CAP_?)?(SMD|RAD)[-_]?ELEC|ELEC_?CAP|^CE\d", "capacitor_pol"),
    (r"^R\d{3,4}(_|$|[A-Z])|^RES", "resistor"),
    (r"^L\d{3,4}(_|$|[A-Z])|^IND|CDRH|SRN\d|^NR\d", "inductor"),
    (r"^(SOD|DO-\d|SMA\b|SMB\b|SMC\b|MELF)", "diode"),
    (r"^(SOT-?23|SOT-?223|SOT-?89|TO-?\d|DPAK|D2PAK|SOT-?363)", "bjt"),
    (r"(QFN|QFP|LQFP|TQFP|BGA|SOIC|SOP\b|SSOP|TSSOP|MSOP|DFN|SON|SC-?70|"
     r"SOT-?553|WLCSP|LGA|PLCC|DIP-?\d)", "ic"),
    (r"(USB|TYPE-?C|MICRO-?B|JST|XH|PH2|SH1|GH1|ZH1|MOLEX|PICOBLADE|"
     r"HDR|HEADER|PIN-?HEADER|PLS|PBS|IDC|RJ-?\d|DC-?JACK|BARREL|"
     r"TERMINAL|KF\d|SCREW|SOCKET|FFC|FPC|CARD|SD_|TF_|SMA_|IPEX|U\.?FL|"
     r"D-?SUB|DB-?\d|M12|BANANA)", "connector"),
    (r"(HC-?49|ABM\d|SMD3225|SMD5032|SMD2016|SMD7050|CRYSTAL|OSC\d)", "crystal"),
    (r"(LED|WS281|SK681|PLCC-?[246])", "led"),
    (r"(SW-?|TACT|KEY|BUTTON|DIP-?SW)", "switch"),
    (r"(RELAY|HK\d|SRD-)", "relay"),
    (r"(BUZZER|SPEAKER)", "buzzer"),
    (r"(BATTERY|CR20\d\d|HOLDER)", "battery"),
    (r"(ANTENNA|ANT-?\d)", "antenna"),
    (r"(TESTPOINT|TP-?\d)", "testpoint"),
    (r"(MOUNT|HOLE|M2|M2\.5|M3\b)", "mount"),
]
_PKG_COMPILED = [(re.compile(rx, re.I), t) for rx, t in PACKAGE_RULES]

# Типы, которые стоит уточнить текстовыми правилами, даже если префикс их дал
_COARSE = {"ic", "diode", "bjt", "connector", "capacitor", "resistor",
           "inductor", "switch"}
# Во что может уточниться каждый «грубый» тип
_REFINE_OK = {
    "ic": {"ic", "mcu", "memory", "logic", "opamp", "comparator", "adc_dac",
           "ldo", "dcdc", "driver", "sensor", "module", "opto", "rf",
           "oscillator", "display"},
    "diode": {"diode", "zener", "schottky", "tvs", "led", "bridge"},
    "bjt": {"bjt", "mosfet", "igbt", "thyristor", "transformer"},
    "connector": {"connector"},
    "capacitor": {"capacitor", "capacitor_pol"},
    "resistor": {"resistor", "resistor_var", "resistor_net"},
    "inductor": {"inductor", "ferrite", "transformer"},
    "switch": {"switch", "button", "relay"},
}


def prefix_type(designator: str) -> str:
    """'C?' -> capacitor, 'USB1' -> connector, '' -> ''."""
    d = re.sub(r"[^A-Za-z]", "", (designator or "").upper())
    if not d:
        return ""
    for n in (4, 3, 2, 1):
        if len(d) >= n and d[:n] in PREFIX_TYPE:
            return PREFIX_TYPE[d[:n]]
    return ""


def package_type(package: str) -> str:
    p = (package or "").strip()
    if not p:
        return ""
    for rx, t in _PKG_COMPILED:
        if rx.search(p):
            return t
    return ""


_RULES = [
    # (regex по тексту, тип)
    (r"\b(mcu|microcontroller|микроконтроллер)\b|stm32|atmega|attiny|rp2040|rp2350|"
     r"esp32|esp8266|nrf5[128]|pic\d{2}|msp430|gd32|apm32|ch32|k1986|стм32", "mcu"),
    (r"\b(ldo|linear regulator|voltage regulator)\b|ams1117|ap2112|lm1117|lp5907|"
     r"tps7[a-z0-9]|xc6206|mic5205|ncp1117|стабилизатор", "ldo"),
    (r"\b(buck|boost|dc-?dc|switching regulator|step-?down|step-?up|sepic)\b|"
     r"mp15\d\d|tps6\d{4}|lm25\d\d|xl4015|mt3608", "dcdc"),
    (r"\b(op-?amp|operational amplifier|оу|опера)\b|lm358|lm324|tl07[124]|opa\d+|"
     r"ad82\d|mcp600\d", "opamp"),
    (r"\b(comparator|компаратор)\b|lm393|lm339|tlv3\d{3}", "comparator"),
    (r"\b(adc|dac|codec|ацп|цап)\b|ads1\d{3}|mcp3\d{3}|pcm\d{4}|ad76\d{2}", "adc_dac"),
    (r"\b(eeprom|fram|sram|sdram|nand|nor flash|psram|память)\b|24[cl]\d+|"
     r"at24|w25q|mx25|is25|gd25|mt41", "memory"),
    (r"\b(74[a-z]{2,4}\d+|cd40\d\d|logic gate|шифратор|дешифратор|триггер|"
     r"мультиплексор|буфер)\b", "logic"),
    (r"\b(optocoupler|opto-?isolator|оптопара|оптрон)\b|pc817|tlp\d+|6n13[67]|il\d{3}", "opto"),
    (r"\b(crystal|quartz|резонатор|кварц)\b|hc-49|abm[0-9]|nx\d{4}", "crystal"),
    (r"\b(oscillator|tcxo|vcxo|ocxo|генератор)\b", "oscillator"),
    (r"\b(connector|header|receptacle|socket|разъ[её]м|соединител|вилка|розетка)\b|"
     r"usb[-_ ]?[abc]\b|type[-_ ]?c|jst|molex|pico ?blade|pls|pbs|idc|"
     r"rj-?\d\d|u\.?fl|ipex|терминал|клеммн", "connector"),
    (r"\b(mosfet|полевой)\b|irf\d+|ao3\d{3}|si\d{4}|bss138|2n7002|ирф", "mosfet"),
    (r"\b(igbt)\b", "igbt"),
    (r"\b(transistor|биполярн)\b|bc\d{3}|2n\d{4}|mmbt|s8050|s8550|кт\d{3}", "bjt"),
    (r"\b(schottky)\b|bat54|ss\d{2}|1n58\d{2}|sk\d{2}", "schottky"),
    (r"\b(zener|стабилитрон)\b|bzx\d+|1n47\d{2}|кс\d{3}", "zener"),
    (r"\b(tvs|esd|supressor|супрессор)\b|smaj|smbj|pesd|usblc6", "tvs"),
    (r"\b(led|светодиод|светоизлуч)\b|ws2812|sk6812", "led"),
    (r"\b(bridge rectifier|диодный мост)\b|mb\d[sm]|db10\d", "bridge"),
    (r"\b(diode|диод)\b|1n400\d|1n414\d|кд\d{3}", "diode"),
    (r"\b(ferrite|bead|бусина)\b|blm\d+", "ferrite"),
    (r"\b(inductor|choke|дроссель|индуктивност|катушк)\b", "inductor"),
    (r"\b(transformer|трансформатор)\b", "transformer"),
    (r"\b(polarized|electrolytic|tantalum|электролит|танталов)\b", "capacitor_pol"),
    (r"\b(capacitor|конденсатор|cap_)\b|^c_", "capacitor"),
    (r"\b(potentiometer|trimmer|подстроечн|переменн)\b", "resistor_var"),
    (r"\b(resistor network|resistor array|сборка резистор)\b", "resistor_net"),
    (r"\b(resistor|резистор|shunt|шунт)\b|^r_", "resistor"),
    (r"\b(fuse|pptc|предохранител)\b", "fuse"),
    (r"\b(varistor|варистор)\b", "varistor"),
    (r"\b(relay|реле)\b", "relay"),
    (r"\b(tact|push ?button|кнопк)\b", "button"),
    (r"\b(switch|dip switch|encoder|переключател|энкодер)\b", "switch"),
    (r"\b(buzzer|speaker|излучател|зуммер)\b", "buzzer"),
    (r"\b(battery|holder|батаре|аккумулятор)\b", "battery"),
    (r"\b(antenna|антенн)\b", "antenna"),
    (r"\b(sensor|imu|accelerometer|gyro|датчик|термометр)\b|bme\d{3}|bmp\d{3}|"
     r"mpu\d{4}|sht\d\d|ds18b20|lsm6|hmc5|ina2\d\d", "sensor"),
    (r"\b(module|модуль)\b|esp-?\d+|sim\d{3}|neo-?[6-9]m|hc-?0[56]", "module"),
    (r"\b(display|oled|lcd|tft|epaper|дисплей|индикатор)\b|ssd1306|st77\d\d|ili9\d{3}", "display"),
    (r"\b(driver|драйвер)\b|drv\d{4}|a4988|tmc2\d{3}|ir2\d{3}", "driver"),
    (r"\b(test ?point|контрольн)\b", "testpoint"),
    (r"\b(mounting ?hole|отверстие|крепёж|крепеж)\b", "mount"),
    (r"\b(motor|двигател)\b", "motor"),
]

_COMPILED = [(re.compile(rx, re.I), t) for rx, t in _RULES]


def text_type(*chunks: str) -> str:
    """Тип по словам в имени/описании/ключевых словах."""
    text = " ".join(c or "" for c in chunks)
    if not text.strip():
        return ""
    # подчёркивания и слэши мешают границам слов: "Resistor_SMD" -> "Resistor SMD"
    text = text + " " + re.sub(r"[_/\\.]", " ", text)
    for rx, t in _COMPILED:
        if rx.search(text):
            return t
    return ""


def pin_type(pins: Optional[List], designator: str = "") -> str:
    """Тип по составу выводов -- последняя линия обороны."""
    n = len(pins or [])
    if not n:
        return ""
    names = [(getattr(p, "name", "") or "").upper() for p in pins]
    etypes = [getattr(p, "etype", "passive") for p in pins]
    pwr = sum(1 for x in names
              if x in ("VCC", "VDD", "GND", "VSS", "VBAT", "AVDD", "AGND",
                       "VDDA", "VSSA", "AVCC"))
    powered = sum(1 for e in etypes if e == "power")
    all_passive = all(e == "passive" for e in etypes)
    # разъём: всё пассивное, имена -- номера/пусто/типовые сигналы
    if all_passive and n >= 4 and pwr == 0:
        numeric = sum(1 for x in names
                      if not x or re.fullmatch(r"(PIN)?\s*[A-Z]?\d+", x))
        if numeric >= n * 0.6:
            return "connector"
    if (pwr or powered) and n >= 8:
        return "ic"
    return ""


def guess_type(name: str = "", description: str = "", keywords: str = "",
               pins: Optional[List] = None, footprint: str = "",
               designator: str = "", package: str = "") -> str:
    """
    Определить тип компонента. Порядок доверия:
      1. префикс позиционного обозначения (C?, USB?, Q? -- источник знает лучше);
      2. слова в имени/описании;
      3. корпус;
      4. состав выводов.
    Если префикс дал «грубый» тип (микросхема, диод, разъём), он уточняется
    текстом и корпусом: D? + "Schottky" -> диод Шоттки, U? + "LDO" -> стабилизатор.
    """
    pref = prefix_type(designator)
    txt = text_type(name, description, keywords, footprint, package)
    pkg = package_type(package or footprint)

    if pref:
        if pref in _COARSE:
            ok = _REFINE_OK.get(pref, set())
            for cand in (txt, pkg):
                if cand and cand in ok and cand != pref:
                    return cand
            # разъём с двумя выводами -- скорее всего всё-таки разъём
            return pref
        return pref

    if txt:
        return txt
    if pkg:
        return pkg
    pt = pin_type(pins, designator)
    if pt:
        return pt
    n = len(pins or [])
    return "ic" if n >= 6 else "other"


def designator_for(ctype: str) -> str:
    return CTYPE_PREFIX.get(ctype, "U") + "?"


# ------------------------------------------------------------- номиналы -----
# На схеме у пассивки должен стоять номинал, а не парт-номер. Если номинал
# не задан (в KiCad его часто нет -- там символ общий), подставляем
# ходовое значение с пометкой «прим.», чтобы на схеме сразу было видно:
# число поставлено нами, а не взято из документации.
VALUE_TYPES = {
    "resistor": "10к",
    "resistor_var": "10к",
    "resistor_net": "10к",
    "capacitor": "100н",
    "capacitor_pol": "10мк",
    "inductor": "10мкГн",
    "ferrite": "600 Ом",
    "crystal": "8 МГц",
    "oscillator": "8 МГц",
    "fuse": "1 А",
    "varistor": "14 В",
    "battery": "3 В",
}

APPROX_MARK = "прим."

# У этих типов обозначение в KiCad уже соответствует ЕСКД (прямоугольник
# резистора, пластины конденсатора, треугольник диода) -- перерисовывать
# нечего. Перерисовываем только то, что в KiCad нарисовано безлико:
# микросхемы, память, логику, разъёмы.
NATIVE_SYMBOL_TYPES = set(VALUE_TYPES) | {
    "diode", "zener", "schottky", "tvs", "led", "bridge",
    "bjt", "mosfet", "igbt", "thyristor", "opto",
    "switch", "button", "relay", "buzzer", "motor", "antenna",
    "testpoint", "mount", "transformer",
}


def keeps_native_symbol(ctype: str) -> bool:
    """Брать обозначение из источника, а не рисовать по ГОСТ."""
    return ctype in NATIVE_SYMBOL_TYPES


def shows_value(ctype: str) -> bool:
    """У этого типа на схеме подписывают номинал, а не парт-номер."""
    return ctype in VALUE_TYPES


def default_value(ctype: str) -> str:
    """Ходовой номинал с пометкой «прим.» -- когда своего нет."""
    v = VALUE_TYPES.get(ctype, "")
    return f"{v} ({APPROX_MARK})" if v else ""


def label_for(comp) -> str:
    """
    Что подписывать у компонента на схеме: у пассивок -- номинал, у
    остального -- парт-номер. Пустым не возвращается.
    """
    ct = getattr(comp, "ctype", "")
    val = (getattr(comp, "value", "") or "").strip()
    if not val:
        val = (getattr(comp, "params", {}) or {}).get("Value", "").strip()
    if shows_value(ct):
        return val or default_value(ct) or getattr(comp, "name", "")
    return ((getattr(comp, "mpn", "") or "").strip() or val
            or getattr(comp, "name", ""))
