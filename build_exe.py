#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Сборка одиночного GostLib.exe для Windows.

Запуск:
    python build_exe.py                 обычная сборка (без консоли)
    python build_exe.py --console       с консолью -- видно traceback и print
    python build_exe.py --clean         снести build/ и dist/ перед сборкой
    python build_exe.py --name MyApp    другое имя exe
    python build_exe.py --slim          без 3D-пакетов: exe вдвое меньше
    python build_exe.py --with-3d       не собирать, если 3D-пакетов нет

Размер exe определяется НЕ папкой проекта, а тем, что установлено в
интерпретаторе, которым запущена сборка. Из проектного .venv с одним
PySide6 выходит примерно 47 МБ; системный Python, где стоят trimesh,
cascadio и numpy, даёт 90+ МБ -- и это не мусор, а работающий просмотр
3D телом. Скрипт печатает состав окружения перед сборкой, чтобы разница
не выглядела загадкой.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = ROOT / "gostlib"

# Каталоги PyInstaller держим внутри build/, чтобы в корне проекта не
# заводился мусор (.spec, промежуточные объекты) рядом с исходниками.
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"

# Модули PySide6, которые GostLib не использует. Каждый из них тянет за
# собой десятки мегабайт DLL (у QtWebEngine это под 150 МБ), поэтому
# выкидываем их явно. Трогать QtCore/QtGui/QtWidgets и QtOpenGL* нельзя:
# на первых трёх держится весь GUI, на QtOpenGLWidgets -- окно 3D-просмотра.
# Что выбрасываем из сборки. Список намеренно консервативный: лишний
# модуль стоит десяток мегабайт, а недостающий -- падение при старте без
# всяких объяснений. QtNetwork, QtSql и QtSvg НЕ исключаем: QtSvg нужен
# превью символа, а первые два тянутся за Qt транзитивно и без них
# сборка падает на ровном месте.
EXCLUDE_QT = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtQml",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
]

# Эти модули анализатор может не увидеть: они нужны, но импортируются
# внутри функций или тянутся Qt'ом.
FORCE_IMPORTS = [
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtPrintSupport",
    "sqlite3",
]

# Необязательные зависимости просмотра 3D: без них программа работает,
# просто не показывает модели и не конвертирует их в STEP.
OPTIONAL_MODULES = ["trimesh", "cascadio"]


def log(msg: str = "") -> None:
    print(msg, flush=True)


def check_windows() -> bool:
    """exe имеет смысл только на Windows: PyInstaller не кросс-компилирует."""
    if os.name == "nt" and sys.platform.startswith("win"):
        return True
    log("Сборка .exe возможна только под Windows.")
    log(f"Текущая система: {sys.platform}. PyInstaller не умеет собирать")
    log("windows-exe из Linux/macOS -- он лишь упаковывает интерпретатор")
    log("той системы, где запущен.")
    log("")
    log("Что делать: скопировать проект на машину с Windows, поставить")
    log("Python 3.10+ и выполнить там build_exe.bat (или build_exe.py).")
    return False


def ensure_pyinstaller() -> bool:
    """PyInstaller ставим тем же интерпретатором, каким собираем.

    Иначе легко получить сборку из «соседнего» Python, где нет PySide6.
    """
    try:
        import PyInstaller  # noqa: F401
        from PyInstaller import __version__ as ver
        log(f"PyInstaller {ver} -- уже установлен")
        return True
    except ImportError:
        pass
    log("PyInstaller не найден, ставлю его...")
    cmd = [sys.executable, "-m", "pip", "install", "pyinstaller"]
    try:
        subprocess.check_call(cmd)
    except (subprocess.CalledProcessError, OSError) as exc:
        log(f"Не удалось установить PyInstaller: {exc}")
        log(f"Поставьте вручную:  {sys.executable} -m pip install pyinstaller")
        return False
    try:
        import PyInstaller  # noqa: F401,F811
    except ImportError:
        log("PyInstaller поставился, но не импортируется. "
            "Проверьте, что pip относится к тому же Python.")
        return False
    return True


