#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Сборка установщика GostLib-Setup-*.exe (Inno Setup 6).

Запуск:
    python build_installer.py              собрать установщик
    python build_installer.py --rebuild    пересобрать GostLib.exe заново
    python build_installer.py --console    exe с консолью (передаётся в build_exe.py)
    python build_installer.py --yes        не спрашивать про установку Inno Setup
    python build_installer.py --no-install не ставить Inno Setup, только сказать
    python build_installer.py --portable   только портативный архив, без Inno Setup

Скрипт сам соберёт dist\\GostLib.exe, если его ещё нет: без него компилятору
Inno Setup нечего упаковывать. Если нет и самого Inno Setup -- предложит
поставить его сам (winget или загрузка с jrsoftware.org), а если поставить
не вышло, соберёт хотя бы портативный архив.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

BUILD_EXE = ROOT / "build_exe.py"
DIST_DIR = ROOT / "dist"
APP_EXE = DIST_DIR / "GostLib.exe"
ISS = HERE / "gostlib.iss"
INIT_PY = ROOT / "gostlib" / "__init__.py"

# Версия на случай, если в gostlib/__init__.py не окажется __version__.
# Ровно то же значение стоит в gostlib.iss под #ifndef, поэтому ручная
# сборка через ISCC даёт тот же результат.
FALLBACK_VERSION = "1.0.0"

ISS_DOWNLOAD_URL = "https://jrsoftware.org/isdl.php"

# Постоянная ссылка «всегда последняя стабильная версия»; вторая -- на
# случай, если редирект однажды переедет.
ISS_DIRECT_URLS = (
    "https://jrsoftware.org/download.php/is.exe",
    "https://files.jrsoftware.org/is/6/innosetup-6.4.3.exe",
)
WINGET_ID = "JRSoftware.InnoSetup"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def check_windows() -> bool:
    """ISCC.exe существует только под Windows, да и упаковывать нечего.

    Wine и прочие обходные пути сознательно не рассматриваем: собранный так
    установщик всё равно пришлось бы проверять на настоящей Windows.
    """
    if os.name == "nt" and sys.platform.startswith("win"):
        return True
    log("Сборка установщика возможна только под Windows.")
    log(f"Текущая система: {sys.platform}. Inno Setup -- windows-программа,")
    log("а упаковываем мы windows-exe, который на этой системе не собрать.")
    log("")
    log("Что делать: скопировать проект на машину с Windows, поставить")
    log("Python 3.10+ и Inno Setup 6, затем выполнить installer\\build_installer.bat.")
    return False


def read_version() -> str:
    """Версию берём разбором текста, а не импортом пакета.

    Импорт gostlib потянул бы PySide6 и остальные зависимости, которых на
    сборочной машине может не быть в этом интерпретаторе.
    """
    if not INIT_PY.is_file():
        log(f"Нет файла {INIT_PY}, беру версию {FALLBACK_VERSION}.")
        return FALLBACK_VERSION
    text = INIT_PY.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", text, re.M)
    if not m:
        log(f"В {INIT_PY.name} нет переменной __version__, "
            f"беру версию {FALLBACK_VERSION}.")
        log("  Чтобы номер версии брался автоматически, добавьте туда строку:")
        log(f'    __version__ = "{FALLBACK_VERSION}"')
        return FALLBACK_VERSION
    return m.group(1).strip()


def _iscc_dirs() -> list[Path]:
    """Каталоги, где может лежать Inno Setup.

    Установка «для всех» кладёт его в Program Files (x86) -- компилятор
    32-битный; установка без администратора (`/CURRENTUSER`) уходит в
    %LOCALAPPDATA%\\Programs.
    """
    dirs: list[Path] = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432",
                "LOCALAPPDATA"):
        base = os.environ.get(var)
        if not base:
            continue
        if var == "LOCALAPPDATA":
            dirs.append(Path(base) / "Programs" / "Inno Setup 6")
        else:
            dirs.append(Path(base) / "Inno Setup 6")
    return dirs


