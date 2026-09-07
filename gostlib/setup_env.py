"""
Подготовка рабочей папки:  python -m gostlib.setup_env

Создаёт каталог, копирует скрипт для Altium и подставляет в него путь,
затем печатает, что и куда прописать в Altium.
"""
from __future__ import annotations

import os
import sys

from . import config
from .service import Service


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    root = argv[0] if argv else ""
    cfg = config.load(root) if root else config.load()
    cfg.save()
    svc = Service(cfg, log=print)
    script = svc.ensure_altium_script()
    svc.close()

    print()
    print("=" * 68)
    print(" GostLib установлен")
    print("=" * 68)
    print(f" Рабочая папка         : {cfg.root}")
    print(f" Каталог компонентов   : {cfg.db_path}")
    print(f" Папка библиотеки      : {cfg.lib_dir}")
    print(f" 3D-модели             : {cfg.models_dir}")
    print(f" Скрипт для Altium     : {script}")
    print()
    print(" Один раз подключите скрипт в Altium:")
    print("   DXP -> Preferences -> Scripting System -> Global Projects")
    print("   -> Add  ->  " + os.path.join(svc.script_dir(),
                                           "GostLibBuilder.PrjScr"))
    print()
    print(" После этого скрипт всегда доступен: DXP -> Run Script ->")
    print("   GostLibBuilder -> RunGostLib")
    print("=" * 68)
    try:
        import PySide6  # noqa: F401
        print(" PySide6: установлен, GUI запустится (run.bat)")
    except ImportError:
        print(" PySide6 не установлен -- GUI не запустится.")
        print(" Выполните:  pip install PySide6")
    try:
        import olefile  # noqa: F401
    except ImportError:
        print(" olefile не установлен -- не читаются .SchLib/.PcbLib.")
        print(" Выполните:  pip install olefile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
