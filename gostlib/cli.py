"""
Консольный интерфейс GostLib -- то же, что делает GUI, только без окна.
Удобен для проверки и для пакетного импорта.

    python -m gostlib.cli import  <файл.zip|папка>
    python -m gostlib.cli kicad   --sym Device:R --fp Resistor_SMD:R_0805_2012Metric
    python -m gostlib.cli lcsc    C2040
    python -m gostlib.cli list    [--type mcu] [--find rp2040]
    python -m gostlib.cli preview <uid> [--out sym.svg]
    python -m gostlib.cli build   [--all | uid1 uid2 ...]   -- задание для Altium
    python -m gostlib.cli fpinfo  [запрос]                  -- габариты посадок
    python -m gostlib.cli model   <uid> <файл.step>         -- 3D-модель
    python -m gostlib.cli kicad-index
    python -m gostlib.cli selftest
    python -m gostlib.cli config  [--set ключ=значение ...]
"""
from __future__ import annotations

import argparse
import os
import sys

from . import classify, config
from .render import svg
from .service import Service


def _svc() -> Service:
    return Service(config.load(), log=lambda s: print(s))


def cmd_import(a):
    s = _svc()
    comps = s.import_archive(a.path)
    print(f"Импортировано: {len(comps)}")
    for c in comps:
        print(f"  {c.uid}  {c.name}")


def cmd_kicad(a):
    s = _svc()
    comps = s.import_kicad(a.sym or "", a.fp or "")
    for c in comps:
        print(f"  {c.uid}  {c.name}")


def cmd_lcsc(a):
    s = _svc()
    for c in s.import_lcsc(a.code):
        print(f"  {c.uid}  {c.name}")


def cmd_list(a):
    s = _svc()
    rows = s.db.search(a.find or "", a.type or "")
    for r in rows:
        print(f"{r['uid']}  {r['ctype']:<12} {r['name'][:34]:<34} "
              f"{(r['mpn'] or '')[:18]:<18} выв.{r['pincount']:<4} "
              f"{'в либе' if r['in_library'] else ''}")
    print(f"Всего: {len(rows)}")


def cmd_preview(a):
    s = _svc()
    c = s.db.get(a.uid)
    if not c:
        print("Не найдено")
        return 1
    out = a.out or f"{c.name}.svg"
    with open(out, "w", encoding="utf-8") as f:
        f.write(svg.symbol_svg(c, s.style()))
    print("Символ:", out)
    if c.footprints and c.footprints[0].pads:
        out2 = os.path.splitext(out)[0] + "_fp.svg"
        with open(out2, "w", encoding="utf-8") as f:
            f.write(svg.footprint_svg(c.footprints[0]))
        print("Посадка:", out2)


def cmd_build(a):
    s = _svc()
    # Без аргументов собирается ЦЕЛЬ: состав текущего проекта либо общая
    # библиотека. Явно перечисленные uid (или --all) -- разовая сборка
    # ровно их, состав проекта при этом не меняется.
    uids = s.db.all_uids() if a.all else list(a.uids or [])
    res = s.build_script_job(uids, only=bool(uids))
    for k, v in res.items():
        if k != "hint":
            print(f"{k}: {v}")
    print()
    print(res.get("hint", ""))


def cmd_kicad_index(a):
    s = _svc()
    st = s.index_kicad(lambda d, t, txt: print(f"\r[{d}/{t}] {txt[:60]:<60}",
                                               end="", flush=True))
    print()
    print(f"Символов: {st['symbols']}, посадок: {st['footprints']}, "
          f"библиотек: {st['files']}")


def cmd_convert(a):
    """Все три пути конвертации одной командой."""
    svc = Service(log=print)
    uids = a.uids or svc.db.all_uids()
    if not uids:
        print("Каталог пуст")
        return 1
    res = svc.build_variant(a.kind, uids)
    print()
    for k in ("path", "dir", "pretty", "schlib", "pcblib", "job",
              "components", "footprints", "models"):
        if res.get(k):
            print(f"{k}: {res[k]}")
    if res.get("hint"):
        print()
        print(res["hint"])
    return 0


def cmd_fpinfo(a):
    """Габариты посадочного места без открытия Altium."""
    s = _svc()
    rows = s.db.search(a.query, "")
    if not rows:
        print("Ничего не нашлось")
        return 1
    for r in rows[:a.limit]:
        c = s.db.get(r["uid"])
        if not c:
            continue
        print(f"=== {c.name}  [{r['uid']}]")
        infos = s.footprint_info(c.uid)
        if not infos:
            print("  посадочного места нет")
        for i, info in enumerate(infos):
            for line in info.lines():
                print("  " + line)
            fp = c.footprints[i]
            if fp.model and fp.model.path:
                from .step3d import parse, compare_with_footprint
                m = parse(fp.model.path)
                for line in m.lines() + compare_with_footprint(m, fp):
                    print("  " + line)
        print()
    return 0


def cmd_model(a):
    """Подгрузить или заменить 3D-модель посадочного места."""
    s = _svc()
    c = s.set_model(a.uid, a.path, a.index, dz=a.dz, rx=a.rx, ry=a.ry, rz=a.rz)
    if not c:
        return 1
    for info in s.footprint_info(a.uid):
        print(info.text())
        print()
    return 0


