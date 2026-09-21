"""Настройки приложения (хранятся в JSON рядом с базой)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Optional


# Переменная окружения, которой рабочую папку можно унести куда угодно:
# на другой диск, в облачную папку отдела, на флешку. Задаётся один раз в
# «Переменные среды» Windows и действует на все запуски -- и на exe, и на
# запуск из исходников.
HOME_ENV = "GOSTLIB_HOME"


def default_root() -> str:
    """
    Рабочая папка: каталог компонентов, настройки, модели, задания,
    резервные копии.

    Лежит ОТДЕЛЬНО от программы и специально: обновление или удаление
    самой программы её не трогает, поэтому библиотека переживает
    переустановку. По умолчанию это %LOCALAPPDATA%\\GostLib (на Windows)
    или ~/.gostlib.
    """
    own = (os.environ.get(HOME_ENV) or "").strip().strip('"')
    if own:
        return os.path.abspath(os.path.expanduser(own))
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "GostLib")
    return os.path.join(os.path.expanduser("~"), ".gostlib")


@dataclass
class Config:
    root: str = field(default_factory=default_root)
    library_name: str = "GOST_Lib"
    # куда складываются .SchLib/.PcbLib и 3D-модели
    out_dir: str = ""
    # шрифт и стиль
    font: str = "GOST type B"
    size_pin: int = 7
    size_pin_num: int = 6
    size_desig: int = 10
    size_type: int = 10
    size_table: int = 7
    pin_length: int = 300
    pin_pitch: int = 100
    num_offset: int = 25         # подъём номера вывода над линией, mil
    auto_pitch: bool = True      # поднимать шаг выводов под кегль текста
    table_cell_source: str = "name"
    table_sort: str = "source"
    table_row_lines: bool = False
    pin_numbers_as_text: bool = True
    show_pin_numbers: bool = True
    show_power_marks: bool = True
    ic_fields: bool = True
    group_gap_rows: int = 1
    compact_symbols: bool = True  # одинаковые узкие боковые поля по сетке
    # масштаб графики двухвыводных элементов (резистор, конденсатор, диод…)
    passive_scale: float = 1.0
    # цвета Altium: целое BGR, 0 = чёрный
    color_graphic: int = 0
    color_text: int = 0
    color_pin_num: int = 0
    color_pin: int = 0
    # Location вывода в Altium: False -- конец у корпуса (проверено, верно),
    # True -- электрический конец
    pin_location_hot: bool = False
    # интеграция с Altium
    altium_exe: str = ""
    auto_install_library: bool = True
    # собирать ещё и .IntLib -- в панели Components будет одна запись
    # вместо пары «символы + посадки»
    build_intlib: bool = False
    keep_vendor_footprints: bool = True
    # Какую установку KiCad использовать. Пусто -- все, что нашлись; иначе
    # путь к share/kicad нужной версии. Когда версий несколько, каждый
    # символ иначе находится по разу на версию.
    kicad_root: str = ""
    # Правила группировки выводов микросхем (см. gost/pingroups.py).
    # Пусто -- встроенные.
    pin_rules: str = ""
    # Текущий проект (id в таблице projects). 0 -- общая библиотека.
    active_project: int = 0
    # Импортированное сразу попадает в текущий проект. Иначе компонент
    # оседает в общем каталоге, а при включённом показе «только проект»
    # его не видно -- и добавить в проект тоже нечем.
    import_to_project: bool = True
    # Кегль текста в превью и в редакторе УГО относительно того, что
    # уйдёт в Altium. Меньше единицы потому, что Altium отмеряет текст по
    # высоте прописной буквы, а SVG и Qt -- по полной высоте кегля (em):
    # у ГОСТ тип Б это отношение примерно 0.72. Значение вынесено в
    # настройки, чтобы подогнать превью под свой Altium раз и навсегда.
    preview_font_scale: float = 0.72
    # Потолок треугольников в 3D-модели, которую мы делаем из OBJ.
    # Это настройка СКОРОСТИ СБОРКИ, а не качества картинки: замер на
    # живом проекте -- модель на ~58 тысяч треугольников вставлялась в
    # посадочное место 337 секунд, соседняя посадка на 355 площадок --
    # 8,5 секунды. Меньше значение -- быстрее сборка.
    model_faces: int = 6000
    # Сажать 3D-тело на плоскость платы. Altium считает Z = 0 плоскостью
    # платы, а модели из EasyEDA приходят с началом координат где придётся:
    # у FBGA-96 геометрия шла от -0.37 мм, и шарики уходили ВНУТРЬ платы.
    # Смещение кладётся в standoff -- сам файл модели не трогаем, он может
    # быть выбран вручную и чужой. У выводных корпусов посадка не
    # применяется: там ножки НИЖЕ платы -- это правильно.
    seat_models: bool = True
    # 3D из EasyEDA брать родным STEP производителя (цветной, точный), а
    # сетку OBJ -- только запасным путём. Выключать стоит, лишь если
    # какая-то модель встала криво: тогда вернётся прежний путь через OBJ.
    easyeda_step: bool = True
    # Кнопка «Собрать задание» (F9) на панели. Обычно пользуются только
    # «Собрать и запустить», поэтому по умолчанию кнопка спрятана; F9 при
    # этом работает всё равно.
    show_job_button: bool = False
    # Экспорт в проект KiCad. Предупреждать, что компонент и так пришёл из
    # KiCad (тогда проще подключить исходную библиотеку, чем копировать).
    kicad_export_warn: bool = True
    # Раскладывать символ, посадку и 3D по разным папкам (иначе -- всё в
    # одну папку внутри проекта).
    kicad_export_split: bool = False
    # Последний проект KiCad и имя библиотеки, куда выгружали.
    kicad_export_project: str = ""
    kicad_export_lib: str = "GostLib"
    # Папка текстового зеркала библиотеки -- та, что лежит в git или в
    # облачной папке отдела. Пусто -- library-git рядом с каталогом.
    git_dir: str = ""
    # Выкладывать вместе с компонентами и 3D-модели. STEP текстовый, git
    # его переваривает, но пара сотен мегабайт в репозитории мало кого
    # радует -- поэтому по умолчанию выключено.
    git_models: bool = False
    # прочее
    theme: str = "dark"
    # параметры, вынесенные в таблицу отдельными колонками
    table_params: list = field(default_factory=list)

    @property
    def db_path(self) -> str:
        return os.path.join(self.root, "catalog.sqlite")

    @property
    def jobs_dir(self) -> str:
        return os.path.join(self.root, "jobs")

    @property
    def models_dir(self) -> str:
        return os.path.join(self.root, "models")

    @property
    def lib_dir(self) -> str:
        return self.out_dir or os.path.join(self.root, "library")

    @property
    def cache_dir(self) -> str:
        """Скачанное из сети: описания LCSC и OBJ-модели. Удаляется без потерь."""
        return os.path.join(self.root, "cache")

    @property
    def cfg_path(self) -> str:
        return os.path.join(self.root, "settings.json")

    def ensure_dirs(self):
        for d in (self.root, self.jobs_dir, self.models_dir, self.lib_dir):
            os.makedirs(d, exist_ok=True)

    def save(self):
        self.ensure_dirs()
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=1)

    def to_style(self):
        from .gost.style import Style
        st = Style()
        st.font = self.font
        st.size_pin = self.size_pin
        st.size_pin_num = self.size_pin_num
        st.size_desig = self.size_desig
        st.size_type = self.size_type
        st.size_table = self.size_table
        st.pin_length = self.pin_length
        st.pin_pitch = self.pin_pitch
        st.num_offset = self.num_offset
        st.auto_pitch = self.auto_pitch
        st.table_cell_source = self.table_cell_source
        st.table_sort = self.table_sort
        st.table_row_lines = self.table_row_lines
        st.pin_numbers_as_text = self.pin_numbers_as_text
        st.show_pin_numbers = self.show_pin_numbers
        st.show_power_marks = self.show_power_marks
        st.ic_fields = self.ic_fields
        st.group_gap_rows = self.group_gap_rows
        st.compact_symbols = self.compact_symbols
        st.passive_scale = self.passive_scale
        st.color_graphic = self.color_graphic
        st.color_text = self.color_text
        st.color_pin_num = self.color_pin_num
        st.color_pin = self.color_pin
        st.preview_font_scale = self.preview_font_scale
        return st


def load(root: str = "") -> Config:
    root = root or default_root()
    path = os.path.join(root, "settings.json")
    cfg = Config(root=root)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data: Dict[str, Any] = json.load(f)
            known = {f.name for f in fields(Config)}
            for k, v in data.items():
                if k in known:
                    setattr(cfg, k, v)
            cfg.root = root
        except Exception:
            pass
    cfg.ensure_dirs()
    return cfg
