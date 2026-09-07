"""Точка входа GUI:  python -m gostlib.gui.app"""
from __future__ import annotations

import os
import sys
import traceback


def crash_log_path() -> str:
    """Куда писать причину падения. Рядом с каталогом, там же журналы."""
    try:
        from .. import config
        root = config.default_root()
    except Exception:
        root = os.path.expanduser("~")
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        root = os.path.expanduser("~")
    return os.path.join(root, "crash.log")


def _write_crash(text: str) -> str:
    """
    Записать трассировку на диск.

    Собранный exe запускается без консоли: если он падает при старте,
    окно просто закрывается и никаких следов не остаётся. Файл рядом с
    каталогом -- единственный способ понять, что случилось.
    """
    path = crash_log_path()
    import time
    head = (f"\n{'=' * 70}\n{time.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"GostLib\nPython {sys.version}\n"
            f"Запущен из: {sys.executable}\n"
            f"Заморожен (exe): {bool(getattr(sys, 'frozen', False))}\n"
            f"{'=' * 70}\n")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(head + text + "\n")
    except OSError:
        pass
    return path


def _show_crash(text: str, path: str):
    """Показать окно с причиной, если Qt ещё жив; иначе -- средствами Windows."""
    short = text.strip().splitlines()[-1] if text.strip() else "неизвестно"
    msg = (f"GostLib не смог запуститься.\n\n{short}\n\n"
           f"Подробности записаны в:\n{path}")
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication([])
        box = QMessageBox()
        box.setWindowTitle("GostLib — ошибка запуска")
        box.setIcon(QMessageBox.Critical)
        box.setText(msg)
        box.setDetailedText(text)
        box.exec()
        return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "GostLib — ошибка запуска", 0x10)
    except Exception:
        print(msg, file=sys.stderr)


def _install_hook():
    """Ловить и записывать всё, что вылетело мимо обработчиков."""
    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        path = _write_crash(text)
        _show_crash(text, path)
    sys.excepthook = hook


def main():
    _install_hook()
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as e:
        text = ("Не установлен PySide6 или он не попал в сборку.\n"
                + traceback.format_exc())
        path = _write_crash(text)
        _show_crash(text, path)
        print("Не установлен PySide6.\n"
              "Запустите install.bat или выполните:  pip install PySide6")
        return 2

    try:
        from .. import config
        from .main_window import DARK_QSS, LIGHT_QSS, MainWindow, app_icon

        os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
        if os.name == "nt":
            # Без своего AppUserModelID Windows считает окно частью
            # python.exe и показывает на панели задач иконку питона.
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "GostLib.Altium.Library.Tool")
            except Exception:
                pass
        app = QApplication(sys.argv)
        app.setApplicationName("GostLib")
        app.setApplicationDisplayName("GostLib")
        cfg = config.load()
        app.setStyleSheet(LIGHT_QSS if cfg.theme == "light" else DARK_QSS)
        icon = app_icon()
        app.setWindowIcon(icon)
        w = MainWindow(cfg)
        w.setWindowIcon(icon)
        w.show()
        return app.exec()
    except Exception:
        text = traceback.format_exc()
        path = _write_crash(text)
        _show_crash(text, path)
        return 1


if __name__ == "__main__":
    sys.exit(main())