def cmd_lint(a):
    """Проверить синтаксис скрипта для Altium, не запуская Altium."""
    from .altium import paslint
    return paslint.main(a.files)


def cmd_selftest(a):
    from .selftest import main as st_main
    return st_main()


def cmd_backup(a):
    """Резервная копия каталога и библиотек -- работает и без интерфейса."""
    svc = Service(config.load(), log=print)
    print(svc.backup(getattr(a, "note", "") or ""))
    return 0


def cmd_clear(a):
    """
    Очистить каталог. Копия делается всегда, до удаления: если утилита
    зависла на кривом компоненте, это единственный способ разгрестись.
    """
    svc = Service(config.load(), log=print)
    n = svc.db.stats().get("total", 0)
    if not getattr(a, "yes", False):
        ans = input(f"Убрать все компоненты из каталога ({n} шт.)? "
                    f"Копия будет сделана. [y/N] ").strip().lower()
        if ans not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return 1
    print("Копия:", svc.backup("before-clear"))
    res = svc.clear_library(drop_files=getattr(a, "files", False))
    print(f"Убрано компонентов: {res['components']}")
    return 0


def cmd_setup3d(a):
    """
    Доставить пакеты для просмотра 3D телом.

    Ставим их тем же интерпретатором, каким запущена сама программа --
    иначе легко попасть в системный Python вместо .venv, и программа
    установленных пакетов не увидит.
    """
    import subprocess
    from . import mesh3d
    if mesh3d.available() and not getattr(a, "force", False):
        print("cascadio и trimesh уже стоят -- просмотр телом работает.")
        return 0
    print(f"Ставлю cascadio и trimesh в {sys.executable}")
    r = subprocess.call([sys.executable, "-m", "pip", "install",
                         "cascadio", "trimesh"])
    if r != 0:
        print("Установка не удалась. Проверьте интернет или прокси.")
        return r
    import importlib
    importlib.invalidate_caches()
    importlib.reload(mesh3d)
    print("Готово." if mesh3d.available() else
          "Пакеты поставились, но не импортируются -- перезапустите программу.")
    return 0


def cmd_config(a):
    cfg = config.load()
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        if not hasattr(cfg, k):
            print(f"нет такого параметра: {k}")
            continue
        cur = getattr(cfg, k)
        if isinstance(cur, bool):
            v2 = v.lower() in ("1", "true", "да", "yes", "on")
        elif isinstance(cur, int):
            v2 = int(v)
        else:
            v2 = v
        setattr(cfg, k, v2)
    cfg.save()
    for f in sorted(vars(cfg)):
        print(f"{f} = {getattr(cfg, f)}")
    print(f"\nфайл настроек: {cfg.cfg_path}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gostlib", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import", help="импорт архива/папки")
    p.add_argument("path")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("kicad", help="импорт из установленного KiCad")
    p.add_argument("--sym")
    p.add_argument("--fp")
    p.set_defaults(func=cmd_kicad)

    p = sub.add_parser("lcsc", help="импорт по коду LCSC (EasyEDA)")
    p.add_argument("code")
    p.set_defaults(func=cmd_lcsc)

    p = sub.add_parser("list", help="список компонентов в каталоге")
    p.add_argument("--type")
    p.add_argument("--find")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("preview", help="SVG-превью")
    p.add_argument("uid")
    p.add_argument("--out")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("build", help="собрать задание для Altium")
    p.add_argument("uids", nargs="*")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("convert",
                       help="конвертация: script | kicad | eagle | binary")
    p.add_argument("kind", choices=["script", "kicad", "eagle", "binary"])
    p.add_argument("uids", nargs="*")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("kicad-index", help="переиндексировать библиотеки KiCad")
    p.set_defaults(func=cmd_kicad_index)

    p = sub.add_parser("fpinfo", help="габариты посадочных мест")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_fpinfo)

    p = sub.add_parser("model", help="подгрузить/заменить 3D-модель")
    p.add_argument("uid")
    p.add_argument("path")
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--dz", type=float, default=0.0)
    p.add_argument("--rx", type=float, default=0.0)
    p.add_argument("--ry", type=float, default=0.0)
    p.add_argument("--rz", type=float, default=0.0)
    p.set_defaults(func=cmd_model)

    p = sub.add_parser("lint", help="проверить синтаксис скрипта для Altium")
    p.add_argument("files", nargs="*")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("selftest", help="самопроверка без Altium")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("backup", help="резервная копия каталога и библиотек")
    p.add_argument("--note", default="", help="пометка в имени файла")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("clear", help="очистить каталог (с резервной копией)")
    p.add_argument("--yes", action="store_true", help="не переспрашивать")
    p.add_argument("--files", action="store_true",
                   help="удалить и собранные .SchLib/.PcbLib/.IntLib")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("setup3d",
                       help="доставить пакеты для просмотра 3D телом")
    p.add_argument("--force", action="store_true",
                   help="ставить, даже если пакеты уже найдены")
    p.set_defaults(func=cmd_setup3d)

    p = sub.add_parser("config", help="показать/изменить настройки")
    p.add_argument("--set", action="append")
    p.set_defaults(func=cmd_config)

    a = ap.parse_args(argv)
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