def _iscc_from_registry() -> Path | None:
    """Путь из записи деинсталляции -- работает при любой папке установки."""
    try:
        import winreg
    except ImportError:
        return None
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"
    roots = [(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_CURRENT_USER, 0)]
    for root, flag in roots:
        try:
            with winreg.OpenKey(root, key, 0, winreg.KEY_READ | flag) as k:
                loc, _ = winreg.QueryValueEx(k, "InstallLocation")
        except OSError:
            continue
        if loc:
            p = Path(loc) / "ISCC.exe"
            if p.is_file():
                return p
    return None


def find_iscc() -> Path | None:
    """ISCC.exe -- консольный компилятор Inno Setup.

    Установщик Inno Setup не прописывает себя в PATH, поэтому ищем и по
    известным каталогам, и по записи в реестре.
    """
    for d in _iscc_dirs():
        p = d / "ISCC.exe"
        if p.is_file():
            return p
    p = _iscc_from_registry()
    if p is not None:
        return p
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    return None


def explain_no_iscc() -> None:
    log("Не найден ISCC.exe -- компилятор Inno Setup.")
    log("")
    log("Искал здесь:")
    for d in _iscc_dirs():
        log(f"  {d / 'ISCC.exe'}")
    log("  запись в реестре (Inno Setup 6_is1) и команду ISCC в PATH")


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ask_yes(question: str, default: bool = True) -> bool:
    """Вопрос в консоли. Без консоли (запуск не из окна) -- значение по умолчанию."""
    if not sys.stdin or not sys.stdin.isatty():
        return default
    hint = "[Д/н]" if default else "[д/Н]"
    try:
        ans = input(f"{question} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        log("")
        return False
    if not ans:
        return default
    return ans[0] in "yд1"


def _try_winget() -> bool:
    if not shutil.which("winget"):
        return False
    log("Пробую winget...")
    cmd = ["winget", "install", "-e", "--id", WINGET_ID,
           "--accept-source-agreements", "--accept-package-agreements",
           "--disable-interactivity"]
    try:
        rc = subprocess.call(cmd)
    except OSError as exc:
        log(f"  winget не запустился: {exc}")
        return False
    if rc != 0:
        log(f"  winget вернул код {rc}.")
        return False
    return find_iscc() is not None


def _try_download() -> bool:
    """Скачать установщик Inno Setup и поставить его тихо.

    Без прав администратора ставим только для текущего пользователя
    (`/CURRENTUSER`) -- тогда не всплывает запрос UAC, а ISCC.exe окажется
    в %LOCALAPPDATA%\\Programs\\Inno Setup 6, где мы его тоже ищем.
    """
    import urllib.error
    import urllib.request

    tmp = Path(tempfile.gettempdir()) / "innosetup-latest.exe"
    got = False
    for url in ISS_DIRECT_URLS:
        log(f"Скачиваю {url} ...")
        try:
            with urllib.request.urlopen(url, timeout=120) as src, \
                    open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            got = tmp.is_file() and tmp.stat().st_size > 1_000_000
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"  не вышло: {exc}")
            got = False
        if got:
            break
    if not got:
        log("Скачать не удалось (нет сети или сайт недоступен).")
        return False

    log(f"Ставлю Inno Setup ({human_size(tmp.stat().st_size)})...")
    flags = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
    flags.append("/ALLUSERS" if is_admin() else "/CURRENTUSER")
    try:
        rc = subprocess.call([str(tmp)] + flags)
    except OSError as exc:
        log(f"  установщик не запустился: {exc}")
        return False
    if rc != 0:
        log(f"  установщик вернул код {rc}.")
        return False
    return find_iscc() is not None


def ensure_iscc(auto: bool, allow_install: bool) -> Path | None:
    """Найти компилятор, при необходимости поставив Inno Setup.

    Просить человека «сходи на сайт и поставь» -- ровно тот шаг, на котором
    сборка и встаёт. Поэтому ставим сами, но спрашиваем: скрипт лезет в
    сеть и ставит стороннюю программу.
    """
    iscc = find_iscc()
    if iscc is not None:
        return iscc
    explain_no_iscc()
    log("")
    if not allow_install:
        log("Поставьте Inno Setup 6.3 или новее вручную:")
        log(f"  {ISS_DOWNLOAD_URL}")
        return None
    if not auto and not ask_yes("Поставить Inno Setup автоматически?"):
        log("")
        log("Хорошо. Тогда вручную:")
        log(f"  {ISS_DOWNLOAD_URL}")
        log("Обычная установка со всеми настройками по умолчанию, потом")
        log("запустите этот скрипт заново.")
        return None
    log("")
    if _try_winget() or _try_download():
        iscc = find_iscc()
        if iscc is not None:
            log(f"Готово, компилятор здесь: {iscc}")
            log("")
            return iscc
    log("")
    log("Поставить автоматически не вышло. Вручную:")
    log(f"  {ISS_DOWNLOAD_URL}")
    return None