def have_module(name: str) -> bool:
    """Проверяем импортом в отдельном процессе.

    Импортировать trimesh/cascadio прямо здесь нельзя: они тянут тяжёлые
    нативные библиотеки и могут упасть на кривой установке, уронив сборку.
    """
    try:
        res = subprocess.run(
            [sys.executable, "-c", f"import {name}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        return res.returncode == 0
    except Exception:
        return False


def ensure_icon() -> Path | None:
    """Иконка окна и exe. Если файла нет -- пробуем сгенерировать его."""
    ico = PKG / "gui" / "gostlib.ico"
    if ico.is_file():
        return ico
    maker = PKG / "gui" / "make_icon.py"
    if maker.is_file():
        log("gostlib.ico не найден, генерирую через make_icon.py...")
        env = dict(os.environ)
        # Рисуем без настоящего окна: на сборочной машине сессии может и
        # не быть (RDP без графики, CI-агент).
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            subprocess.run([sys.executable, str(maker)], env=env, timeout=300)
        except Exception as exc:
            log(f"  не вышло: {exc}")
    if ico.is_file():
        return ico
    log("Иконки нет -- exe будет с иконкой PyInstaller по умолчанию.")
    return None


def collect_data() -> list[tuple[Path, str]]:
    """Файлы, которые программа читает с диска в рантайме.

    Код ищет их относительно __file__ своего модуля, а PyInstaller в режиме
    onefile распаковывает всё во временную папку и подменяет __file__ на
    путь внутри неё. Поэтому целевой путь обязан повторять структуру
    пакета: gostlib/altium/... лежит именно в gostlib/altium.
    """
    items: list[tuple[Path, str]] = []
    pas = PKG / "altium" / "GostLibBuilder.pas"
    if pas.is_file():
        items.append((pas, "gostlib/altium"))
    else:
        log("ВНИМАНИЕ: не найден GostLibBuilder.pas -- в собранной программе "
            "не будет скрипта для Altium.")
    prj = PKG / "altium" / "GostLibBuilder.PrjScr"
    if prj.is_file():
        items.append((prj, "gostlib/altium"))
    tpl = PKG / "templates"
    if tpl.is_dir():
        items.append((tpl, "gostlib/templates"))
    else:
        log("Папки gostlib/templates нет -- сборка без шаблона empty.PcbLib "
            "(экспорт .PcbLib потребует шаблон рядом с exe).")
    ico = PKG / "gui" / "gostlib.ico"
    if ico.is_file():
        # Не только иконка exe: main_window.app_icon() читает этот же файл
        # рядом с кодом, иначе иконка окна рисуется примитивом.
        items.append((ico, "gostlib/gui"))
    return items


def make_launcher() -> Path:
    """PyInstaller не умеет точку входа вида `python -m пакет.модуль`.

    Пишем крошечный стартовый файл в build/ -- внутрь gostlib/ ничего
    добавлять не нужно.
    """
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    path = BUILD_DIR / "gostlib_launcher.py"
    path.write_text(
        "import sys\n"
        "from gostlib.gui.app import main\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main() or 0)\n",
        encoding="utf-8")
    return path


def clean_build_env() -> dict[str, str]:
    """Не позволять PyInstaller подмешивать DLL из служебных runtime.

    Desktop-среды разработки могут добавлять в PATH собственные Poppler,
    LibreOffice и другие наборы DLL.  Анализатор зависимостей видит их как
    подходящие системные библиотеки и, например, кладёт poppler-овскую
    ``icuuc.dll`` рядом с Qt.  В исходном Python Qt использует ICU из
    System32, а собранный exe после такой подмены падает ещё до открытия
    окна с ``DLL load failed: procedure not found``.

    Зависимости самого проекта приходят через sys.path/PYTHONPATH и от этой
    фильтрации не страдают.
    """
    env = dict(os.environ)
    path_items = env.get("PATH", "").split(os.pathsep)
    foreign_markers = ("\\.cache\\codex-runtimes\\",)
    env["PATH"] = os.pathsep.join(
        item for item in path_items
        if not any(marker in item.lower() for marker in foreign_markers)
    )
    return env


def sweep_old(dist: Path) -> int:
    """
    Подобрать хвосты прошлых сборок: GostLib.old-*.exe.

    Файл, который был занят запущенной программой, мы переименовываем, а
    не удаляем -- Windows переименовать работающий exe разрешает, удалить
    нет. Мусор убираем при следующем запуске, когда программа уже закрыта.
    """
    n = 0
    if not dist.is_dir():
        return 0
    for p in dist.glob("*.old-*.exe"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass                      # всё ещё запущен -- уберём в другой раз
    return n


def locked(path: Path) -> bool:
    """Занят ли файл. Проверяем попыткой открыть на запись -- на Windows
    работающий exe открыть так нельзя."""
    if not path.is_file():
        return False
    try:
        with open(path, "r+b"):
            return False
    except OSError:
        return True


def free_target(exe: Path) -> bool:
    """
    Освободить место под новый exe.

    Самая частая причина «Отказано в доступе: dist\\GostLib.exe» -- сама
    программа сейчас запущена. PyInstaller в этом месте просто падает
    трассировкой, из которой ничего не понятно. Переименовываем занятый
    файл: Windows это разрешает, сборка идёт дальше, а старый файл
    удалится при следующем запуске. Возвращает False, если и это не
    вышло, -- тогда честно скажем, что делать.
    """
    if not locked(exe):
        return True
    stamp = time.strftime("%Y%m%d-%H%M%S")
    spare = exe.with_name(f"{exe.stem}.old-{stamp}.exe")
    try:
        exe.rename(spare)
        log(f"{exe.name} занят (программа запущена) — отложил его в "
            f"{spare.name}, соберу новый.")
        log("Старый файл удалится сам при следующей сборке.")
        return True
    except OSError as exc:
        log(f"{exe.name} занят и не переименовывается: {exc}")
        return False


def human_size(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb:.1f} МБ ({n} байт)"


def in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def pkg_size(name: str) -> int:
    """Сколько весит пакет на диске -- чтобы объяснить размер exe."""
    try:
        res = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util,os,sys;"
             f"s=importlib.util.find_spec({name!r});"
             "p=(s.submodule_search_locations[0] if s and "
             "s.submodule_search_locations else (s.origin if s else ''));"
             "t=0\n"
             "if p and os.path.isdir(p):\n"
             "    for r,_d,f in os.walk(p):\n"
             "        for x in f:\n"
             "            try: t+=os.path.getsize(os.path.join(r,x))\n"
             "            except OSError: pass\n"
             "elif p and os.path.isfile(p): t=os.path.getsize(p)\n"
             "print(t)"],
            capture_output=True, text=True, timeout=120)
        return int((res.stdout or "0").strip() or 0)
    except Exception:
        return 0


def report_env(want_3d: bool) -> None:
    """
    Из чего складывается размер exe.

    Вопрос «почему из одной папки 47 МБ, а из другой 90» возникает
    ровно один раз и всегда с одним ответом: дело не в папке, а в
    ИНТЕРПРЕТАТОРЕ. Из проектного .venv, где стоит только PySide6,
    выходит компактный exe; системный Python обычно несёт numpy,
    trimesh и прочее, и PyInstaller всё это добросовестно упаковывает.
    Поэтому печатаем состав прямо в начале сборки.
    """
    log("Окружение сборки:")
    log(f"  интерпретатор: {sys.executable}")
    log(f"  это {'виртуальное окружение проекта' if in_venv() else 'СИСТЕМНЫЙ Python'}")
    heavy = [("PySide6", "обязателен"), ("olefile", "обязателен"),
             ("trimesh", "3D телом"), ("cascadio", "STEP -> сетка"),
             ("numpy", "тянется за trimesh"), ("scipy", "тянется за trimesh"),
             ("shapely", "тянется за trimesh"), ("PIL", "тянется за trimesh")]
    total = 0
    for name, why in heavy:
        if not have_module(name):
            continue
        sz = pkg_size(name)
        total += sz
        log(f"  {name:9} {human_size(sz):>22}   {why}")
    log(f"  итого пакетов на диске: {human_size(total)}")
    if not in_venv():
        log("  ВНИМАНИЕ: сборка идёт системным Python -- в exe попадёт всё,")
        log("  что в нём установлено. Компактную сборку даёт своё окружение:")
        log("      python -m venv .venv")
        log("      .venv\\Scripts\\pip install -r requirements.txt")
        log("      build_exe.bat")
    if not want_3d:
        log("  режим --slim: trimesh/cascadio в exe не пойдут "
            "(3D рисуется своим разбором STEP)")
    log("")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Сборка GostLib.exe (PyInstaller, один файл)")
    ap.add_argument("--console", action="store_true",
                    help="собрать с консолью: видно traceback и print")
    ap.add_argument("--clean", action="store_true",
                    help="удалить build/ и dist/ перед сборкой")
    ap.add_argument("--name", default="GostLib", help="имя exe (без .exe)")
    # Размер exe зависит не от папки, а от того, что стоит в
    # интерпретаторе. Даём это решать явно, а не «как повезёт».
    ap.add_argument("--slim", action="store_true",
                    help="без trimesh/cascadio: exe вдвое меньше, 3D "
                         "рисуется своим разбором STEP")
    ap.add_argument("--with-3d", action="store_true",
                    help="требовать trimesh и cascadio: без них сборка "
                         "остановится, а не соберёт молча урезанный exe")
    args = ap.parse_args()

    log("=== Сборка GostLib.exe ===")
    log(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    log(f"Проект: {ROOT}")
    log("")

    if not check_windows():
        return 1
    if not PKG.is_dir():
        log(f"Не найден пакет gostlib в {ROOT}. "
            "Запускайте скрипт из корня проекта.")
        return 1
    # Без PySide6 собирать нечего: PyInstaller упакует exe, который упадёт
    # при первом запуске.
    if not have_module("PySide6"):
        log("В этом интерпретаторе нет PySide6.")
        log(f"Поставьте:  {sys.executable} -m pip install PySide6")
        return 1
    if not ensure_pyinstaller():
        return 1
    report_env(not args.slim)

    if args.clean:
        for d in (BUILD_DIR, DIST_DIR):
            if d.exists():
                log(f"Удаляю {d}")
                shutil.rmtree(d, ignore_errors=True)

    # Прошлый exe может быть занят: чаще всего просто запущена сама
    # программа. Разбираемся с этим ДО PyInstaller -- он на этом месте
    # падает трассировкой, в которой причина не видна.
    dropped = sweep_old(DIST_DIR)
    if dropped:
        log(f"Убрал {dropped} файл(ов) от прошлых сборок")
    target = DIST_DIR / f"{args.name}.exe"
    if not free_target(target):
        log("")
        log("Закройте запущенный GostLib и повторите сборку.")
        log("Если программа закрыта, файл держит антивирус или проводник:")
        log(f"  * добавьте {DIST_DIR} в исключения антивируса;")
        log("  * закройте окно проводника, открытое на этой папке.")
        log("Найти, кто держит файл:")
        log(f'  powershell "Get-Process | ? {{$_.Path -eq \'{target}\'}}"')
        return 1

    ico = ensure_icon()
    launcher = make_launcher()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconfirm",
        "--name", args.name,
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR / "work"),
        "--specpath", str(BUILD_DIR),
        # gostlib лежит в корне проекта, а стартовый файл -- в build/,
        # поэтому путь поиска пакета задаём явно.
        "--paths", str(ROOT),
        # Половина модулей подгружается лениво (внутри функций и по кнопкам
        # GUI), автоанализ их не видит -- забираем пакет целиком.
        "--collect-submodules", "gostlib",
    ]
    cmd.append("--console" if args.console else "--windowed")
    if ico:
        cmd += ["--icon", str(ico)]

    # На Windows разделитель источника и приёмника в --add-data -- ";"
    # (на Unix ":"), собираем только под Windows, поэтому жёстко ";".
    for src, dest in collect_data():
        cmd += ["--add-data", f"{src};{dest}"]
        log(f"данные: {src.relative_to(ROOT)} -> {dest}")

    # olefile нужен для чтения/записи .PcbLib и .SchLib (формат OLE).
    if have_module("olefile"):
        cmd += ["--hidden-import", "olefile"]
    else:
        log("ВНИМАНИЕ: нет olefile -- экспорт библиотек Altium не заработает."
            f"  Поставьте: {sys.executable} -m pip install olefile")

    have_3d = []
    if not args.slim:
        for mod in OPTIONAL_MODULES:
            if have_module(mod):
                have_3d.append(mod)
                cmd += ["--hidden-import", mod, "--collect-all", mod]
    if args.with_3d and len(have_3d) < len(OPTIONAL_MODULES):
        log("Запрошено --with-3d, но в этом интерпретаторе нет "
            + ", ".join(m for m in OPTIONAL_MODULES if m not in have_3d))
        log(f"  Поставьте: {sys.executable} -m pip install trimesh cascadio")
        return 1
    if args.slim:
        log("3D-просмотр телом: НЕ включён (--slim). Программа работает, "
            "модели рисуются нашим разбором STEP -- каркасом.")
        # numpy и прочее из окружения без trimesh в exe не нужны
        for mod in ("trimesh", "cascadio", "numpy", "scipy", "shapely",
                    "networkx", "PIL", "matplotlib", "pandas"):
            cmd += ["--exclude-module", mod]
    elif len(have_3d) == len(OPTIONAL_MODULES):
        log("3D-просмотр: включён (trimesh + cascadio). Это главная статья "
            "размера exe: пакеты тянут за собой numpy и данные.")
    elif have_3d:
        log(f"3D-просмотр: частично, есть только {', '.join(have_3d)}. "
            "Конвертация STEP может быть недоступна.")
    else:
        log("3D-просмотр: НЕ включён -- нет trimesh/cascadio. Программа "
            "соберётся и будет работать, кроме просмотра 3D-моделей.")
        log(f"  Чтобы включить: {sys.executable} -m pip install trimesh "
            "cascadio, затем пересобрать.")

    for mod in FORCE_IMPORTS:
        cmd += ["--hidden-import", mod]
    for mod in EXCLUDE_QT:
        cmd += ["--exclude-module", mod]

    if args.clean:
        cmd.append("--clean")
    cmd.append(str(launcher))

    log("")
    log("Запускаю PyInstaller (первый раз это 2-5 минут)...")
    log("")
    try:
        rc = subprocess.call(cmd, env=clean_build_env())
    except OSError as exc:
        log(f"Не удалось запустить PyInstaller: {exc}")
        return 1
    if rc != 0:
        log("")
        log(f"PyInstaller завершился с кодом {rc}. Смотрите сообщения выше:")
        log("  * 'ModuleNotFoundError' при запуске exe -- добавьте модуль в")
        log("    --hidden-import (или уберите его из EXCLUDE_QT);")
        log("  * блокировка файла -- закройте запущенный GostLib.exe и")
        log("    добавьте dist/ в исключения антивируса;")
        log("  * мусор от прошлой сборки -- запустите с --clean.")
        return rc

    exe = DIST_DIR / f"{args.name}.exe"
    log("")
    if exe.is_file():
        log("Готово.")
        log(f"Файл:   {exe}")
        log(f"Размер: {human_size(exe.stat().st_size)}")
        log("")
        log("Это самодостаточный exe: Python и PySide6 внутри, ставить")
        log("ничего не нужно. Первый запуск дольше -- распаковка во")
        log("временную папку.")
        mb = exe.stat().st_size / (1024 * 1024)
        if have_3d and mb > 60:
            log("")
            log(f"Почему {mb:.0f} МБ, а не ~47: в сборку вошли "
                f"{' и '.join(have_3d)} вместе с numpy -- это просмотр 3D")
            log("телом. Нужен компактный exe -- соберите с --slim либо из")
            log("окружения, где этих пакетов нет.")
        elif not have_3d and mb < 60:
            log("")
            log("Сборка без 3D-пакетов. Просмотр моделей телом работать не")
            log("будет -- модели рисуются каркасом по нашему разбору STEP.")
        return 0
    log(f"PyInstaller отработал, но {exe} не появился. "
        f"Проверьте содержимое {DIST_DIR}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
