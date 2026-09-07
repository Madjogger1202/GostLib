"""
Прикладная логика: импорт, каталог, сборка задания для Altium.

GUI и CLI работают только через этот модуль, поэтому всё можно проверить
без Qt.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import classify, config
from .db import Catalog
from .emit import job as jobmod
from .gost import symbolgen
from .ir import Component, Footprint, Model3D, short_fp_name
from .sources import archive, easyeda
from .sources import kicad as kc

Logger = Callable[[str], None]


def _safe_name(name: str) -> str:
    """Имя файла библиотеки из имени проекта: без пробелов и запретных знаков."""
    out = re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "_", (name or "").strip())
    return out.strip("_")


def _read_text(path: str) -> str:
    """Прочитать текст, не гадая о кодировке."""
    try:
        raw = open(path, "rb").read()
    except OSError:
        return ""
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin1")


class Service:
    def __init__(self, cfg: Optional[config.Config] = None,
                 log: Optional[Logger] = None):
        self.cfg = cfg or config.load()
        self.cfg.ensure_dirs()
        self.db = Catalog(self.cfg.db_path)
        self.log: Logger = log or (lambda s: None)
        from .undo import UndoStack
        self.undo = UndoStack(self.db, lambda t: self.log(t))
        kc.set_only_root(getattr(self.cfg, "kicad_root", ""))
        try:
            self.ensure_altium_script()
        except Exception:
            pass

    # ------------------------------------------------- скрипт для Altium ----
    def script_dir(self) -> str:
        return os.path.join(self.cfg.root, "altium")

    def ensure_altium_script(self) -> str:
        """
        Скопировать GostLibBuilder.pas/.PrjScr в рабочую папку и подставить
        в него путь к файлу-указателю. Делается при каждом запуске, поэтому
        скрипт всегда актуален.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        cands = [os.path.join(here, "altium"),
                 os.path.join(os.path.dirname(here), "altium")]
        src = ""
        for d in cands:
            if os.path.isfile(os.path.join(d, "GostLibBuilder.pas")):
                src = d
                break
        if not src:
            return ""
        dst = self.script_dir()
        os.makedirs(dst, exist_ok=True)
        ptr = os.path.join(self.cfg.root, "current_job.txt")
        text = _read_text(os.path.join(src, "GostLibBuilder.pas"))
        # исходник может лежать уже с CRLF -- нормализуем, иначе при записи
        # с newline="\r\n" получится "\r\r\n" и Altium покажет файл
        # вдвое длиннее, с пустой строкой после каждой строки
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("@@POINTER@@", ptr)
        out = os.path.join(dst, "GostLibBuilder.pas")
        old = _read_text(out) if os.path.isfile(out) else ""
        if old != text:
            # Altium читает .pas как ANSI, поэтому кириллица в сообщениях
            # должна лежать в CP1251 -- иначе в редакторе скрипта каша.
            with open(out, "w", encoding="cp1251", errors="replace",
                      newline="\r\n") as f:
                f.write(text)
        prj_src = os.path.join(src, "GostLibBuilder.PrjScr")
        if os.path.isfile(prj_src):
            prj_dst = os.path.join(dst, "GostLibBuilder.PrjScr")
            if not os.path.isfile(prj_dst):
                shutil.copy2(prj_src, prj_dst)
        return out

    def close(self):
        self.db.close()

    # ----------------------------------------------------------- символы ----
    def style(self):
        return self.cfg.to_style()

    def rebuild_symbol(self, c: Component) -> Component:
        symbolgen.build(c, self.style_for(c))
        return c

    def style_for(self, c: Component):
        """Стиль для конкретного компонента: с его правилами группировки."""
        return self.style().with_rules(c, getattr(self.cfg, "pin_rules", ""))

    def _absorb(self, comps: List[Component], rebuild: bool = True
                ) -> List[Component]:
        out = []
        for c in comps:
            if not c.name:
                continue
            if not c.raw_pins:
                c.raw_pins = list(c.symbol.pins)
            if rebuild and c.raw_pins:
                self.rebuild_symbol(c)
            c.designator = c.designator or classify.designator_for(c.ctype)
            c.created = c.created or time.strftime("%Y-%m-%d %H:%M")
            self._localize_models(c)
            self.db.upsert(c)
            # Импортированное сразу считается частью общей библиотеки:
            # иначе сборка «общей» молча пропускала бы всё, что ещё ни
            # разу не собирали. Убрать -- меню «Пометить как “не в
            # библиотеке”».
            self.db.set_in_library([c.uid], True)
            out.append(c)
            self.log(f"+ {c.name} [{classify.CTYPE_NAME.get(c.ctype, c.ctype)}]"
                     f" выводов {len(c.symbol.pins)}"
                     f"{', посадка ' + c.footprints[0].name if c.footprints else ''}")
        self._adopt_into_project(out)
        return out

    def _adopt_into_project(self, comps: List[Component]) -> int:
        """
        Свежий импорт сразу входит в текущий проект.

        Иначе выходило так: работаешь в проекте, импортируешь компонент, а
        он оседает в общем каталоге. При включённом показе «только проект»
        его в таблице нет -- и добавить в проект нечем, потому что выбрать
        нечего. Компонент при этом всё равно остаётся и в общем каталоге:
        проект хранит ссылки, а не копии.
        """
        if not comps or not getattr(self.cfg, "import_to_project", True):
            return 0
        pr = self.active_project()
        if pr is None:
            return 0
        uids = [c.uid for c in comps if c.uid]
        if not uids:
            return 0
        n = self.project_add(int(pr["id"]), uids)
        if n:
            self.log(f"  → в проект «{pr['name']}»: {n}")
        return n

    def _localize_models(self, c: Component):
        """Скопировать 3D-модели и вендорские .PcbLib в рабочую папку."""
        for fp in c.footprints:
            # вендорские имена не трогаем -- ссылка должна совпадать с их файлом
            if not fp.is_external():
                short = short_fp_name(fp.name)
                if short != fp.name:
                    self.log(f"  имя посадки укорочено: {fp.name} -> {short}")
                    fp.name = short
            if fp.model and fp.model.path and os.path.isfile(fp.model.path):
                dst = os.path.join(self.cfg.models_dir,
                                   os.path.basename(fp.model.path))
                if os.path.abspath(fp.model.path) != os.path.abspath(dst):
                    try:
                        shutil.copy2(fp.model.path, dst)
                        fp.model.path = dst
                    except Exception as e:
                        self.log(f"  3D не скопирована: {e}")
                else:
                    fp.model.path = dst
            if fp.source_pcblib and os.path.isfile(fp.source_pcblib):
                vend = os.path.join(self.cfg.lib_dir, "vendor")
                os.makedirs(vend, exist_ok=True)
                dst = os.path.join(vend, os.path.basename(fp.source_pcblib))
                if os.path.abspath(fp.source_pcblib) != os.path.abspath(dst):
                    try:
                        shutil.copy2(fp.source_pcblib, dst)
                    except Exception as e:
                        self.log(f"  библиотека посадок не скопирована: {e}")
                fp.source_pcblib = dst

    # ------------------------------------------------------------ импорт ----
    def import_archive(self, path: str) -> List[Component]:
        self.log(f"Разбираю {os.path.basename(path)} ...")
        sc = archive.open_archive(path)
        try:
            self.log(f"Тип содержимого: {sc.kind()}")
            comps = archive.components(sc, self.log)
            if not comps:
                self.log("Ничего пригодного не найдено.")
            return self._absorb(comps)
        finally:
            sc.cleanup()

    # -------------------------------------------------------- KiCad --------
    def index_kicad(self, progress=None) -> Dict[str, int]:
        """
        Построить индекс установленных библиотек KiCad. Делается один раз,
        дальше поиск идёт по базе и отвечает мгновенно.
        """
        kc.set_only_root(getattr(self.cfg, "kicad_root", ""))
        rows = kc.scan_all(progress)
        self.db.set_kicad_index(rows)
        st = self.db.kicad_stats()
        self.log(f"Индекс KiCad: символов {st['symbols']}, "
                 f"посадок {st['footprints']} (файлов {st['files']})")
        return st

    def project_add(self, pid: int, uids) -> int:
        """Добавить в проект (под откат)."""
        with self.undo.step("Добавить в проект"):
            self.undo.touch_project(pid)
            return self.db.project_add(pid, uids)

    def project_remove(self, pid: int, uids) -> int:
        with self.undo.step("Убрать из проекта"):
            self.undo.touch_project(pid)
            return self.db.project_remove(pid, uids)

    def delete(self, uids) -> int:
        """Удалить компоненты (под откат -- вернуть можно Ctrl+Z)."""
        uids = list(uids)
        with self.undo.step(f"Удалить компонентов: {len(uids)}"):
            self.undo.touch_many(uids)
            self.db.delete(uids)
        return len(uids)

    def refresh_native(self, uid: str) -> Optional[Component]:
        """
        Перечитать родное обозначение из исходного .kicad_sym.

        Компоненты, импортированные до появления родной графики, её не
        имеют -- переключатель «как в источнике» у них пустой. Здесь мы
        находим исходный файл по source_ref и забираем графику и позиции
        выводов заново, ничего больше не трогая.
        """
        c = self.db.get(uid)
        if not c:
            return None
        self.undo.touch(uid)
        ref = c.source_ref or ""
        if "::" not in ref:
            raise ValueError(f"{c.name}: неизвестно, откуда он импортирован")
        fname, sym_name = ref.split("::", 1)
        rows = self.db.kicad_search("sym", sym_name, 20)
        path = ""
        for r in rows:
            if os.path.basename(r["path"]).lower() == fname.lower() \
                    and r["name"] == sym_name:
                path = r["path"]
                break
        if not path:
            hits = kc.find_symbol(sym_name)
            path = hits[0][1] if hits else ""
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(
                f"{c.name}: файл {fname} не найден. Обновите индекс KiCad.")
        fresh = kc.component_from_kicad_sym(path, sym_name)
        c.native_prims = list(fresh.native_prims)
        c.native_pins = list(fresh.native_pins)
        if not c.native_prims:
            self.log(f"{c.name}: в {fname} у символа нет своей графики")
        c.symbol.manual_layout = False
        self.rebuild_symbol(c)
        self.db.upsert(c)
        self.log(f"{c.name}: обозначение перечитано из {os.path.basename(path)}"
                 f" ({len(c.native_prims)} фигур, {len(c.native_pins)} выводов)")
        return c

    def kicad_stats(self) -> Dict[str, int]:
        return self.db.kicad_stats()

    def kicad_find(self, kind: str, query: str, limit: int = 400):
        """kind: 'sym' | 'fp'. Возвращает строки индекса."""
        return self.db.kicad_search(kind, query, limit)

    def _resolve_sym(self, ref: str):
        rows = self.db.kicad_search("sym", ref, 5)
        if rows:
            r = rows[0]
            return (r["nickname"], r["path"], r["name"])
        hits = kc.find_symbol(ref)
        return hits[0] if hits else None

    def _resolve_fp(self, ref: str):
        rows = self.db.kicad_search("fp", ref, 5)
        if rows:
            r = rows[0]
            p = r["path"]
            if os.path.isdir(p):
                p = os.path.join(p, r["name"] + ".kicad_mod")
            return (r["nickname"], p)
        hits = kc.find_footprint(ref)
        return hits[0] if hits else None

    def import_kicad(self, sym_ref: str = "", fp_ref: str = "",
                     sym_path: str = "", sym_name: str = "",
                     fp_path: str = "") -> List[Component]:
        """
        Импорт из KiCad. Можно задать либо текстовые ссылки (`Device:R`,
        `Resistor_SMD:R_0805_2012Metric`), либо точные пути -- их передаёт
        диалог, когда пользователь выбрал строку в списке.
        """
        comps: List[Component] = []
        if not sym_path and sym_ref:
            hit = self._resolve_sym(sym_ref)
            if not hit:
                raise FileNotFoundError(
                    f"Символ '{sym_ref}' не найден. Если индекс KiCad пуст, "
                    f"нажмите «Обновить индекс».")
            _nick, sym_path, sym_name = hit
        if sym_path:
            if not sym_name:
                names = list(kc.parse_kicad_sym(sym_path).keys())
                if not names:
                    raise ValueError(f"В {os.path.basename(sym_path)} нет символов")
                sym_name = names[0]
            self.log(f"Читаю {os.path.basename(sym_path)} :: {sym_name} ...")
            comps.append(kc.component_from_kicad_sym(sym_path, sym_name))

        if not fp_path:
            ref = fp_ref or (comps[0].params.get("KiCadFootprint", "")
                             if comps else "")
            if ref:
                hit = self._resolve_fp(ref)
                if hit:
                    fp_path = hit[1]
                else:
                    self.log(f"Посадка '{ref}' не найдена")
        if fp_path and os.path.isfile(fp_path):
            self.log(f"Читаю {os.path.basename(fp_path)} ...")
            fp = kc.parse_kicad_mod(fp_path)
            if fp.model and fp.model.path:
                r = kc.resolve_3d(fp.model.path)
                fp.model.path = r or ""
                if not r:
                    self.log("  3D-модель не найдена (в KiCad обычно .wrl, "
                             "Altium понимает только .step)")
            if comps:
                comps[0].footprints.append(fp)
            else:
                c = Component(name=fp.name, ctype="other", source="kicad",
                              source_ref=fp_path)
                c.footprints.append(fp)
                comps.append(c)
        return self._absorb(comps)

    def import_lcsc(self, code: str) -> List[Component]:
        self.log(f"Запрашиваю {code} в EasyEDA ...")
        c = easyeda.fetch(code, out_dir=self.cfg.models_dir, log=self.log)
        return self._absorb([c])

    # ------------------------------------------------------------ выгрузка --
    def collect(self, uids: Iterable[str]) -> List[Component]:
        out = []
        for u in uids:
            c = self.db.get(u)
            if c:
                out.append(c)
        return out

    def _atomic_write(self, path: str, writer) -> str:
        """
        Записать во временный файл и подменить целевой. Если Altium держит
        файл открытым, Windows не даст его заменить -- тогда результат
        остаётся рядом как <имя>.new, а подменит его скрипт в Altium.
        """
        tmp = path + ".tmp"
        writer(tmp)
        try:
            os.replace(tmp, path)
            return path
        except PermissionError:
            newp = path + ".new"
            try:
                if os.path.exists(newp):
                    os.remove(newp)
                os.replace(tmp, newp)
            except Exception:
                pass
            self.log(f"  {os.path.basename(path)} занят Altium -- новая "
                     f"версия рядом как .new, подменит скрипт")
            return newp

    def export_schlib(self, comps: List[Component]) -> str:
        """
        Записать .SchLib напрямую, без Altium. Библиотека -- полная проекция
        выбранных компонентов, а не дописка, поэтому пересборка идемпотентна.
        """
        from .emit.schlib import write_schlib
        os.makedirs(self.cfg.lib_dir, exist_ok=True)
        path = os.path.join(self.cfg.lib_dir, self.cfg.library_name + ".SchLib")
        st = self.style()
        self._atomic_write(path, lambda t: write_schlib(t, comps, st))
        self.log(f"Схемная библиотека записана: {path} "
                 f"(компонентов {len(comps)})")
        return path

    def export_pcblib(self, comps: List[Component]) -> Tuple[str, int]:
        """
        Записать .PcbLib напрямую. Попадают только собственные посадочные
        места (KiCad/EasyEDA) -- вендорские подключаются своими файлами.
        """
        from .emit.pcblib import write_pcblib
        fps: List[Footprint] = []
        seen = set()
        for c in comps:
            for fp in c.footprints:
                if fp.is_external() or not (fp.pads or fp.prims):
                    continue
                if fp.name in seen:
                    continue
                seen.add(fp.name)
                fps.append(fp)
        path = os.path.join(self.cfg.lib_dir, self.cfg.library_name + ".PcbLib")
        if not fps:
            return ("", 0)
        self._atomic_write(path, lambda t: write_pcblib(t, fps, filename=path))
        self.log(f"Библиотека посадок записана: {path} (посадок {len(fps)})")
        return (path, len(fps))

    def build_job(self, uids: Iterable[str], install: bool = True
                  ) -> Dict[str, str]:
        """
        Собрать библиотеку целиком: .SchLib и .PcbLib пишутся напрямую из
        Python в формат Altium. Скрипт внутри Altium больше не нужен.
        """
        uids = list(uids)
        comps = self.collect(uids)
        if not comps:
            raise ValueError("Не выбрано ни одного компонента")

        self.db.set_in_library(uids, True)
        lib_uids = self.db.all_uids(only_lib=True)
        lib_comps = self.collect(lib_uids) or comps

        os.makedirs(self.cfg.lib_dir, exist_ok=True)
        schlib = self.export_schlib(lib_comps)
        pcblib, n_fp = self.export_pcblib(lib_comps)

        extra: List[str] = []
        for c in lib_comps:
            for fp in c.footprints:
                if fp.source_pcblib and os.path.isfile(fp.source_pcblib):
                    if fp.source_pcblib not in extra:
                        extra.append(fp.source_pcblib)
        if extra:
            self.log(f"Вендорских библиотек посадок рядом: {len(extra)}")

        libs = [schlib] + ([pcblib] if pcblib else []) + extra
        ts = time.strftime("%Y%m%d_%H%M%S")
        job_path = os.path.join(self.cfg.jobs_dir, f"deploy_{ts}.txt")
        log_path = os.path.join(self.cfg.jobs_dir, f"deploy_{ts}.log")
        jobmod.write_deploy(job_path, libs, log_path)
        jobmod.write_pointer(self.cfg.root, job_path)
        pending = [p for p in libs if os.path.exists(p + ".new")]
        if pending:
            self.log(f"Занятых файлов: {len(pending)} -- подменит скрипт "
                     f"в Altium")

        return {"schlib": schlib, "pcblib": pcblib,
                "footprints": str(n_fp), "components": str(len(lib_comps)),
                "vendor": ";".join(extra), "job": job_path, "log": log_path,
                "pending": str(len(pending)), "libs": str(len(libs))}

    # ------------------------------- сборка силами самого Altium (основной) --
    # --------------------------------------------------------- проекты -----
    def active_project(self):
        """Текущий проект или None -- значит работаем с общей библиотекой."""
        pid = int(getattr(self.cfg, "active_project", 0) or 0)
        return self.db.project(pid) if pid else None

    def set_active_project(self, pid: int):
        self.cfg.active_project = int(pid or 0)
        self.cfg.save()

    def project_library(self, pr) -> Tuple[str, str, str]:
        """(папка, имя библиотеки, папка проекта) для проекта."""
        if pr is None:
            return (self.cfg.lib_dir, self.cfg.library_name, "")
        path = pr["path"] or ""
        lib_dir = pr["lib_dir"] or (os.path.join(path, "Libraries") if path
                                    else self.cfg.lib_dir)
        # Имя библиотеки по умолчанию -- по имени проекта: в панели
        # Components сразу видно, чья это библиотека.
        name = pr["lib_name"] or _safe_name(pr["name"]) or self.cfg.library_name
        return (lib_dir, name, path)

    def library_paths(self, pr=None) -> Tuple[str, str]:
        lib_dir, name, _ = self.project_library(
            pr if pr is not None else self.active_project())
        os.makedirs(lib_dir, exist_ok=True)
        return (os.path.join(lib_dir, name + ".SchLib"),
                os.path.join(lib_dir, name + ".PcbLib"))

    def target_uids(self, pr=None) -> List[str]:
        """
        Что войдёт в собираемую библиотеку.

        Для проекта -- только его состав; для общей -- всё, помеченное
        «в библиотеке». Компоненты при этом общие: правка резистора
        доезжает во все проекты, где он есть.
        """
        pr = pr if pr is not None else self.active_project()
        if pr is not None:
            return self.db.project_uids(int(pr["id"]))
        return self.db.all_uids(only_lib=True)

    def build_script_job(self, uids: Iterable[str] = None, install: bool = True,
                         fresh: bool = True, only: bool = False
                         ) -> Dict[str, str]:
        """
        Основной путь: Python готовит задание, а .SchLib/.PcbLib строит сам
        Altium штатным API. Пользователю остаётся один тык по кнопке скрипта.

        Что попадёт в библиотеку:

        * выбран проект -- РОВНО его состав, ничего сверх того. Раньше сюда
          подмешивалось выделение в таблице, а при пустом выделении -- весь
          каталог, который заодно и вписывался в проект. Отсюда и бралась
          «хрень», которой в проекте быть не должно;
        * общая библиотека -- всё, помеченное «в библиотеке»;
        * `only=True` -- ровно переданные `uids`, разовая сборка мимо цели.

        Состав проекта меняется только явно: импортом, добором из каталога
        и меню «Проект». Сборка его не трогает.
        """
        from .emit import altiumjob

        pr = self.active_project()
        uids = list(uids or [])
        if only:
            if not uids:
                raise ValueError("Не выбрано ни одного компонента")
            targets = uids
            self.db.set_in_library(uids, True)
        else:
            targets = self.target_uids(pr)
            if not targets and pr is None:
                # первый запуск: «в библиотеке» ещё ничего не помечено
                targets = self.db.all_uids()
                self.db.set_in_library(targets, True)
        lib_comps = self.collect(targets)
        if not lib_comps:
            if pr is not None:
                raise ValueError(
                    f"В проекте «{pr['name']}» пусто. Добавьте компоненты: "
                    f"кнопка «Взять из каталога…» или импорт при выбранном "
                    f"проекте.")
            raise ValueError("Каталог пуст")
        if pr is not None and not only:
            self.log(f"Библиотека проекта «{pr['name']}»: "
                     f"компонентов {len(lib_comps)} (только состав проекта)")
        elif only:
            self.log(f"Разовая сборка выделенного: {len(lib_comps)}")

        schlib, pcblib = self.library_paths(pr)
        vendor: List[str] = []
        n_fp = 0
        n_3d = 0
        missing_3d: List[str] = []
        seen = set()
        for c in lib_comps:
            for fp in c.footprints:
                if fp.source_pcblib and os.path.isfile(fp.source_pcblib):
                    if fp.source_pcblib not in vendor:
                        vendor.append(fp.source_pcblib)
                    continue
                if not (fp.pads or fp.prims) or fp.name in seen:
                    continue
                seen.add(fp.name)
                n_fp += 1
                if fp.model and fp.model.path:
                    if os.path.isfile(fp.model.path):
                        n_3d += 1
                    else:
                        missing_3d.append(fp.name)

        libpkg = ""
        if getattr(self.cfg, "build_intlib", False):
            from .emit import libpkg as lp
            libpkg = os.path.splitext(schlib)[0] + ".LibPkg"
            lp.write(libpkg, [schlib, pcblib])
            self.log(f"Пакет библиотек: {libpkg}")

        ts = time.strftime("%Y%m%d_%H%M%S")
        job_path = os.path.join(self.cfg.jobs_dir, f"build_{ts}.txt")
        log_path = job_path + ".log"
        altiumjob.write_job(job_path, lib_comps, schlib, pcblib,
                            st=self.style(), log_path=log_path,
                            install=install, vendor_libs=vendor, fresh=fresh,
                            intlib=bool(libpkg), libpkg=libpkg,
                            pin_hot_end=getattr(self.cfg, "pin_location_hot",
                                                False))
        altiumjob.write_pointer(self.cfg.root, job_path)
        # старый отчёт убираем: иначе после неудачной сборки утилита
        # покажет прошлый успешный и «не обновилось» снова пройдёт мимо
        from .emit import buildreport
        buildreport.clear(job_path)

        # Задание уже записано -- проверяем его тем же разбором, каким его
        # читает скрипт. Не отменяем сборку: замечания вида «два вывода в
        # одной точке» ничего не ломают, но объясняют, почему в Альтиуме
        # выводов оказалось меньше, чем в исходнике.
        from .emit import jobcheck
        job_problems: List[str] = []
        try:
            job_problems = jobcheck.check_file(job_path)
        except Exception as e:                       # проверка не критична
            self.log(f"  проверить задание не удалось: {e}")
        if job_problems:
            self.log(f"  замечаний по заданию: {len(job_problems)}")
            for p in job_problems[:20]:
                self.log(f"    {p}")
            if len(job_problems) > 20:
                self.log(f"    ... и ещё {len(job_problems) - 20}")

        self.log(f"Задание готово: {job_path}")
        self.log(f"  символов {len(lib_comps)}, посадок {n_fp}, "
                 f"3D-моделей {n_3d}")
        for nm in missing_3d:
            self.log(f"  3D-модель не найдена на диске, посадка {nm}")
        if vendor:
            self.log(f"  вендорских библиотек посадок: {len(vendor)}")

        where = ("Общая библиотека" if pr is None
                 else f"Проект «{pr['name']}»")
        return {"kind": "script", "job": job_path, "log": log_path,
                "target": f"{where} → {os.path.basename(schlib)}",
                "schlib": schlib, "pcblib": pcblib, "libpkg": libpkg,
                "components": str(len(lib_comps)), "footprints": str(n_fp),
                "models": str(n_3d), "vendor": ";".join(vendor),
                "problems": "\n".join(job_problems),
                "hint": ("В Altium нажмите кнопку GostLib на панели "
                         "(или DXP → Run Script → GostLibBuilder → "
                         "RunGostLib). Altium сам построит и подключит "
                         "библиотеки.")}

    # ----------------------------------------------- запуск скрипта -------
    def find_altium(self) -> str:
        """Путь к X2.EXE: из настроек, иначе поиск по стандартным местам."""
        exe = (getattr(self.cfg, "altium_exe", "") or "").strip()
        if exe and os.path.isfile(exe):
            return exe
        if os.name != "nt":
            return ""
        import glob as _g
        pats = []
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", ""),
                     r"C:\Program Files", r"D:\Program Files"):
            if base:
                pats.append(os.path.join(base, "Altium", "*", "X2.EXE"))
                pats.append(os.path.join(base, "Altium*", "X2.EXE"))
        found = []
        for pat in pats:
            found.extend(_g.glob(pat))
        found.sort()
        return found[-1] if found else ""

    def run_in_altium(self, wait: bool = False) -> str:
        """
        Запустить сборку в Altium без ручного Run Script.

        Altium принимает команду вида

            X2.EXE -RScriptingSystem:RunScript(ProjectName="…\\X.PrjScr"
                   |Document="X.pas"|ProcName="RunGostLib")

        Три вещи, на которых это ломается:

        * разделитель полей -- обычная вертикальная черта. `^|` пишут в
          .bat, где `^` экранирует её для cmd; мы запускаем процесс
          напрямую, и `^` уезжает в Altium частью пути;
        * `Document` обязателен -- без него Altium ищет процедуру в пустом
          модуле и говорит «Module name ''»;
        * если Altium уже открыт, команда уходит в работающую копию.
        """
        exe = self.find_altium()
        if not exe:
            raise FileNotFoundError(
                "Altium не найден. Укажите путь к X2.EXE в настройках.")
        pas = os.path.join(self.script_dir(), "GostLibBuilder.pas")
        if not os.path.isfile(pas):
            self.ensure_altium_script()
        prj = os.path.join(self.script_dir(), "GostLibBuilder.PrjScr")
        if not os.path.isfile(prj):
            prj = self.ensure_script_project()
        arg = (f'-RScriptingSystem:RunScript(ProjectName="{prj}"'
               f'|Document="{os.path.basename(pas)}"'
               f'|ProcName="RunGostLib")')
        import subprocess
        # Командную строку собираем САМИ. Если передать список, Python
        # соберёт её через list2cmdline и заэкранирует внутренние кавычки
        # как \", а Altium покажет путь с лишним слэшем на конце и решит,
        # что проекта нет. Ровно это и было видно в его сообщении.
        cmd = f'"{exe}" {arg}'
        self.log(f"Запускаю Altium: {arg}")
        if os.name == "nt":
            proc = subprocess.Popen(cmd)
        else:
            proc = subprocess.Popen([exe, arg])
        if wait:
            proc.wait()
        return exe

    def build_report(self, job_path: str = ""):
        """Сверка: что просили построить и что скрипт реально построил."""
        from .emit import buildreport
        if not job_path:
            job_path = self.last_job()
        return buildreport.read(job_path)

    def last_job(self) -> str:
        """Последнее задание -- то, на которое смотрит файл-указатель."""
        ptr = os.path.join(self.cfg.root, "current_job.txt")
        if os.path.isfile(ptr):
            txt = _read_text(ptr).strip().splitlines()
            if txt and os.path.isfile(txt[0].strip()):
                return txt[0].strip()
        jobs = []
        if os.path.isdir(self.cfg.jobs_dir):
            for f in os.listdir(self.cfg.jobs_dir):
                if f.startswith("build_") and f.endswith(".txt"):
                    full = os.path.join(self.cfg.jobs_dir, f)
                    jobs.append((os.path.getmtime(full), full))
        jobs.sort(reverse=True)
        return jobs[0][1] if jobs else ""

    def altium_command(self) -> str:
        """Готовая командная строка -- чтобы её можно было показать и скопировать."""
        exe = self.find_altium() or "X2.EXE"
        prj = os.path.join(self.script_dir(), "GostLibBuilder.PrjScr")
        return (f'"{exe}" -RScriptingSystem:RunScript(ProjectName="{prj}"'
                f'|Document="GostLibBuilder.pas"|ProcName="RunGostLib")')

    def ensure_script_project(self) -> str:
        """
        Проект скриптов рядом с .pas. Altium запускает процедуру только из
        проекта, поэтому если его нет -- создаём.
        """
        dst = self.script_dir()
        os.makedirs(dst, exist_ok=True)
        prj = os.path.join(dst, "GostLibBuilder.PrjScr")
        if os.path.isfile(prj):
            return prj
        text = ("[Design]\r\n"
                "Version=1.0\r\n"
                "HierarchyMode=0\r\n"
                "\r\n"
                "[Document1]\r\n"
                "DocumentPath=GostLibBuilder.pas\r\n"
                "AnnotationEnabled=1\r\n"
                "AnnotateStartValue=1\r\n"
                "\r\n")
        with open(prj, "w", encoding="cp1251", errors="replace",
                  newline="") as f:
            f.write(text)
        self.log(f"Создан проект скриптов: {prj}")
        return prj

    # ---------------------------------------------------- место на диске ----
    @staticmethod
    def _dir_size(path: str) -> int:
        if not path or not os.path.isdir(path):
            return 0
        total = 0
        for root, _d, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    @staticmethod
    def _files_size(paths) -> int:
        total = 0
        for p in paths:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total

    def disk_usage(self) -> Dict[str, object]:
        """
        Сколько занимают библиотеки и всё вокруг них.

        3D-модели растут быстрее всего: одна модель из EasyEDA бывает на
        десятки мегабайт. Смотреть на это надо в программе, а не в
        проводнике.
        """
        def lib_files(lib_dir: str, name: str):
            base = os.path.join(lib_dir, name)
            return [base + ext for ext in (".SchLib", ".PcbLib", ".IntLib",
                                           ".LibPkg", ".SchLib.bak",
                                           ".PcbLib.bak")]

        common = self._files_size(lib_files(self.cfg.lib_dir,
                                            self.cfg.library_name))
        projects: Dict[str, int] = {}
        for r in self.db.projects():
            lib_dir, name, _ = self.project_library(r)
            projects[r["name"]] = self._files_size(lib_files(lib_dir, name))

        mesh = os.path.join(self.cfg.models_dir, "_mesh_cache")
        models = max(0, self._dir_size(self.cfg.models_dir)
                     - self._dir_size(mesh))
        out = {
            "common": common,
            "projects": projects,
            "models": models,
            "mesh_cache": self._dir_size(mesh),
            "jobs": self._dir_size(self.cfg.jobs_dir),
            "backups": self._dir_size(self.backup_dir()),
            "catalog": self._files_size([self.cfg.db_path]),
        }
        out["total"] = (common + sum(projects.values()) + models
                        + out["mesh_cache"] + out["jobs"] + out["backups"]
                        + out["catalog"])
        return out

    def clean_temp(self, keep_jobs: int = 5) -> int:
        """
        Убрать то, что восстанавливается само: кеш сеток и старые задания.
        Возвращает освобождённые байты. Библиотеки и модели не трогает.
        """
        freed = 0
        mesh = os.path.join(self.cfg.models_dir, "_mesh_cache")
        if os.path.isdir(mesh):
            freed += self._dir_size(mesh)
            shutil.rmtree(mesh, ignore_errors=True)
        jobs = []
        if os.path.isdir(self.cfg.jobs_dir):
            for f in os.listdir(self.cfg.jobs_dir):
                full = os.path.join(self.cfg.jobs_dir, f)
                if os.path.isfile(full):
                    jobs.append((os.path.getmtime(full), full))
        jobs.sort(reverse=True)
        for _t, full in jobs[keep_jobs * 2:]:     # задание + его журнал
            try:
                freed += os.path.getsize(full)
                os.remove(full)
            except OSError:
                pass
        self.log(f"Освобождено: {freed / 1024 ** 2:.1f} МБ")
        return freed

    # ------------------------------------------- резервная копия и очистка --
    def backup_dir(self) -> str:
        return os.path.join(self.cfg.root, "backups")

    def backup(self, note: str = "") -> str:
        """
        Сложить в один .zip всё, что нажито: каталог, настройки, собранные
        библиотеки, 3D-модели и задания. Имя -- с датой и временем, чтобы
        копии не перетирали друг друга.
        """
        import zipfile
        os.makedirs(self.backup_dir(), exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H%M")
        tag = ("_" + re.sub(r"[^\w.-]+", "-", note)) if note else ""
        path = os.path.join(self.backup_dir(), f"gostlib_{stamp}{tag}.zip")
        n = 1
        while os.path.exists(path):
            path = os.path.join(self.backup_dir(),
                                f"gostlib_{stamp}{tag}_{n}.zip")
            n += 1

        # каталог пишется прямо сейчас -- копируем его штатной выгрузкой
        # SQLite, иначе в архив может попасть половина транзакции
        tmp_db = os.path.join(self.cfg.root, "_backup.sqlite")
        try:
            import sqlite3
            dst = sqlite3.connect(tmp_db)
            with dst:
                self.db.conn.backup(dst)
            dst.close()
        except Exception as e:
            self.log(f"Копия базы штатным способом не вышла ({e}), "
                     f"беру файл как есть")
            tmp_db = self.cfg.db_path

        added = 0
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            if os.path.isfile(tmp_db):
                z.write(tmp_db, "catalog.sqlite")
                added += 1
            if os.path.isfile(self.cfg.cfg_path):
                z.write(self.cfg.cfg_path, "settings.json")
                added += 1
            for folder, inside in ((self.cfg.lib_dir, "library"),
                                   (self.cfg.models_dir, "models"),
                                   (self.cfg.jobs_dir, "jobs")):
                if not os.path.isdir(folder):
                    continue
                for root, _dirs, files in os.walk(folder):
                    for f in files:
                        full = os.path.join(root, f)
                        rel = os.path.relpath(full, folder)
                        try:
                            z.write(full, os.path.join(inside, rel))
                            added += 1
                        except OSError:
                            pass
        if tmp_db != self.cfg.db_path and os.path.isfile(tmp_db):
            try:
                os.remove(tmp_db)
            except OSError:
                pass
        size = os.path.getsize(path) / 1e6
        self.log(f"Резервная копия: {path} ({added} файлов, {size:.1f} МБ)")
        return path

    def clear_library(self, drop_files: bool = False) -> Dict[str, int]:
        """
        Очистить каталог. Резервную копию делает вызывающий -- здесь только
        удаление, чтобы не было соблазна «а вдруг копия не удалась».
        """
        n = self.db.clear_components()
        removed = 0
        if drop_files:
            for name in (self.cfg.library_name + ".SchLib",
                         self.cfg.library_name + ".PcbLib",
                         self.cfg.library_name + ".IntLib",
                         self.cfg.library_name + ".LibPkg"):
                f = os.path.join(self.cfg.lib_dir, name)
                if os.path.isfile(f):
                    try:
                        os.remove(f)
                        removed += 1
                    except OSError as e:
                        self.log(f"Не удалось удалить {name}: {e}")
        self.log(f"Каталог очищен: убрано компонентов {n}"
                 + (f", файлов библиотеки {removed}" if drop_files else ""))
        return {"components": n, "files": removed}

    # ------------------------------------------------------------ 3D-модели --
    def _fp_of(self, uid: str, fp_index: int = 0):
        c = self.db.get(uid)
        if not c or not c.footprints:
            return (None, None)
        if fp_index < 0 or fp_index >= len(c.footprints):
            return (c, None)
        return (c, c.footprints[fp_index])

    def footprint_info(self, uid: str) -> List:
        """Габариты и прочие сведения о посадочных местах компонента."""
        from . import fpinfo
        c = self.db.get(uid)
        if not c:
            return []
        return [fpinfo.describe(fp) for fp in c.footprints]

    def set_model(self, uid: str, path: str, fp_index: int = 0,
                  dz: float = 0.0, rx: float = 0.0, ry: float = 0.0,
                  rz: float = 0.0, dx: float = 0.0, dy: float = 0.0
                  ) -> Optional[Component]:
        """
        Подгрузить или заменить 3D-модель посадочного места.

        STEP кладётся как есть; OBJ/STL конвертируются в фасетный STEP --
        Altium другого не понимает.
        """
        c, fp = self._fp_of(uid, fp_index)
        if not c or fp is None:
            raise ValueError("Компонент или посадочное место не найдено")
        self.undo.touch(uid)
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        ext = os.path.splitext(path)[1].lower()
        os.makedirs(self.cfg.models_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(path))[0]
        if ext in (".step", ".stp"):
            dst = os.path.join(self.cfg.models_dir, os.path.basename(path))
            if os.path.abspath(path) != os.path.abspath(dst):
                shutil.copy2(path, dst)
        elif ext in (".obj", ".wrl", ".stl"):
            from .mesh2step import obj_to_step
            dst = os.path.join(self.cfg.models_dir, base + ".step")
            self.log(f"Конвертирую {os.path.basename(path)} -> STEP ...")
            obj_to_step(path, dst, name=base, log=self.log)
        else:
            raise ValueError(f"Не знаю такой формат модели: {ext}. "
                             f"Нужен .step/.stp, либо .obj/.stl для конвертации.")
        fp.model = Model3D(path=dst, dx=dx, dy=dy, dz=dz, rx=rx, ry=ry, rz=rz)
        self.db.upsert(c)
        self.log(f"{c.name}: 3D-модель -> {os.path.basename(dst)}")
        return c

    def clear_model(self, uid: str, fp_index: int = 0) -> Optional[Component]:
        c, fp = self._fp_of(uid, fp_index)
        if not c or fp is None:
            return None
        fp.model = None
        self.db.upsert(c)
        self.log(f"{c.name}: 3D-модель отвязана")
        return c

    def set_model_transform(self, uid: str, fp_index: int = 0, **kw
                            ) -> Optional[Component]:
        c, fp = self._fp_of(uid, fp_index)
        if not c or fp is None or not fp.model:
            return None
        for k in ("dx", "dy", "dz", "rx", "ry", "rz"):
            if k in kw and kw[k] is not None:
                setattr(fp.model, k, float(kw[k]))
        self.db.upsert(c)
        return c

    def set_tape_rotation(self, uid: str, angle: float, fp_index: int = 0
                          ) -> Optional[Component]:
        """Угол корпуса в ленте поставщика — для файлов сборки."""
        c, fp = self._fp_of(uid, fp_index)
        if not c or fp is None or not fp.model:
            return None
        fp.model.tape_rot = float(angle) % 360.0
        self.db.upsert(c)
        self.log(f"{c.name}: угол в ленте {fp.model.tape_rot:g}°")
        return c

    def set_height(self, uid: str, height: float, fp_index: int = 0
                   ) -> Optional[Component]:
        c, fp = self._fp_of(uid, fp_index)
        if not c or fp is None:
            return None
        fp.height = float(height)
        self.db.upsert(c)
        return c

    # -------------------------------------------------- варианты выгрузки ----
    # Altium официально умеет импортировать чужие форматы сам, поэтому кроме
    # прямой записи двоичных библиотек есть ещё два пути, где .SchLib/.PcbLib
    # делает САМ Altium из текстовых файлов:
    #   kicad  -- File > Import Wizard > "KiCad Design Files" (нужно расширение
    #             "KiCad Importer" в Extensions & Updates);
    #   eagle  -- File > Import Wizard > "EAGLE Projects and Designs"
    #             (принимает XML версий 6.4 ... 9.4).

    VARIANTS = {
        "script": "Собрать в Altium (штатным API, основной путь)",
        "binary": "Прямая запись .SchLib/.PcbLib",
        "kicad": "Через штатный импортёр KiCad (.kicad_sym + .pretty)",
        "eagle": "Через импортёр EAGLE (.lbr)",
    }

    def export_dir(self, kind: str) -> str:
        d = os.path.join(self.cfg.root, "export", kind)
        os.makedirs(d, exist_ok=True)
        return d

    def export_eagle(self, comps: List[Component]) -> Dict[str, str]:
        """EAGLE .lbr -- дальше File > Import Wizard > EAGLE."""
        from .emit.eaglelbr import write_lbr
        d = self.export_dir("eagle")
        path = os.path.join(d, self.cfg.library_name + ".lbr")
        write_lbr(path, comps, lib_name=self.cfg.library_name)
        st = getattr(write_lbr, "last_stats", {}) or {}
        self.log(f"EAGLE-библиотека записана: {path}")
        self.log(f"  компонентов {st.get('components', len(comps))}, "
                 f"символов {st.get('symbols', 0)}, "
                 f"корпусов {st.get('packages', 0)}")
        self._check_xml(path)
        return {"path": path, "dir": d,
                "components": str(st.get("components", len(comps))),
                "footprints": str(st.get("packages", 0))}

    def export_kicad_bundle(self, comps: List[Component]) -> Dict[str, str]:
        """KiCad .kicad_sym + .pretty -- дальше File > Import Wizard > KiCad."""
        from .emit.kicadout import write_kicad_bundle
        d = self.export_dir("kicad")
        res = write_kicad_bundle(d, comps, lib_name=self.cfg.library_name)
        self.log(f"KiCad-библиотека записана: {res['sym']}")
        self.log(f"  посадок в {os.path.basename(str(res['pretty']))}: "
                 f"{res['footprints']}")
        return {"path": str(res["sym"]), "dir": d,
                "pretty": str(res["pretty"]),
                "components": str(res["components"]),
                "footprints": str(res["footprints"])}

    def _check_xml(self, path: str) -> bool:
        """Проверка, что XML вообще разбирается -- ошибки видны сразу, а не в Altium."""
        try:
            import xml.etree.ElementTree as ET
            ET.parse(path)
            return True
        except Exception as e:
            self.log(f"  ВНИМАНИЕ: XML не разбирается: {e}")
            return False

    def build_variant(self, kind: str, uids: Iterable[str] = None,
                      only: bool = False) -> Dict[str, str]:
        """
        Единая точка для всех кнопок конвертации.
        Возвращает словарь с путями и подсказкой, что делать дальше.

        Для основного пути (`script`) состав определяет цель -- проект или
        общая библиотека; `uids` нужны только при `only=True` (разовая
        сборка выделенного). Остальные варианты -- выгрузка выделенного.
        """
        if kind == "script":
            return self.build_script_job(uids, only=only)

        uids = list(uids or [])
        comps = self.collect(uids)
        if not comps:
            raise ValueError("Не выбрано ни одного компонента")
        self.db.set_in_library(uids, True)
        lib_comps = self.collect(self.db.all_uids(only_lib=True)) or comps

        if kind == "binary":
            res = self.build_job(uids)
            res["kind"] = "binary"
            res["hint"] = (
                "Библиотеки записаны напрямую. В Altium: DXP > Run Script > "
                "GostLibBuilder (подменит занятые файлы и подключит библиотеки). "
                "Если Altium ругается на файл -- используйте вариант KiCad или "
                "EAGLE, там библиотеку собирает сам Altium.")
            return res
        if kind == "kicad":
            res = self.export_kicad_bundle(lib_comps)
            res["kind"] = "kicad"
            res["hint"] = (
                "В Altium: File > Import Wizard > «KiCad Design Files» > Next, "
                "кнопкой Add добавьте GOST_Lib.kicad_sym и все .kicad_mod из "
                "папки GOST_Lib.pretty. Altium сам создаст .SchLib и .PcbLib.\n"
                "Если этого пункта нет в мастере -- поставьте расширение "
                "«KiCad Importer» (Settings > Extensions and Updates).")
            return res
        if kind == "eagle":
            res = self.export_eagle(lib_comps)
            res["kind"] = "eagle"
            res["hint"] = (
                "В Altium: File > Import Wizard > «EAGLE Projects and Designs» "
                "> Next > Add, выберите GOST_Lib.lbr. Altium создаст .SchLib, "
                ".PcbLib и соберёт .IntLib.")
            return res
        raise ValueError(f"Неизвестный вариант конвертации: {kind}")

    def read_job_log(self, log_path: str) -> str:
        if not os.path.isfile(log_path):
            return ""
        for enc in ("utf-8", "cp1251", "latin1"):
            try:
                with open(log_path, encoding=enc) as f:
                    return f.read()
            except Exception:
                continue
        return ""

    # ------------------------------------------------------------ прочее ----
    def apply_params(self, uid: str, params: Dict[str, str],
                     ctype: str = "", designator: str = "",
                     name: str = "", rebuild: bool = True,
                     designator_manual: bool = True) -> Optional[Component]:
        c = self.db.get(uid)
        if not c:
            return None
        c.params = dict(params)
        if ctype:
            c.ctype = ctype
        if designator:
            c.designator = designator
        # Обозначение едет за типом всегда, когда пользователь не набирал его
        # руками. Раньше проверялась только смена типа, и «U?» у компонента,
        # уже помеченного как микроконтроллер, так и оставался «U?».
        if ctype and (not designator_manual
                      or self.is_default_designator(c.designator)):
            c.designator = classify.designator_for(ctype)
        if name:
            c.name = name
        c.manufacturer = params.get("Manufacturer", c.manufacturer)
        c.mpn = params.get("MPN", c.mpn)
        c.value = params.get("Value", c.value)
        c.datasheet = params.get("Datasheet", c.datasheet)
        if rebuild:
            self.rebuild_symbol(c)
        self.db.upsert(c)
        return c

    @staticmethod
    def is_default_designator(text: str) -> bool:
        """
        Похоже ли обозначение на автоматически проставленное.

        Автоматическим считаем пустое, «безродное» U/IC и любое, совпадающее
        с дефолтом какого-нибудь типа (R?, C?, DA?, DD?...). Всё остальное --
        например DD1 или DA2.1 -- пользователь набрал сам, и мы его не трогаем.
        """
        cur = (text or "").strip()
        if not cur:
            return True
        if cur.upper() in ("U", "U?", "IC", "IC?", "?"):
            return True
        defaults = set()
        for code in classify.CTYPE_CODES:
            d = classify.designator_for(code)
            defaults.add(d.upper())
            defaults.add(d.rstrip("?").upper())
        return cur.upper() in defaults

    @staticmethod
    def auto_designator(old_ctype: str, new_ctype: str, current: str) -> str:
        """Каким станет обозначение при смене типа."""
        if Service.is_default_designator(current):
            return classify.designator_for(new_ctype)
        return (current or "").strip()

    def set_type(self, uids: Iterable[str], ctype: str,
                 fix_designator: bool = True) -> int:
        """Назначить тип вручную и перерисовать символ."""
        n = 0
        for u in uids:
            c = self.db.get(u)
            if not c or c.ctype == ctype:
                continue
            self.undo.touch(u)
            old = c.ctype
            c.ctype = ctype
            if fix_designator:
                c.designator = self.auto_designator(old, ctype, c.designator)
            self.rebuild_symbol(c)
            self.db.upsert(c)
            n += 1
            self.log(f"{c.name}: тип -> {classify.CTYPE_NAME.get(ctype, ctype)}")
        return n

    def apply_layout(self, uid: str, sym) -> Optional[Component]:
        """Сохранить ручную раскладку выводов из редактора символа."""
        c = self.db.get(uid)
        if not c:
            return None
        self.undo.touch(uid)
        c.symbol = sym
        c.symbol.manual_layout = True
        symbolgen.build(c, self.style())
        self.db.upsert(c)
        self.log(f"{c.name}: раскладка выводов сохранена вручную")
        return c

    def reset_layout(self, uid: str) -> Optional[Component]:
        """Вернуться к автоматической раскладке по ГОСТ."""
        c = self.db.get(uid)
        if not c:
            return None
        self.undo.touch(uid)
        c.symbol.manual_layout = False
        c.symbol.dividers = []
        c.symbol.body_w = c.symbol.body_h = 0
        c.symbol.field_l = c.symbol.field_r = 0
        c.symbol.user_lines = []
        c.symbol.parts = {}          # и геометрия секций тоже
        for lst in (c.raw_pins, c.symbol.pins,
                    getattr(c, "native_pins", None) or []):
            for p in lst:
                p.manual = False
                p.group = ""
        self.rebuild_symbol(c)
        self.db.upsert(c)
        self.log(f"{c.name}: раскладка снова автоматическая")
        return c

    def rename(self, uid: str, name: str) -> Optional[Component]:  # noqa: D401
        """
        Переименовать компонент.

        Имя -- это LibReference в Altium, по нему компонент ищут в панели
        Components. Из KiCad приезжают безликие «R», «C», «D», и когда их
        набирается десяток, отличить их невозможно. Дубликаты не запрещаем
        (бывает осмысленно), но предупреждаем.
        """
        c = self.db.get(uid)
        if not c:
            return None
        self.undo.touch(uid)
        name = (name or "").strip()
        if not name:
            raise ValueError("Имя не может быть пустым")
        bad = set('\\/:*?"<>|')
        if bad & set(name):
            raise ValueError("В имени нельзя использовать \\ / : * ? \" < > |")
        if name == c.name:
            return c
        same = [r for r in self.db.search(name, "")
                if r["name"] == name and r["uid"] != uid]
        old = c.name
        c.name = name
        self.db.upsert(c)
        self.db.log(uid, f"переименован: {old} -> {name}")
        self.log(f"{old} -> {name}"
                 + (f"  (внимание: имя уже занято, компонентов с ним "
                    f"{len(same) + 1})" if same else ""))
        return c

    def suggest_name(self, c: Component) -> str:
        """
        Осмысленное имя вместо «R»: тип, номинал, корпус.

        Ровно то, чего не хватает после импорта из KiCad, где все
        резисторы называются R.
        """
        parts = []
        if c.mpn:
            parts.append(c.mpn)
        else:
            base = classify.CTYPE_PREFIX.get(c.ctype, "U")
            val = (c.value or (c.params or {}).get("Value") or "").strip()
            parts.append(base + ("_" + val if val else ""))
        pkg = ((c.params or {}).get("Package")
               or (c.footprints[0].name if c.footprints else "")).strip()
        if pkg and pkg.lower() not in parts[0].lower():
            parts.append(pkg)
        name = "_".join(p for p in parts if p)
        return _safe_name(name) or c.name

    def apply_pins(self, uid: str, pins: List[dict]) -> Optional[Component]:
        """pins: [{'number','name','etype','unit','side','group'}] -- ручная раскладка."""
        c = self.db.get(uid)
        if not c:
            return None
        self.undo.touch(uid)
        by_num = {p.number: p for p in c.raw_pins}
        # Порядок строк в таблице = порядок выводов сверху вниз. Для
        # разъёмов это единственный вменяемый способ: там ни имена, ни
        # номера не идут по порядку, в котором их надо показать.
        for i, row in enumerate(pins):
            p = by_num.get(row.get("number", ""))
            if not p:
                continue
            p.name = row.get("name", p.name)
            p.etype = row.get("etype", p.etype)
            p.unit = int(row.get("unit", p.unit) or 1)
            p.order = i
            # Порядок строк задан руками -- значит он важнее и правил
            # группировки, и сортировки по номеру. Раньше эта «ручность»
            # выводилась из группы с «!», а группа ставилась только когда
            # в строке была заполнена сторона. У разъёма сторона пустая,
            # группы не появлялись, и раскладка применялась лишь со
            # второго раза -- когда сторона «R» успевала сохраниться.
            p.manual = True
            side = (row.get("side") or "").upper()
            grp = (row.get("group") or "").strip()
            if side in ("L", "R", "T", "B"):
                p.side = side
                p.group = "!" + (grp or side)
            elif grp:
                p.group = grp if grp.startswith("!") else "!" + grp
            else:
                # сторону не трогаем: её никто не задавал
                p.group = "!" + (p.side or "L")
        # порядок в raw_pins тоже переставляем -- он определяет вид таблицы
        # при следующем открытии
        order = {row.get("number", ""): i for i, row in enumerate(pins)}
        c.raw_pins.sort(key=lambda q: order.get(q.number, 10 ** 6))

        # Имя и тип надо перенести и в те списки, из которых потом
        # собирается символ. native_pins хранят геометрию из KiCad, а
        # ручная раскладка живёт в symbol.pins -- без этого правка имени
        # в таблице просто не доезжала до УГО.
        for lst in (getattr(c, "native_pins", None) or [], c.symbol.pins):
            for q in lst:
                src = by_num.get(q.number)
                if src is not None:
                    q.name = src.name
                    q.etype = src.etype
                    q.unit = src.unit
                    q.order = src.order
                    q.side = src.side
                    q.group = src.group
                    q.manual = True
        self.rebuild_symbol(c)
        self.db.upsert(c)
        return c