def make_portable(version: str) -> Path | None:
    """Запасной вариант распространения: zip с одним exe.

    Установщика нет, но отдать человеку что-то работающее всё равно нужно:
    GostLib.exe самодостаточен, распаковал -- запустил.
    """
    if not APP_EXE.is_file():
        return None
    out = DIST_DIR / f"GostLib-{version}-portable.zip"
    note = (
        "GostLib -- менеджер библиотеки компонентов для Altium Designer.\r\n"
        "\r\n"
        "Установка не нужна: распакуйте папку куда угодно и запустите\r\n"
        "GostLib.exe. Настройки и каталог компонентов программа держит\r\n"
        "в %LOCALAPPDATA%\\GostLib.\r\n"
        "\r\n"
        f"Версия {version}.\r\n"
    )
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(APP_EXE, "GostLib/GostLib.exe")
            z.writestr("GostLib/ПРОЧТИ МЕНЯ.txt", note.encode("cp1251",
                                                              "replace"))
            readme = ROOT / "README.md"
            if readme.is_file():
                z.write(readme, "GostLib/README.md")
    except OSError as exc:
        log(f"Не удалось собрать портативный архив: {exc}")
        return None
    return out


def build_app_exe(extra: list[str]) -> bool:
    """Собираем GostLib.exe тем же интерпретатором, каким запущены.

    Отдельным процессом, а не импортом build_exe: тот вызывает
    sys.exit() и argparse, и внутри одного процесса это неудобно.
    """
    if not BUILD_EXE.is_file():
        log(f"Не найден {BUILD_EXE}. Запускайте скрипт из папки installer "
            "внутри проекта GostLib.")
        return False
    log(f"Собираю {APP_EXE.name} (build_exe.py)...")
    log("")
    cmd = [sys.executable, str(BUILD_EXE)] + extra
    try:
        rc = subprocess.call(cmd, cwd=str(ROOT))
    except OSError as exc:
        log(f"Не удалось запустить build_exe.py: {exc}")
        return False
    if rc != 0:
        log("")
        log(f"build_exe.py завершился с кодом {rc}. Установщик не собран.")
        return False
    if not APP_EXE.is_file():
        log(f"build_exe.py отработал, но {APP_EXE} не появился.")
        return False
    return True


def newer_sources(exe: Path) -> list[Path]:
    """Исходники, изменённые позже сборки exe.

    Без этой проверки установщик молча упаковывал прошлый `dist\\GostLib.exe`,
    и человек ставил старую версию, будучи уверенным, что собрал новую.
    Смотрим весь пакет плюс сборочные скрипты; `dist` и `build` пропускаем.
    """
    try:
        stamp = exe.stat().st_mtime
    except OSError:
        return []
    watch: list[Path] = [BUILD_EXE, ROOT / "gostlib.bat"]
    pkg = ROOT / "gostlib"
    if pkg.is_dir():
        for p in pkg.rglob("*"):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts:
                continue
            watch.append(p)
    out: list[Path] = []
    for p in watch:
        try:
            if p.is_file() and p.stat().st_mtime > stamp + 1:
                out.append(p)
        except OSError:
            continue
    return out


def human_size(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb:.1f} МБ ({n} байт)"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Сборка установщика GostLib (Inno Setup 6)")
    ap.add_argument("--rebuild", action="store_true",
                    help="пересобрать GostLib.exe, даже если он уже есть")
    ap.add_argument("--console", action="store_true",
                    help="собрать GostLib.exe с консолью (для отладки)")
    ap.add_argument("--clean", action="store_true",
                    help="передать --clean в build_exe.py")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="не спрашивать про установку Inno Setup")
    ap.add_argument("--no-install", action="store_true",
                    help="не ставить Inno Setup, только сообщить")
    ap.add_argument("--portable", action="store_true",
                    help="собрать только портативный архив, без Inno Setup")
    args = ap.parse_args()

    log("=== Сборка установщика GostLib ===")
    log(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    log(f"Проект: {ROOT}")
    log("")

    if not check_windows():
        return 1
    if not ISS.is_file():
        log(f"Не найден скрипт установщика {ISS}.")
        return 1

    # Компилятор ищем до долгой сборки exe: узнать, что Inno Setup не
    # установлен, лучше сразу, а не через пять минут работы PyInstaller.
    iscc = None
    if not args.portable:
        iscc = ensure_iscc(auto=args.yes, allow_install=not args.no_install)
        if iscc is None:
            log("")
            log("Соберу портативный архив -- распространять можно и им.")
            log("")
        else:
            log(f"Inno Setup: {iscc}")

    passthrough: list[str] = []
    if args.console:
        passthrough.append("--console")
    if args.clean:
        passthrough.append("--clean")

    stale: list[Path] = []
    if APP_EXE.is_file() and not (args.rebuild or args.clean):
        stale = newer_sources(APP_EXE)
        if stale:
            log(f"{APP_EXE.name} старее исходников "
                f"({len(stale)} файлов изменено после сборки), например:")
            for p in stale[:5]:
                try:
                    rel = p.relative_to(ROOT)
                except ValueError:
                    rel = p
                log(f"  {rel}")
            log("Пересобираю, иначе установщик упакует прошлую версию.")
            log("")

    if args.rebuild or args.clean or stale or not APP_EXE.is_file():
        if not APP_EXE.is_file() and not (args.rebuild or args.clean):
            log(f"{APP_EXE} не найден.")
        if not build_app_exe(passthrough):
            return 1
        log("")
    else:
        log(f"Беру готовый {APP_EXE} ({human_size(APP_EXE.stat().st_size)})")
        log("  он свежее всех исходников; пересобрать принудительно: --rebuild")

    version = read_version()
    log(f"Версия: {version}")
    log("")

    if iscc is None:
        zip_path = make_portable(version)
        if zip_path is None:
            return 1
        log("Готово (портативный архив).")
        log(f"Файл:   {zip_path}")
        log(f"Размер: {human_size(zip_path.stat().st_size)}")
        log("")
        log("Распаковал -- запустил, установка не нужна. Полноценный")
        log("установщик соберётся, как только появится Inno Setup:")
        log("  installer\\build_installer.bat --yes")
        return 0

    # /DAppVersion перекрывает значение по умолчанию из #ifndef в .iss,
    # так что номер версии живёт в одном месте -- в gostlib/__init__.py.
    cmd = [str(iscc), f"/DAppVersion={version}", str(ISS)]
    log("Запускаю ISCC (сжатие lzma2/max занимает минуту-другую)...")
    log("")
    try:
        rc = subprocess.call(cmd, cwd=str(HERE))
    except OSError as exc:
        log(f"Не удалось запустить ISCC: {exc}")
        return 1
    if rc != 0:
        log("")
        log(f"ISCC завершился с кодом {rc}. Смотрите сообщения выше:")
        log("  * 'Unknown value' у ArchitecturesAllowed -- у вас Inno Setup")
        log(f"    старее 6.3, обновите: {ISS_DOWNLOAD_URL};")
        log("  * 'file not found' -- проверьте, что dist\\GostLib.exe на месте;")
        log("  * 'cannot create' -- закройте открытый GostLib-Setup из dist\\.")
        return rc

    setup = DIST_DIR / f"GostLib-Setup-{version}.exe"
    log("")
    if setup.is_file():
        log("Готово.")
        log(f"Файл:   {setup}")
        log(f"Размер: {human_size(setup.stat().st_size)}")
        zip_path = make_portable(version)
        if zip_path is not None:
            log(f"Плюс портативный архив: {zip_path.name} "
                f"({human_size(zip_path.stat().st_size)})")
        log("")
        log("Это обычный установщик: ставит программу в Program Files (или")
        log("только для текущего пользователя, без администратора), делает")
        log("ярлыки и запись в «Установка и удаление программ».")
        return 0
    log(f"ISCC отработал, но {setup} не появился.")
    log(f"Проверьте содержимое {DIST_DIR}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
